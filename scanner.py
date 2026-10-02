#!/usr/bin/env python3
"""Daily trend scan: the 20 strongest-trending liquid US stocks and ETFs.
Read-only: places no orders and changes nothing the bot uses.

    python3 scanner.py            # scan as of the latest completed daily bar
    python3 scanner.py --post     # also comment on the `market-scan` issue

Universe: a fixed list of big caps and major ETFs plus Alpaca's 50 most
active stocks by volume today. Names under $10 or under $250M average
daily dollar volume are dropped, and so are leveraged and inverse ETFs, so the scan only lists things the bot
could trade in size without slippage worries.

Trend score (higher = stronger, steadier uptrend):
    momentum = average of the 3-month and 6-month returns
    score    = momentum / annualised volatility (63 days)
A name counts as an uptrend only when close > 50-day avg > 200-day avg.
Also reported: market regime (SPY vs its 200-day), breadth (share of the
universe above its 50-day), and the 5 weakest names.

Writes scan.json (full table) and the markdown summary to the run's
Summary tab. Uses daily bars, so it can run any time after the close
or before the next open; a late run is still correct.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

import engine
import replay

ET = engine.ET
TOP_N = 20
WEAK_N = 5
MIN_PRICE = 10.0
MIN_DOLLAR_VOL = 250e6
HISTORY_DAYS = 400  # calendar days, enough for a 200-day average
OUT_PATH = engine.ROOT / "scan.json"
LABEL = "market-scan"
ISSUE_TITLE = "Daily trend scan: top 20"

# Large, liquid names that should always be looked at, whatever today's
# volume leaders are. Broad index and sector ETFs first.
CORE = (
    "SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY",
    "XLP", "XLU", "XLB", "XLC", "XLRE", "SMH", "TLT", "GLD",
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "BRK.B",
    "JPM", "LLY", "V", "MA", "UNH", "XOM", "COST", "WMT", "HD", "PG",
    "JNJ", "NFLX", "ORCL", "CRM", "AMD", "ADBE", "PLTR", "BAC", "KO",
    "PEP", "CVX", "MRK", "ABBV", "CSCO", "INTC", "MU", "QCOM", "GS",
    "CAT", "GE", "UBER", "COIN", "MSTR",
)


def most_actives(top=50):
    st, body = engine.api(engine.DATA, f"/v1beta1/screener/stocks/most-actives?by=volume&top={top}")
    if st != 200 or not isinstance(body, dict):
        print(f"most-actives unavailable ({st}); scanning the core list only")
        return []
    return [r["symbol"] for r in body.get("most_actives") or [] if r.get("symbol")]


# Leveraged and inverse ETFs show up among the most active names but
# track a multiple of something else's daily move, so their trends mislead.
LEVERAGED = re.compile(r"\b(2x|3x|-1x|-2x|-3x|ultra\w*|leveraged|inverse|bull|bear|daily target)\b", re.I)


def plain(sym):
    # True for a tradable, non-leveraged asset.
    st, body = engine.api(engine.ALPACA, f"/v2/assets/{urllib.parse.quote(sym)}")
    if st != 200 or not isinstance(body, dict):
        return False
    return bool(body.get("tradable")) and not LEVERAGED.search(body.get("name") or "")


def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def ret(xs, n):
    return xs[-1] / xs[-1 - n] - 1 if len(xs) > n and xs[-1 - n] > 0 else None


def metrics(sym, rows):
    # rows: daily bars oldest first, split/dividend adjusted.
    closes = [float(r["c"]) for r in rows]
    if len(closes) < 130:
        return None
    price = closes[-1]
    dvol = sum(float(r["c"]) * float(r["v"]) for r in rows[-20:]) / min(20, len(rows))
    daily = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - 62, len(closes))]
    mean = sum(daily) / len(daily)
    vol = math.sqrt(sum((d - mean) ** 2 for d in daily) / (len(daily) - 1)) * math.sqrt(252)
    r21, r63, r126 = ret(closes, 21), ret(closes, 63), ret(closes, 126)
    s50, s200 = sma(closes, 50), sma(closes, 200)
    mom = (r63 + r126) / 2
    hi252 = max(closes[-252:])
    return {
        "symbol": sym,
        "date": rows[-1]["dt"].date().isoformat(),
        "price": round(price, 2),
        "dollar_vol_m": round(dvol / 1e6),
        "r1m": round(r21, 4), "r3m": round(r63, 4), "r6m": round(r126, 4),
        "vol": round(vol, 4),
        "score": round(mom / vol, 3) if vol > 0 else 0.0,
        "above50": price > s50,
        "above200": None if s200 is None else price > s200,
        "uptrend": s200 is not None and price > s50 > s200,
        "off_high": round(price / hi252 - 1, 4),
    }


def scan():
    extra = [s for s in most_actives() if s not in CORE]
    dropped = [s for s in extra if not plain(s)]
    if dropped:
        print(f"skipped leveraged/inverse or untradable: {' '.join(dropped)}")
    universe = list(CORE) + [s for s in extra if s not in dropped]
    now = datetime.now(ET)
    # Before 4:20 PM ET today's daily bar is still forming (or missing), so
    # stop at midnight; after it, stay 20 min back (free plan: SIP needs 15).
    if now.hour * 60 + now.minute < 16 * 60 + 20:
        # Daily bars are stamped at midnight ET, so stop just before today's.
        end = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(minutes=1)
    else:
        end = now - timedelta(minutes=20)
    start = end - timedelta(days=HISTORY_DAYS)
    data, feed = {}, None
    for i in range(0, len(universe), 50):
        chunk = universe[i:i + 50]
        try:
            got, feed = replay.bars(chunk, "1Day", start, end, adjustment="all")
            data.update(got)
            continue
        except SystemExit:
            pass
        # One bad symbol fails the whole request, so retry one at a time.
        for sym in chunk:
            try:
                got, feed = replay.bars([sym], "1Day", start, end, adjustment="all")
                data.update(got)
            except SystemExit as exc:
                print(f"skipped {sym}: {exc}")
    rows = [m for s in universe if data.get(s) and (m := metrics(s, data[s]))]
    liquid = [m for m in rows if m["price"] >= MIN_PRICE and m["dollar_vol_m"] * 1e6 >= MIN_DOLLAR_VOL]
    liquid.sort(key=lambda m: m["score"], reverse=True)
    spy = next((m for m in rows if m["symbol"] == "SPY"), None)
    breadth = sum(m["above50"] for m in liquid) / len(liquid) if liquid else 0.0
    return {
        "as_of": max((m["date"] for m in rows), default=None),
        "feed": feed,
        "universe": len(universe),
        "liquid": len(liquid),
        "spy_above200": spy["above200"] if spy else None,
        "breadth50": round(breadth, 3),
        "top": [m for m in liquid if m["uptrend"]][:TOP_N],
        "weakest": liquid[-WEAK_N:][::-1],
        "all": liquid,
    }


def pct(x):
    return "" if x is None else f"{x * 100:+.1f}%"


def table(rows):
    out = ["| # | symbol | price | score | 1m | 3m | 6m | vol | off 52w high | $vol/day |",
           "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for i, m in enumerate(rows, 1):
        out.append(f"| {i} | {m['symbol']} | {m['price']:.2f} | {m['score']:.2f} | {pct(m['r1m'])} | "
                   f"{pct(m['r3m'])} | {pct(m['r6m'])} | {m['vol'] * 100:.0f}% | {pct(m['off_high'])} | "
                   f"${m['dollar_vol_m']:,}M |")
    return out


def render(res):
    regime = {True: "SPY above its 200-day (risk on)", False: "SPY below its 200-day (risk off)",
              None: "SPY regime unknown"}[res["spy_above200"]]
    lines = [
        f"## Trend scan, as of the {res['as_of']} close",
        "",
        f"{regime}. Breadth: {res['breadth50'] * 100:.0f}% of {res['liquid']} liquid names are above "
        f"their 50-day average. Scanned {res['universe']} symbols ({res['feed']} feed).",
        "",
        f"### Top {len(res['top'])} uptrends",
        "Score = average of 3- and 6-month return ÷ annualised volatility. "
        "Only names with close > 50-day > 200-day.",
        "",
        *table(res["top"]),
        "",
        f"### Weakest {len(res['weakest'])}",
        "",
        *table(res["weakest"]),
        "",
        "_Read-only scan from `scanner.py`. It places no orders and the bot does not use it yet._",
    ]
    return "\n".join(lines)


def post(text):
    import report
    if not (os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPOSITORY")):
        print("no GITHUB_TOKEN/GITHUB_REPOSITORY; not posting")
        return
    issues = report.gh("GET", f"/issues?state=open&labels={LABEL}&per_page=1")
    if issues:
        num = issues[0]["number"]
    else:
        num = report.gh("POST", "/issues", {
            "title": ISSUE_TITLE,
            "labels": [LABEL],
            "body": "One comment per trading day from the `scan` workflow: the 20 strongest "
                    "uptrends among liquid US stocks and ETFs. Watch this issue to get it "
                    "as a notification.",
        })["number"]
    report.gh("POST", f"/issues/{num}/comments", {"body": text})
    print(f"posted to issue #{num}")


def main():
    res = scan()
    OUT_PATH.write_text(json.dumps(res, indent=1))
    text = render(res)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(text + "\n")
    if "--post" in sys.argv[1:]:
        post(text)


if __name__ == "__main__":
    main()
