#!/usr/bin/env python3
"""Replay one past session through the desk's rules. Read-only: no orders.

    python3 replay.py                          # last completed trading day
    python3 replay.py 2026-09-22               # one day, cycle-by-cycle detail
    python3 replay.py 2026-06-22 2026-09-22    # a range, summary + per-day table
    python3 replay.py --days 63                # last 63 trading days (~3 months)

Pulls 1-minute bars from Alpaca, rebuilds what each 10-minute cycle
would have seen, and simulates entries, bracket exits and the close
flatten under two rule sets:

  OLD  pre-ALBOT PR #2 rules: score >= 55, entries 09:45-15:00, flatten 15:30,
       re-entry allowed after an exit.
  NEW  current engine.py: score >= 75, one entry per day, times from
       the real close (close-60 entries stop, close-30 flatten).

Past event blackouts only apply for dates listed in events.json, which
starts in 2026-09; earlier days replay with no blackout.

Assumptions: cycles fire exactly on :00/:10/... (real Actions runs lag a
few minutes); market orders fill at the next minute's open; if a minute
touches both stop and target, the stop is assumed to hit first.
"""
from __future__ import annotations

import os
import sys
import urllib.parse
from datetime import date, datetime, timedelta, timezone

import engine

ET = engine.ET


def get(host, path, params):
    st, body = engine.api(host, f"{path}?{urllib.parse.urlencode(params)}")
    if st != 200:
        raise SystemExit(f"{path} failed {st} {body}")
    return body


def calendar(start, end):
    return get(engine.ALPACA, "/v2/calendar", {"start": start.isoformat(), "end": end.isoformat()})


def bars(symbols, timeframe, start, end, adjustment="raw"):
    # SIP (all exchanges) when the plan allows it for historical data,
    # else IEX, which is what the free live snapshot uses.
    out, feed_used = {s: [] for s in symbols}, None
    for feed in ("sip", "iex"):
        params = {
            "symbols": ",".join(symbols), "timeframe": timeframe,
            "start": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 10000, "adjustment": adjustment, "feed": feed,
        }
        ok = True
        token = None
        while True:
            if token:
                params["page_token"] = token
            st, body = engine.api(engine.DATA, f"/v2/stocks/bars?{urllib.parse.urlencode(params)}")
            if st != 200:
                ok = False
                break
            for sym, rows in (body.get("bars") or {}).items():
                out[sym].extend(rows)
            token = body.get("next_page_token")
            if not token:
                break
        if ok:
            feed_used = feed
            break
        out = {s: [] for s in symbols}
    if not feed_used:
        raise SystemExit(f"bars {timeframe} unavailable on sip and iex")
    for rows in out.values():
        for r in rows:
            r["dt"] = datetime.fromisoformat(r["t"].replace("Z", "+00:00")).astimezone(ET)
        rows.sort(key=lambda r: r["dt"])
    return out, feed_used


def old_score(q):
    # score() as it was before PR #2, kept here for comparison.
    n = 50
    if q["gap"] < -0.012:
        n -= 20
    elif q["gap"] > 0:
        n += 16
    if q["range_pct"] > 0.008:
        n += 8
    if q["loc"] >= 0.55:
        n += 18
    elif q["loc"] <= 0.34:
        n += 10
    return max(0, min(100, n))


def card(symbol, mins, prev_c, t):
    seen = [b for b in mins if b["dt"] + timedelta(minutes=1) <= t]
    if not seen:
        return None
    last = seen[-1]["c"]
    open_p = seen[0]["o"]
    high = max(b["h"] for b in seen)
    low = min(b["l"] for b in seen)
    span = max(high - low, 0.01)
    return {"symbol": symbol, "last": last, "loc": (last - low) / span,
            "gap": (open_p - prev_c) / prev_c, "range_pct": span / last}


