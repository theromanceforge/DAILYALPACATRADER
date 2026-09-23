#!/usr/bin/env python3
"""Intraday momentum test (Gao, Han, Li & Zhou 2018). Read-only: no orders.

Pre-registered, no parameters:
  signal  = return from the prior regular-session close to 10:00 ET
  trade   = at close-30 go long if signal > 0, short if < 0
  exit    = at the close (market-on-close live)
Primary test: SPY, all available history. QQQ and 2024+ are secondary.
Uses 30-minute split/dividend-adjusted bars: the first regular bar's close
is the 10:00 price, the last bar's open/close are close-30 and the close
(early-close days handled via Alpaca's calendar). Costs COST_BPS per side.
$2,500 notional per trade to match the live bot.
"""
from __future__ import annotations

import math
import os
from collections import defaultdict
from datetime import date, datetime, timedelta

import engine
import replay

ET = engine.ET
COST_BPS = 1.0
NOTIONAL = engine.MAX_NOTIONAL
START = date(2016, 1, 4)


def days_for(symbol, cal, bars):
    # Per session: (prev regular close, 10:00 price, close-30 price, close).
    by_day = defaultdict(list)
    for b in bars:
        by_day[b["dt"].date().isoformat()].append(b)
    out, prev_close = [], None
    for c in cal:
        o = datetime.fromisoformat(f"{c['date']}T{c['open']}").replace(tzinfo=ET)
        cl = datetime.fromisoformat(f"{c['date']}T{c['close']}").replace(tzinfo=ET)
        reg = {b["dt"]: b for b in by_day.get(c["date"], []) if o <= b["dt"] < cl}
        first, last = reg.get(o), reg.get(cl - timedelta(minutes=30))
        if first and last and prev_close:
            out.append({"date": c["date"], "sig": first["c"] / prev_close - 1,
                        "entry": last["o"], "exit": last["c"]})
        prev_close = last["c"] if last else None
    return out


def trades(days):
    res = []
    for d in days:
        if d["sig"] == 0:
            continue
        side = 1 if d["sig"] > 0 else -1
        r = side * (d["exit"] / d["entry"] - 1) - 2 * COST_BPS / 1e4
        res.append({"date": d["date"], "side": side, "r": r})
    return res


def stats(label, tr):
    n = len(tr)
    if n < 2:
        return f"| {label} | {n} | | | | | | |"
    rs = [t["r"] for t in tr]
    mean = sum(rs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (n - 1))
    t = mean / (sd / math.sqrt(n)) if sd else 0
    wins = sum(1 for x in rs if x > 0)
    eq = peak = dd = 0.0
    for x in rs:
        eq += x * NOTIONAL
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return (f"| {label} | {n} | {100 * wins / n:.0f}% | {mean * 1e4:+.2f} bp | {t:+.2f} "
            f"| ${sum(rs) * NOTIONAL:+,.0f} | ${dd:+,.0f} | {math.sqrt(252) * mean / sd if sd else 0:+.2f} |")


HEADER = ["| test | trades | win rate | avg per trade (after costs) | t-stat | P&L at $2,500 | max drawdown | Sharpe (ann.) |",
          "|---|---|---|---|---|---|---|---|"]


def main():
    end = engine.now_et().date() - timedelta(days=1)
    cal = replay.calendar(START - timedelta(days=10), end)
    first = datetime.combine(START - timedelta(days=10), datetime.min.time(), ET)
    last = datetime.combine(end + timedelta(days=1), datetime.min.time(), ET)
    bars, feed = replay.bars(engine.SYMBOLS, "30Min", first, last, adjustment="all")
    res = {s: trades(days_for(s, cal, bars[s])) for s in engine.SYMBOLS}
    spy = res["SPY"]
    out = [f"# Intraday momentum test ({feed.upper()} 30-min bars, {COST_BPS:g} bp/side)", "",
           "Pre-registered rule: sign of (prior close -> 10:00) return sets the direction of a close-30 -> close "
           "trade. No parameters. Primary = SPY, all history.", "", "## Results", ""] + HEADER
    out.append(stats(f"**PRIMARY: SPY {spy[0]['date']}..{spy[-1]['date']}**", spy))
    out.append(stats("SPY 2024-01 onward", [t for t in spy if t["date"] >= "2024-01-01"]))
    out.append(stats("SPY longs only", [t for t in spy if t["side"] > 0]))
    out.append(stats("SPY shorts only", [t for t in spy if t["side"] < 0]))
    q = res["QQQ"]
    out.append(stats(f"QQQ {q[0]['date']}..{q[-1]['date']}", q))
    out.append(stats("QQQ 2024-01 onward", [t for t in q if t["date"] >= "2024-01-01"]))
    out += ["", "## SPY by year", ""] + HEADER
    for y in sorted({t["date"][:4] for t in spy}):
        out.append(stats(y, [t for t in spy if t["date"].startswith(y)]))
    out += ["", "_|t| above ~2 is the usual bar for 'probably not luck'; with one pre-registered rule there is "
            "no multiple-testing discount._"]
    text = "\n".join(out)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
