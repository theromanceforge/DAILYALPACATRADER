#!/usr/bin/env python3
"""Trend-following research: replays candidate rules against the live
baseline over past sessions. Read-only: no orders.

    python3 research.py            # last 168 trading days (~8 months)
    python3 research.py --days 63

The period is split: the first part is IN-SAMPLE (compare and pick here),
the last HOLDOUT_DAYS are OUT-OF-SAMPLE and stay hidden unless
REVEAL_OOS=1, so a candidate is chosen before it's checked on months it
wasn't tuned on.

Candidate family "ORB trend" (long and/or short):
  - Opening range = first OR_MIN minutes. From then until close-60, each
    10-minute cycle enters long if price is above the range high AND
    above VWAP, short if below the range low AND below VWAP.
  - Exit with a trailing stop (a broker-side trailing_stop order live,
    so it's simulated minute by minute), or the close-30 flatten.
  - One position at a time, up to MAX_TRADES entries a day.
Same notional cap as live ($2,500) so P&L is comparable with the
baseline. Costs: COST_BPS per side on every fill, baseline included.
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import timedelta

import engine
import replay

ET = engine.ET
HOLDOUT_DAYS = 42          # ~2 months kept out of sample
COST_BPS = 1.0             # per side (spread + slippage), applied to all rules
CYCLE = timedelta(minutes=10)


def vwap(seen):
    pv = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in seen)
    v = sum(b["v"] for b in seen)
    return pv / v if v else seen[-1]["c"]


def signal(sess, s, t):
    # (last price, VWAP) as seen at cycle t; same for every candidate, so cached.
    cache = sess.setdefault("_sig", {})
    if (s, t) not in cache:
        seen = [b for b in sess["mins"][s] if b["dt"] + timedelta(minutes=1) <= t]
        cache[(s, t)] = (seen[-1]["c"], vwap(seen)) if seen else None
    return cache[(s, t)]


def trend(sess, or_min, trail, both_sides, max_trades):
    open_dt, close_dt = sess["open"], sess["close"]
    entry_start = open_dt + timedelta(minutes=or_min)
    entry_end = close_dt - timedelta(minutes=engine.ENTRY_STOP_BEFORE_CLOSE_MIN)
    flat_at = close_dt - timedelta(minutes=engine.FLAT_BEFORE_CLOSE_MIN)
    mins = sess["mins"]
    orng = {}
    for s in engine.SYMBOLS:
        first = [b for b in mins[s] if b["dt"] < entry_start]
        if not first:
            return []
        orng[s] = (max(b["h"] for b in first), min(b["l"] for b in first))
    trades, pos, n = [], None, 0
    t = entry_start
    while t <= close_dt:
        if pos:
            # Trailing stop, minute by minute between cycles.
            for b in mins[pos["sym"]]:
                if not (pos["from"] <= b["dt"] < t):
                    continue
                d = pos["dir"]
                stop = pos["best"] * (1 - d * trail)
                if (d > 0 and b["o"] <= stop) or (d < 0 and b["o"] >= stop):
                    pos.update(exit=b["o"], why="trail stop", at=b["dt"])
                elif (d > 0 and b["l"] <= stop) or (d < 0 and b["h"] >= stop):
                    pos.update(exit=stop, why="trail stop", at=b["dt"])
                if "exit" in pos:
                    break
                pos["best"] = max(pos["best"], b["h"]) if d > 0 else min(pos["best"], b["l"])
            if "exit" in pos:
                trades.append(pos)
                pos = None
            else:
                pos["from"] = t
        if t >= flat_at:
            if pos:
                nxt = [b for b in mins[pos["sym"]] if b["dt"] >= t]
                pos.update(exit=nxt[0]["o"] if nxt else mins[pos["sym"]][-1]["c"], why="close flatten", at=t)
                trades.append(pos)
                pos = None
            break
        if not pos and n < max_trades and t <= entry_end and not engine.blackout(t):
            best = None
            for s in engine.SYMBOLS:
                sig = signal(sess, s, t)
                if not sig:
                    continue
                last, vw = sig
                hi, lo = orng[s]
                if last > hi and last > vw:
                    d, strength = 1, (last - hi) / hi
                elif both_sides and last < lo and last < vw:
                    d, strength = -1, (lo - last) / lo
                else:
                    continue
                if best is None or strength > best[2]:
                    best = (s, d, strength)
            if best:
                s, d, _ = best
                nxt = [b for b in mins[s] if b["dt"] >= t]
                if nxt:
                    fill = nxt[0]["o"]
                    qty = int(engine.MAX_NOTIONAL / fill)
                    if qty >= 1:
                        pos = {"sym": s, "dir": d, "qty": qty, "entry": fill, "best": fill,
                               "in": t, "from": t}
                        n += 1
        t += CYCLE
    if pos:
        pos.update(exit=mins[pos["sym"]][-1]["c"], why="close flatten", at=close_dt)
        trades.append(pos)
    return trades


def baseline(sess):
    rules = dict(replay.rule_sets(sess))["NEW"]
    trades = replay.simulate("NEW", rules, sess["mins"], sess["prev_c"], sess["open"], sess["close"], [])
    for tr in trades:
        tr["dir"] = 1
    return trades


def pl(tr):
    gross = (tr["exit"] - tr["entry"]) * tr["qty"] * tr.get("dir", 1)
    cost = (tr["entry"] + tr["exit"]) * tr["qty"] * COST_BPS / 1e4
    return gross - cost


def row(name, days):
    trades = [tr for d in days for tr in d["trades"]]
    daily = [sum(map(pl, d["trades"])) for d in days]
    wins = [pl(t) for t in trades if pl(t) > 0]
    losses = [pl(t) for t in trades if pl(t) <= 0]
    eq = peak = dd = 0.0
    for x in daily:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    n = len(trades)
    shorts = sum(1 for t in trades if t.get("dir") == -1)
    months = defaultdict(float)
    for d in days:
        months[d["date"][:7]] += sum(map(pl, d["trades"]))
    up = sum(1 for v in months.values() if v > 0)
    avg_w = sum(wins) / len(wins) if wins else 0
    avg_l = sum(losses) / len(losses) if losses else 0
    return (f"| {name} | {n} ({shorts} short) | {100 * len(wins) / n if n else 0:.0f}% "
            f"| ${sum(daily):+.2f} | ${avg_w:+.2f} / ${avg_l:+.2f} | ${dd:+.2f} "
            f"| {up}/{len(months)} |"), sum(daily), months


def table(title, sessions, candidates):
    out = [f"## {title}: {sessions[0]['date']} to {sessions[-1]['date']} ({len(sessions)} days)", "",
           "| rules | trades | win rate | total P&L | avg win / avg loss | max drawdown | months up |",
           "|---|---|---|---|---|---|---|"]
    monthly = {}
    for name, fn in candidates:
        days = [{"date": s["date"], "trades": fn(s)} for s in sessions]
        line, _, months = row(name, days)
        out.append(line)
        monthly[name] = months
    keys = sorted({m for v in monthly.values() for m in v})
    out += ["", "Monthly P&L:", "", "| rules | " + " | ".join(keys) + " |", "|---|" + "---|" * len(keys)]
    for name, months in monthly.items():
        out.append(f"| {name} | " + " | ".join(f"{months.get(k, 0):+.0f}" for k in keys) + " |")
    return out


def candidates():
    out = [("BASELINE (live rules)", baseline)]
    for both in (False, True):
        for trail in (0.003, 0.005, 0.008):
            for mt in (1, 3):
                name = f"ORB30 {'L/S' if both else 'long'} trail {trail * 100:.1f}% max {mt}/day"
                out.append((name, lambda s, tr=trail, b=both, m=mt: trend(s, 30, tr, b, m)))
    # Round 2: the best round-1 results sat at the widest trail, so
    # extend that edge for long/short (in-sample only).
    for trail in (0.010, 0.012, 0.015):
        for mt in (1, 3):
            name = f"ORB30 L/S trail {trail * 100:.1f}% max {mt}/day"
            out.append((name, lambda s, tr=trail, m=mt: trend(s, 30, tr, True, m)))
    return out


def main():
    args = [a.strip() for a in sys.argv[1:] if a.strip()]
    n = int(args[1]) if args[:1] == ["--days"] else 168
    today = engine.now_et().date()
    cal = replay.calendar(today - timedelta(days=int(n * 1.6) + 10), today - timedelta(days=1))
    from datetime import date
    first, last = date.fromisoformat(cal[-n]["date"]), date.fromisoformat(cal[-1]["date"])
    sessions, feed = replay.load(first, last)
    sessions = [s for s in sessions if all(s["mins"][x] and s["prev_c"][x] for x in engine.SYMBOLS)]
    ins, oos = sessions[:-HOLDOUT_DAYS], sessions[-HOLDOUT_DAYS:]
    cands = candidates()
    out = [f"# Trend research ({feed.upper()} bars, {COST_BPS:g} bp/side costs, ${engine.MAX_NOTIONAL:,.0f} notional)", ""]
    out += table("IN-SAMPLE", ins, cands)
    if os.environ.get("REVEAL_OOS") == "1":
        picks = [c for c in cands if c[0] in os.environ.get("OOS_PICKS", "").split(";")] or cands
        out += [""] + table("OUT-OF-SAMPLE (holdout)", oos, [cands[0]] + [c for c in picks if c is not cands[0]])
    else:
        out += ["", f"_Out-of-sample: last {len(oos)} days held out ({oos[0]['date']} to {oos[-1]['date']})._"]
    text = "\n".join(out)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