def simulate(name, rules, mins, prev, open_dt, close_dt, lines):
    score_fn, score_min, entry_end, flat_at, one_per_day = rules
    pos = None
    trades = []
    entered = False
    t = open_dt
    while t <= close_dt:
        # Exits between the previous cycle and this one.
        if pos:
            for b in mins[pos["sym"]]:
                if not (pos["from"] <= b["dt"] < t):
                    continue
                if b["o"] <= pos["stop"]:
                    pos.update(exit=b["o"], why="stop (gapped)", at=b["dt"])
                elif b["l"] <= pos["stop"]:
                    pos.update(exit=pos["stop"], why="stop", at=b["dt"])
                elif b["o"] >= pos["target"]:
                    pos.update(exit=b["o"], why="target (gapped)", at=b["dt"])
                elif b["h"] >= pos["target"]:
                    pos.update(exit=pos["target"], why="target", at=b["dt"])
                if "exit" in pos:
                    break
            if "exit" in pos:
                trades.append(pos)
                lines.append(f"  {pos['at']:%H:%M} {name} EXIT {pos['sym']} {pos['why']} @ {pos['exit']:.2f}")
                pos = None
            else:
                pos["from"] = t
        clock = t.strftime("%H:%M")
        if t >= flat_at:
            if pos:
                nxt = [b for b in mins[pos["sym"]] if b["dt"] >= t]
                px = nxt[0]["o"] if nxt else mins[pos["sym"]][-1]["c"]
                pos.update(exit=px, why="close flatten", at=t)
                trades.append(pos)
                lines.append(f"  {clock} {name} EXIT {pos['sym']} close flatten @ {px:.2f}")
                pos = None
            t += timedelta(minutes=10)
            continue
        if pos or clock < engine.ENTRY_START or t > entry_end or (one_per_day and entered):
            t += timedelta(minutes=10)
            continue
        if engine.blackout(t):
            lines.append(f"  {clock} {name} blackout")
            t += timedelta(minutes=10)
            continue
        cards = []
        for sym in engine.SYMBOLS:
            q = card(sym, mins[sym], prev[sym], t)
            if q:
                q["score"] = score_fn(q)
                cards.append(q)
        cards.sort(key=lambda x: x["score"], reverse=True)
        for q in cards:
            if q["score"] < score_min:
                continue
            last = q["last"]
            stop = round(last * (1 - engine.STOP_PCT), 2)
            target = round(last + engine.TARGET_R * (last - stop), 2)
            qty = int(min(engine.MAX_NOTIONAL / last, engine.MAX_RISK / max(last - stop, 0.01)))
            nxt = [b for b in mins[q["symbol"]] if b["dt"] >= t]
            if qty < 1 or not nxt:
                continue
            fill = nxt[0]["o"]
            pos = {"sym": q["symbol"], "qty": qty, "entry": fill, "stop": stop, "target": target,
                   "in": t, "from": t, "score": q["score"]}
            entered = True
            lines.append(f"  {clock} {name} BUY {qty} {q['symbol']} @ {fill:.2f} "
                         f"(score {q['score']}, stop {stop}, target {target})")
            break
        t += timedelta(minutes=10)
    if pos:
        px = mins[pos["sym"]][-1]["c"]
        pos.update(exit=px, why="held past close (GTC legs)", at=close_dt)
        trades.append(pos)
    return trades


def load(first, last):
    # Sessions, per-day minute bars and prior closes for first..last.
    cal = calendar(first - timedelta(days=10), last)
    idx = [i for i, c in enumerate(cal) if first.isoformat() <= c["date"] <= last.isoformat()]
    if not idx:
        raise SystemExit(f"no trading days in {first}..{last}")
    if idx[0] == 0:
        raise SystemExit("calendar lookback too short for the prior close")
    sessions = []
    for i in idx:
        c = cal[i]
        sessions.append({
            "date": c["date"], "prev": cal[i - 1]["date"],
            "open": datetime.fromisoformat(f"{c['date']}T{c['open']}").replace(tzinfo=ET),
            "close": datetime.fromisoformat(f"{c['date']}T{c['close']}").replace(tzinfo=ET),
        })
    mins, feed = bars(engine.SYMBOLS, "1Min", sessions[0]["open"], sessions[-1]["close"])
    daily, _ = bars(engine.SYMBOLS, "1Day",
                    datetime.fromisoformat(f"{sessions[0]['prev']}T00:00").replace(tzinfo=ET),
                    sessions[-1]["close"])
    # Daily bars are stamped at midnight ET (04:00Z or 05:00Z), so the
    # UTC date in the stamp is the trading date in either season.
    closes = {s: {b["t"][:10]: b["c"] for b in daily[s]} for s in engine.SYMBOLS}
    for sess in sessions:
        sess["mins"] = {s: [b for b in mins[s] if sess["open"] <= b["dt"] < sess["close"]] for s in engine.SYMBOLS}
        sess["prev_c"] = {s: closes[s].get(sess["prev"]) for s in engine.SYMBOLS}
    return sessions, feed


