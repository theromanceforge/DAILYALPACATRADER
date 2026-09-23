#!/usr/bin/env python3
"""Noise-area intraday momentum test (Zarattini, Aziz & Barbon 2024).
Read-only: no orders.

Pre-registered with the paper's own settings, nothing tuned:
  sigma(t)  = mean over the prior 14 days of |price(t) / open - 1| at the
              same time of day t
  bands     = upper max(open, prev close) * (1 + sigma(t))
              lower min(open, prev close) * (1 - sigma(t))
  checks    = every :00 and :30 from 10:00; decisions use the price at the
              check, orders fill at the next bar's open
  entries   = long above the upper band, short below the lower band
              (flips allowed, several trades a day possible)
  exits     = long: price below max(upper band, VWAP); short: above
              min(lower band, VWAP); everything closes at the close
  sizing    = paper's volatility targeting, shares ~ min(4, 2% / 14-day
              daily vol); scaled down so the 4x maximum is $2,500 notional,
              matching the live bot's cap
Primary test: SPY, 2024-05 onward (after the paper's sample ended).
Secondary: SPY 2016-01..2024-04 (overlaps the paper), full period, QQQ,
and a variant flattening at close-30 like the live bot. 30-minute bars
with Alpaca's per-bar VWAP; costs COST_BPS per side.
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
LOOKBACK = 14
TARGET_VOL = 0.02
MAX_LEV = 4.0
CAP = engine.MAX_NOTIONAL
START = date(2016, 1, 4)
PAPER_END = "2024-05-01"


def sessions_for(cal, bars):
    by_day = defaultdict(list)
    for b in bars:
        by_day[b["dt"].date().isoformat()].append(b)
    out = []
    for c in cal:
        o = datetime.fromisoformat(f"{c['date']}T{c['open']}").replace(tzinfo=ET)
        cl = datetime.fromisoformat(f"{c['date']}T{c['close']}").replace(tzinfo=ET)
        reg = sorted((b for b in by_day.get(c["date"], []) if o <= b["dt"] < cl), key=lambda b: b["dt"])
        if reg and reg[0]["dt"] == o and len(reg) == int((cl - o).total_seconds() // 1800):
            out.append({"date": c["date"], "bars": reg, "open": reg[0]["o"], "close": reg[-1]["c"]})
    return out


def run(sessions, flat_early=False):
    # Returns per-day records: {"date", "pl" ($), "trades", "gross"}.
    days, moves, closes = [], [], []  # moves: per past day, list of |move| at each half-hour slot
    for i, s in enumerate(sessions):
        bars = s["bars"]
        slots = [abs(b["c"] / s["open"] - 1) for b in bars]
        prev_close = closes[-1] if closes else None
        ready = len(moves) >= LOOKBACK and prev_close and len(closes) > LOOKBACK
        if ready:
            past = moves[-LOOKBACK:]
            rets = [closes[-k] / closes[-k - 1] - 1 for k in range(1, LOOKBACK + 1)]
            m = sum(rets) / LOOKBACK
            vol = math.sqrt(sum((r - m) ** 2 for r in rets) / (LOOKBACK - 1))
            lev = min(MAX_LEV, TARGET_VOL / vol) if vol else MAX_LEV
            qty = int(CAP * lev / MAX_LEV / s["open"])
            pos, entry, pl, gross, trades = 0, 0.0, 0.0, 0.0, 0
            cpv = cv = 0.0
            last_i = len(bars) - (2 if flat_early else 1)  # index of the last bar we may hold into
            for k, b in enumerate(bars[:-1]):
                cpv += b.get("vw", b["c"]) * b["v"]
                cv += b["v"]
                if k >= last_i:
                    continue  # checks run at the end of each bar from 10:00; none at/after the exit point
                # Same slot across past days (early-close days may be shorter).
                sig = [p[k] for p in past if len(p) > k]
                if len(sig) < LOOKBACK // 2:
                    continue
                sigma = sum(sig) / len(sig)
                up = max(s["open"], prev_close) * (1 + sigma)
                lo = min(s["open"], prev_close) * (1 - sigma)
                px, vwap = b["c"], cpv / cv if cv else b["c"]
                want = pos
                if pos > 0 and px < max(up, vwap):
                    want = 0
                elif pos < 0 and px > min(lo, vwap):
                    want = 0
                if want == 0:
                    want = 1 if px > up else -1 if px < lo else 0
                if want != pos and qty >= 1:
                    fill = bars[k + 1]["o"]
                    if pos:
                        g = pos * (fill - entry) * qty
                        gross += g
                        pl += g - (entry + fill) * qty * COST_BPS / 1e4
                    pos, entry = want, fill
                    trades += 1 if want else 0
            if pos:
                fill = bars[last_i]["c"] if not flat_early else bars[last_i + 1]["o"]
                g = pos * (fill - entry) * qty
                gross += g
                pl += g - (entry + fill) * qty * COST_BPS / 1e4
            days.append({"date": s["date"], "pl": pl, "gross": gross, "trades": trades})
        moves.append(slots)
        closes.append(s["close"])
    return days


def stats(label, days):
    n = len(days)
    if n < 2:
        return f"| {label} | {n} | | | | | | |"
    pls = [d["pl"] for d in days]
    mean = sum(pls) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in pls) / (n - 1))
    t = mean / (sd / math.sqrt(n)) if sd else 0
    eq = peak = dd = 0.0
    for x in pls:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    trades = sum(d["trades"] for d in days)
    return (f"| {label} | {n} | {trades} | ${sum(d['gross'] for d in days):+,.0f} | ${sum(pls):+,.0f} "
            f"| {t:+.2f} | ${dd:+,.0f} | {math.sqrt(252) * mean / sd if sd else 0:+.2f} |")


HEADER = ["| test | days | trades | P&L before costs | P&L after costs | t-stat (daily) | max drawdown | Sharpe (ann.) |",
          "|---|---|---|---|---|---|---|---|"]


def main():
    end = engine.now_et().date() - timedelta(days=1)
    cal = replay.calendar(START - timedelta(days=40), end)
    first = datetime.combine(START - timedelta(days=40), datetime.min.time(), ET)
    last = datetime.combine(end + timedelta(days=1), datetime.min.time(), ET)
    bars, feed = replay.bars(engine.SYMBOLS, "30Min", first, last, adjustment="all")
    res = {}
    for sym in engine.SYMBOLS:
        sess = sessions_for(cal, bars[sym])
        res[sym] = (run(sess), run(sess, flat_early=True))
    spy, spy_early = res["SPY"]
    after = [d for d in spy if d["date"] >= PAPER_END]
    before = [d for d in spy if d["date"] < PAPER_END]
    out = [f"# Noise-area momentum test ({feed.upper()} 30-min bars, {COST_BPS:g} bp/side, max ${CAP:,.0f} notional)",
           "", "Pre-registered with the paper's settings (14-day bands, :00/:30 checks, VWAP/band trailing stop, "
           "vol-targeted size). Primary = SPY after the paper's sample (2024-05 onward).", "",
           "## Results", ""] + HEADER
    out.append(stats(f"**PRIMARY: SPY {after[0]['date']}..{after[-1]['date']}**", after))
    out.append(stats(f"SPY {before[0]['date']}..2024-04 (overlaps paper)", before))
    out.append(stats("SPY full period", spy))
    out.append(stats("SPY 2024-05+, flatten at close-30 (live-bot timing)",
                     [d for d in spy_early if d["date"] >= PAPER_END]))
    q, _ = res["QQQ"]
    out.append(stats("QQQ 2024-05+", [d for d in q if d["date"] >= PAPER_END]))
    out.append(stats("QQQ full period", q))
    out += ["", "## SPY by year", ""] + HEADER
    for y in sorted({d["date"][:4] for d in spy}):
        out.append(stats(y, [d for d in spy if d["date"].startswith(y)]))
    text = "\n".join(out)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
