#!/usr/bin/env python3
"""End-of-day report for the Daily Trendy Trader. Read-only on Alpaca.

Posts one comment per trading day on the repo's open issue labelled
`daily-report` (creating the issue the first time), so each day's result
arrives as a GitHub notification. Also writes the same text to the run's
Summary tab.

    python3 report.py              # today (ET)
    python3 report.py 2026-09-23   # a past day

Needs APCA_API_KEY_ID / APCA_API_SECRET_KEY (paper) and, to post,
GITHUB_TOKEN + GITHUB_REPOSITORY (set automatically in Actions).
Without a GitHub token it just prints the report.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import engine

ET = engine.ET
LABEL = "daily-report"
ISSUE_TITLE = "Daily Trendy Trader: daily reports"


def get(path, params=None):
    q = f"?{urllib.parse.urlencode(params)}" if params else ""
    st, body = engine.api(engine.ALPACA, path + q)
    if st != 200:
        raise SystemExit(f"{path} failed {st} {body}")
    return body


def fills(day):
    # All fills for the ET calendar day, oldest first.
    start = datetime.combine(day, datetime.min.time(), ET).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    return get("/v2/account/activities/FILL", {
        "after": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "until": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "direction": "asc",
        "page_size": 100,
    }) or []


def round_trips(rows):
    # Per symbol: buys vs sells for the day. Realized P&L only when the
    # day's buys and sells net to zero shares; otherwise it's still open.
    by = defaultdict(lambda: {"bq": 0.0, "bc": 0.0, "sq": 0.0, "sp": 0.0, "first": None, "last": None})
    for r in rows:
        s = by[r["symbol"]]
        qty, px = float(r["qty"]), float(r["price"])
        t = datetime.fromisoformat(r["transaction_time"].replace("Z", "+00:00")).astimezone(ET)
        s["first"] = s["first"] or t
        s["last"] = t
        if r["side"] == "buy":
            s["bq"] += qty
            s["bc"] += qty * px
        else:
            s["sq"] += qty
            s["sp"] += qty * px
    out = []
    for sym, s in by.items():
        entry = s["bc"] / s["bq"] if s["bq"] else None
        exit_ = s["sp"] / s["sq"] if s["sq"] else None
        closed = abs(s["bq"] - s["sq"]) < 1e-9
        out.append({
            "symbol": sym, "qty": s["bq"] or s["sq"], "entry": entry, "exit": exit_,
            "in": s["first"], "out": s["last"] if s["sq"] else None,
            "pl": s["sp"] - s["bc"] if closed else None,
        })
    return out


def total_since_start(equity_now):
    # Change since the account's first recorded equity (up to a year back).
    hist = get("/v2/account/portfolio/history", {"period": "1A", "timeframe": "1D"})
    seen = [float(e) for e in (hist.get("equity") or []) if isinstance(e, (int, float)) and e]
    if not seen:
        return None, None
    return equity_now - seen[0], seen[0]


def money(x):
    return "n/a" if x is None else f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def build(day):
    cal = get("/v2/calendar", {"start": day.isoformat(), "end": day.isoformat()})
    if not cal:
        return None
    acct = get("/v2/account")
    equity = float(acct["equity"])
    is_today = day == datetime.now(ET).date()
    trips = round_trips(fills(day))
    lines = [f"### {day:%a %Y-%m-%d}", ""]
    if is_today:
        day_pl = equity - float(acct["last_equity"])
        lines.append(f"**Day P&L:** {money(day_pl)}  ")
    if trips:
        lines += ["", "| symbol | qty | in | entry | out | exit | P&L |", "|---|---|---|---|---|---|---|"]
        for t in trips:
            lines.append(
                f"| {t['symbol']} | {t['qty']:g} | {t['in']:%H:%M} | {t['entry'] or 0:.2f} | "
                f"{t['out']:%H:%M} | {t['exit'] or 0:.2f} | {money(t['pl'])} |" if t["out"] else
                f"| {t['symbol']} | {t['qty']:g} | {t['in']:%H:%M} | {t['entry'] or 0:.2f} | open | – | – |"
            )
        # GitHub cron runs can start late; record how late the close flatten ran.
        close_dt = datetime.fromisoformat(f"{day}T{cal[0]['close']}").replace(tzinfo=ET)
        flat_at = close_dt - timedelta(minutes=engine.FLAT_BEFORE_CLOSE_MIN)
        for t in trips:
            if t["out"] and t["out"] >= flat_at:
                lag = (t["out"] - flat_at).total_seconds() / 60
                lines.append(f"\n**Flatten:** {t['symbol']} sold {t['out']:%H:%M} "
                             f"(target {flat_at:%H:%M}, {lag:.0f} min late)")
    else:
        lines.append("**Trades:** none (no setup scored ≥ 75, or blocked by a blackout/halt)")
    total, base = total_since_start(equity)
    lines += ["", f"**Equity:** ${equity:,.2f} · **Since start:** {money(total)}"
              + (f" (from ${base:,.2f})" if base else "")]
    st, pos = engine.api(engine.ALPACA, "/v2/positions")
    held = [p for p in pos if isinstance(p, dict)] if st == 200 and isinstance(pos, list) else []
    if held:
        lines.append(f"**⚠ Still holding after close:** {', '.join(p['symbol'] for p in held)} "
                     "(flatten missed; GTC stop/target still protect it)")
    return "\n".join(lines)


def gh(method, path, payload=None):
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}{path}",
        data=None if payload is None else json.dumps(payload).encode(),
        method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=15) as res:
        return json.loads(res.read().decode() or "null")


def post(text):
    if not (os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPOSITORY")):
        print("no GITHUB_TOKEN/GITHUB_REPOSITORY; not posting")
        return
    issues = gh("GET", f"/issues?state=open&labels={LABEL}&per_page=1")
    if issues:
        num = issues[0]["number"]
    else:
        num = gh("POST", "/issues", {
            "title": ISSUE_TITLE,
            "labels": [LABEL],
            "body": "One comment per trading day from the `report` workflow. "
                    "Watch this issue to get each day's result as a notification.",
        })["number"]
    gh("POST", f"/issues/{num}/comments", {"body": text})
    print(f"posted to issue #{num}")


def main():
    arg = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    day = date.fromisoformat(arg) if arg else datetime.now(ET).date()
    text = build(day)
    if text is None:
        print(f"{day} was not a trading day; nothing to report")
        return
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(text + "\n")
    post(text)


if __name__ == "__main__":
    main()