def rule_sets(sess):
    o, c = sess["open"], sess["close"]
    return (
        ("OLD", (old_score, 55, o.replace(hour=15, minute=0), o.replace(hour=15, minute=30), False)),
        ("NEW", (engine.score, engine.SCORE_MIN,
                 c - timedelta(minutes=engine.ENTRY_STOP_BEFORE_CLOSE_MIN),
                 c - timedelta(minutes=engine.FLAT_BEFORE_CLOSE_MIN), True)),
    )


def pl(tr):
    return (tr["exit"] - tr["entry"]) * tr["qty"]


def day_move(sess, s):
    m, prev_c = sess["mins"][s], sess["prev_c"][s]
    if not m or not prev_c:
        return None
    return 100 * (m[-1]["c"] - prev_c) / prev_c


def single(sess, feed):
    mins, prev = sess["mins"], sess["prev_c"]
    open_dt, close_dt = sess["open"], sess["close"]
    out = [f"# Replay {sess['date']} ({open_dt:%H:%M}-{close_dt:%H:%M} ET, {feed.upper()} bars)", ""]
    out.append("## Market")
    for s in engine.SYMBOLS:
        m = mins[s]
        if not m:
            out.append(f"- {s}: no bars")
            continue
        o, c = m[0]["o"], m[-1]["c"]
        h, low = max(b["h"] for b in m), min(b["l"] for b in m)
        out.append(f"- {s}: prev close {prev[s]:.2f}, open {o:.2f} (gap {100 * (o - prev[s]) / prev[s]:+.2f}%), "
                   f"high {h:.2f}, low {low:.2f}, close {c:.2f} (day {100 * (c - prev[s]) / prev[s]:+.2f}%)")

    out += ["", "## Scores each cycle (old / new)", "", "| time | " + " | ".join(engine.SYMBOLS) + " |",
            "|---|" + "---|" * len(engine.SYMBOLS)]
    t = open_dt + timedelta(minutes=10)
    while t < close_dt:
        row = []
        for s in engine.SYMBOLS:
            q = card(s, mins[s], prev[s], t)
            row.append(f"{old_score(q)} / {engine.score(q)} (loc {q['loc']:.2f})" if q else "-")
        out.append(f"| {t:%H:%M} | " + " | ".join(row) + " |")
        t += timedelta(minutes=10)

    for name, rules in rule_sets(sess):
        lines = []
        trades = simulate(name, rules, mins, prev, open_dt, close_dt, lines)
        out += ["", f"## {name} rules: {len(trades)} trade(s), P&L ${sum(map(pl, trades)):+.2f}", "```"]
        out += lines or ["  no entries"]
        for tr in trades:
            out.append(f"  {tr['sym']} x{tr['qty']} {tr['in']:%H:%M}->{tr['at']:%H:%M} "
                       f"{tr['entry']:.2f}->{tr['exit']:.2f} {tr['why']}: ${pl(tr):+.2f}")
        out.append("```")
    return out


def stats(name, days):
    trades = [tr for d in days for tr in d["trades"]]
    daily = [sum(map(pl, d["trades"])) for d in days]
    wins = [pl(t) for t in trades if pl(t) > 0]
    losses = [pl(t) for t in trades if pl(t) <= 0]
    equity = peak = dd = 0.0
    for x in daily:
        equity += x
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
    why = {}
    for t in trades:
        k = t["why"].split(" (")[0]
        why[k] = why.get(k, 0) + 1
    n = len(trades)
    return (f"| {name} | {n} | {len(wins)} / {len(losses)} | {100 * len(wins) / n if n else 0:.0f}% "
            f"| ${sum(daily):+.2f} | ${sum(daily) / n if n else 0:+.2f} "
            f"| ${max(wins, default=0):+.2f} / ${min(losses, default=0):+.2f} "
            f"| ${min(daily, default=0):+.2f} | ${dd:+.2f} "
            f"| {', '.join(f'{k} {v}' for k, v in sorted(why.items())) or '-'} |")


def multi(sessions, feed):
    results = {"OLD": [], "NEW": []}
    rows = []
    for sess in sessions:
        if not all(sess["mins"][s] and sess["prev_c"][s] for s in engine.SYMBOLS):
            rows.append(f"| {sess['date']} | missing data | | | |")
            continue
        cells = {}
        for name, rules in rule_sets(sess):
            trades = simulate(name, rules, sess["mins"], sess["prev_c"], sess["open"], sess["close"], [])
            for tr in trades:
                tr["date"] = sess["date"]
            results[name].append({"date": sess["date"], "trades": trades})
            cells[name] = ("-" if not trades else
                           f"${sum(map(pl, trades)):+.2f} ({', '.join(t['sym'] + ' ' + t['why'].split(' (')[0] for t in trades)})")
        moves = " | ".join(f"{day_move(sess, s):+.2f}%" for s in engine.SYMBOLS)
        rows.append(f"| {sess['date']} | {moves} | {cells['OLD']} | {cells['NEW']} |")

    first, last = sessions[0]["date"], sessions[-1]["date"]
    out = [f"# Replay {first} to {last} ({len(sessions)} trading days, {feed.upper()} bars)", "",
           "Read-only simulation; no orders placed. Fills at next minute's open, stop assumed first if a minute "
           "touches both, cycles exactly every 10 minutes.", "",
           "## Summary", "",
           "| rules | trades | wins / losses | win rate | total P&L | avg / trade | best / worst trade "
           "| worst day | max drawdown | exits |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    out += [stats(name, days) for name, days in results.items()]
    out += ["", "## Per day", "", "| date | " + " | ".join(f"{s} day" for s in engine.SYMBOLS) + " | OLD | NEW |",
            "|---|" + "---|" * len(engine.SYMBOLS) + "---|---|"] + rows
    out += ["", "## NEW trades", "```"]
    new_trades = [tr for d in results["NEW"] for tr in d["trades"]]
    out += [f"  {tr['date']} {tr['sym']} x{tr['qty']} {tr['in']:%H:%M}->{tr['at']:%H:%M} "
            f"{tr['entry']:.2f}->{tr['exit']:.2f} score {tr['score']} {tr['why']}: ${pl(tr):+.2f}"
            for tr in new_trades] or ["  none"]
    out.append("```")
    return out


def main():
    args = [a.strip() for a in sys.argv[1:] if a.strip()]
    today = datetime.now(ET).date()
    if args[:1] == ["--days"]:
        n = int(args[1])
        cal = calendar(today - timedelta(days=int(n * 1.6) + 10), today - timedelta(days=1))
        first, last = date.fromisoformat(cal[-n]["date"]), date.fromisoformat(cal[-1]["date"])
    elif len(args) >= 2:
        first, last = date.fromisoformat(args[0]), date.fromisoformat(args[1])
    elif args:
        first = last = date.fromisoformat(args[0])
    else:
        cal = calendar(today - timedelta(days=10), today - timedelta(days=1))
        first = last = date.fromisoformat(cal[-1]["date"])
    if first == last:
        sessions, feed = load(first, last)
        if sessions[0]["date"] != first.isoformat():
            raise SystemExit(f"{first} was not a trading day")
        out = single(sessions[0], feed)
    else:
        sessions, feed = load(first, last)
        out = multi(sessions, feed)

    text = "\n".join(out)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
