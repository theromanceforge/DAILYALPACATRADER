#!/usr/bin/env python3
"""Trend-following research with walk-forward testing. Read-only: no orders.

    python3 research.py              # last 500 trading days (~2 years)
    python3 research.py --days 300

Walk-forward: the period is cut into TEST_DAYS blocks. For each block, a
candidate is picked using ONLY the days before it (best train P&L with at
least MIN_TRAIN_TRADES trades), then scored on the block. Stitching the
blocks together gives an out-of-sample record where every day was traded
with settings chosen without seeing it. The live rules are scored on the
same days for comparison.

Candidate family "ORB trend" (one position at a time, max 1 entry a day):
  - Opening range = first 30 minutes. From 10:00 until close-60, each
    10-minute cycle enters long if price is above the range high AND
    above VWAP (short: below the low AND below VWAP, if enabled).
  - Exit with a trailing stop (broker-side trailing_stop order live, so
    simulated minute by minute), or the close-30 flatten.
  - Trend-day filter (only trade days that look like trend days):
      none   no filter
      wideOR opening-range width >= 1.2x its 20-day average
      rvol   opening-range volume >= 1.2x its 20-day average
      gap    opening gap >= 0.3% in the trade's direction
      move   price already >= 0.4% from the open in the trade's direction
Same notional cap as live ($2,500). Costs: COST_BPS per side on every
fill, live rules included.
"""
from __future__ import annotations

import os
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import date, timedelta

import engine
import replay

ET = engine.ET
COST_BPS = 1.0
CYCLE = timedelta(minutes=10)
OR_MIN = 30
LOOKBACK = 20              # days for the wideOR / rvol averages
TEST_DAYS = 21             # ~1 month per walk-forward block
MIN_TRAIN_DAYS = 84        # ~4 months before the first test block
MIN_TRAIN_TRADES = 20
FILTERS = ("none", "wideOR", "rvol", "gap", "move")
TRAILS = (0.005, 0.008, 0.012)


def prep(sessions):
    # Per session/symbol: minute timestamps, VWAP prefix sums, opening
    # range, and trailing averages for the filters (prior days only).
    hist = defaultdict(list)
    for sess in sessions:
        sess["f"] = {}
        start = sess["open"] + timedelta(minutes=OR_MIN)
        for s in engine.SYMBOLS:
            m = sess["mins"][s]
            ts = [b["dt"] for b in m]
            pv, v, cpv, cv = [], [], 0.0, 0.0
            for b in m:
                cpv += (b["h"] + b["l"] + b["c"]) / 3 * b["v"]
                cv += b["v"]
                pv.append(cpv)
                v.append(cv)
            k = bisect_left(ts, start)
            if k == 0:
                sess["f"][s] = None
                continue
            hi, lo = max(b["h"] for b in m[:k]), min(b["l"] for b in m[:k])
            width, vol = (hi - lo) / m[0]["o"], v[k - 1]
            past = hist[s][-LOOKBACK:]
            full = len(past) == LOOKBACK
            sess["f"][s] = {
                "ts": ts, "pv": pv, "v": v, "hi": hi, "lo": lo, "open": m[0]["o"],
                "gap": (m[0]["o"] - sess["prev_c"][s]) / sess["prev_c"][s],
                "wide": width / (sum(p[0] for p in past) / LOOKBACK) if full else None,
                "rvol": vol / (sum(p[1] for p in past) / LOOKBACK) if full else None,
            }
            hist[s].append((width, vol))


_blackout = {}


def blackout(t):
    # engine.blackout re-reads events.json each call; cache per cycle time.
    if t not in _blackout:
        _blackout[t] = engine.blackout(t)
    return _blackout[t]


def passes(filt, f, d, last):
    if filt == "none":
        return True
    if filt == "wideOR":
        return f["wide"] is not None and f["wide"] >= 1.2
    if filt == "rvol":
        return f["rvol"] is not None and f["rvol"] >= 1.2
    if filt == "gap":
        return d * f["gap"] >= 0.003
    if filt == "move":
        return d * (last - f["open"]) / f["open"] >= 0.004
    raise ValueError(filt)


def trend(sess, cfg):
    # At most one trade a day, so this returns [] or [trade].
    trail, both, filt = cfg["trail"], cfg["both"], cfg["filter"]
    open_dt, close_dt = sess["open"], sess["close"]
    entry_end = close_dt - timedelta(minutes=engine.ENTRY_STOP_BEFORE_CLOSE_MIN)
    flat_at = close_dt - timedelta(minutes=engine.FLAT_BEFORE_CLOSE_MIN)
    mins, feats = sess["mins"], sess["f"]
    if not all(feats[s] for s in engine.SYMBOLS):
        return []
    pos, t = None, open_dt + timedelta(minutes=OR_MIN)
    while t <= close_dt:
        if pos:
            # Trailing stop, minute by minute up to this cycle.
            m, d = mins[pos["sym"]], pos["dir"]
            end = bisect_left(feats[pos["sym"]]["ts"], t)
            while pos["i"] < end:
                b = m[pos["i"]]
                stop = pos["best"] * (1 - d * trail)
                if (d > 0 and b["o"] <= stop) or (d < 0 and b["o"] >= stop):
                    return [dict(pos, exit=b["o"], why="trail stop", at=b["dt"])]
                if (d > 0 and b["l"] <= stop) or (d < 0 and b["h"] >= stop):
                    return [dict(pos, exit=stop, why="trail stop", at=b["dt"])]
                pos["best"] = max(pos["best"], b["h"]) if d > 0 else min(pos["best"], b["l"])
                pos["i"] += 1
            if t >= flat_at:
                i = min(pos["i"], len(m) - 1)
                return [dict(pos, exit=m[i]["o"], why="close flatten", at=t)]
        elif t > entry_end:
            return []
        elif not blackout(t):
            best = None
            for s in engine.SYMBOLS:
                f = feats[s]
                # Bars fully closed by t (bar start + 1 min <= t).
                k = bisect_right(f["ts"], t - timedelta(minutes=1))
                if k == 0:
                    continue
                last = mins[s][k - 1]["c"]
                vw = f["pv"][k - 1] / f["v"][k - 1] if f["v"][k - 1] else last
                if last > f["hi"] and last > vw:
                    d, strength = 1, (last - f["hi"]) / f["hi"]
                elif both and last < f["lo"] and last < vw:
                    d, strength = -1, (f["lo"] - last) / f["lo"]
                else:
                    continue
                if passes(filt, f, d, last) and (best is None or strength > best[2]):
                    best = (s, d, strength)
            if best:
                s, d, _ = best
                i = bisect_left(feats[s]["ts"], t)
                if i < len(mins[s]):
                    fill = mins[s][i]["o"]
                    qty = int(engine.MAX_NOTIONAL / fill)
                    if qty >= 1:
                        pos = {"sym": s, "dir": d, "qty": qty, "entry": fill, "best": fill, "in": t, "i": i}
        t += CYCLE
    if pos:
        return [dict(pos, exit=mins[pos["sym"]][-1]["c"], why="close flatten", at=close_dt)]
    return []


def baseline(sess):
    rules = dict(replay.rule_sets(sess))["NEW"]
    trades = replay.simulate("NEW", rules, sess["mins"], sess["prev_c"], sess["open"], sess["close"], [])
    for tr in trades:
        tr["dir"] = 1
    return trades


def pl(tr):
    gross = (tr["exit"] - tr["entry"]) * tr["qty"] * tr.get("dir", 1)
    return gross - (tr["entry"] + tr["exit"]) * tr["qty"] * COST_BPS / 1e4


def name(cfg):
    return f"{cfg['filter']} {'L/S' if cfg['both'] else 'long'} trail {cfg['trail'] * 100:.1f}%"


def grid():
    return [{"filter": f, "both": b, "trail": tr} for f in FILTERS for b in (False, True) for tr in TRAILS]


def summary(label, days):
    # days: list of (date, [trades])
    trades = [t for _, ts in days for t in ts]
    daily = [sum(map(pl, ts)) for _, ts in days]
    wins = [pl(t) for t in trades if pl(t) > 0]
    losses = [pl(t) for t in trades if pl(t) <= 0]
    eq = peak = dd = 0.0
    for x in daily:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    months = defaultdict(float)
    for dt, ts in days:
        months[dt[:7]] += sum(map(pl, ts))
    n = len(trades)
    shorts = sum(1 for t in trades if t.get("dir") == -1)
    return (f"| {label} | {n} ({shorts} short) | {100 * len(wins) / n if n else 0:.0f}% | ${sum(daily):+.2f} "
            f"| ${sum(wins) / len(wins) if wins else 0:+.2f} / ${sum(losses) / len(losses) if losses else 0:+.2f} "
            f"| ${dd:+.2f} | {sum(1 for v in months.values() if v > 0)}/{len(months)} |"), months


HEADER = ["| rules | trades | win rate | total P&L | avg win / avg loss | max drawdown | months up |",
          "|---|---|---|---|---|---|---|"]


def walk_forward(sessions, runs, base, cfgs):
    dates = [s["date"] for s in sessions]

    def total(key, lo, hi):
        ts = [t for day in runs[key][lo:hi] for t in day]
        return sum(map(pl, ts)), len(ts)

    folds, wf = [], {"ALL": [], **{f: [] for f in FILTERS}}
    start = MIN_TRAIN_DAYS
    while start < len(sessions):
        end = min(start + TEST_DAYS, len(sessions))
        for scope in wf:
            pool = [name(c) for c in cfgs if scope == "ALL" or c["filter"] == scope]
            scored = [(total(k, 0, start), k) for k in pool]
            ok = [(p, k) for (p, cnt), k in scored if cnt >= MIN_TRAIN_TRADES] or [(p, k) for (p, _), k in scored]
            train_pl, pick = max(ok)
            wf[scope] += [(dates[i], runs[pick][i]) for i in range(start, end)]
            if scope == "ALL":
                folds.append({"test": (dates[start], dates[end - 1]), "pick": pick, "train": train_pl,
                              "block": total(pick, start, end)[0],
                              "base": sum(pl(t) for day in base[start:end] for t in day)})
        start = end
    return folds, wf


def main():
    args = [a.strip() for a in sys.argv[1:] if a.strip()]
    n = int(args[1]) if args[:1] == ["--days"] else 500
    today = engine.now_et().date()
    cal = replay.calendar(today - timedelta(days=int(n * 1.6) + 10), today - timedelta(days=1))
    first, last = date.fromisoformat(cal[-n]["date"]), date.fromisoformat(cal[-1]["date"])
    sessions, feed = replay.load(first, last)
    sessions = [s for s in sessions if all(s["mins"][x] and s["prev_c"][x] for x in engine.SYMBOLS)]
    print("\n".join(report(sessions, feed)))


def report(sessions, feed):
    prep(sessions)
    dates = [s["date"] for s in sessions]
    cfgs = grid()
    runs = {name(c): [trend(s, c) for s in sessions] for c in cfgs}
    base = [baseline(s) for s in sessions]
    folds, wf = walk_forward(sessions, runs, base, cfgs)

    oos = list(range(MIN_TRAIN_DAYS, len(sessions)))
    base_oos = [(dates[i], base[i]) for i in oos]
    out = [f"# Trend research, walk-forward ({feed.upper()} bars, {COST_BPS:g} bp/side, "
           f"${engine.MAX_NOTIONAL:,.0f} notional)", "",
           f"{len(sessions)} days, {dates[0]} to {dates[-1]}. Out-of-sample record: {dates[oos[0]]} to "
           f"{dates[-1]} ({len(oos)} days, {len(folds)} blocks of ~{TEST_DAYS}). Each block is traded with the "
           f"candidate that did best on all days before it.", "",
           "## Out-of-sample (walk-forward)", ""] + HEADER
    line, m = summary("LIVE RULES", base_oos)
    out.append(line)
    monthly = {"LIVE RULES": m}
    for scope, days in wf.items():
        label = "WF pick, any filter" if scope == "ALL" else f"WF pick, filter={scope}"
        line, m = summary(label, days)
        out.append(line)
        monthly[label] = m

    out += ["", "## Blocks (pick from any filter)", "",
            "| test block | picked on prior days | train P&L | block P&L | live rules block P&L |",
            "|---|---|---|---|---|"]
    out += [f"| {f['test'][0]}..{f['test'][1]} | {f['pick']} | ${f['train']:+.0f} | ${f['block']:+.0f} "
            f"| ${f['base']:+.0f} |" for f in folds]

    keys = sorted({k for m in monthly.values() for k in m})
    out += ["", "## Out-of-sample monthly P&L", "", "| rules | " + " | ".join(k[2:] for k in keys) + " |",
            "|---|" + "---|" * len(keys)]
    for label, m in monthly.items():
        out.append(f"| {label} | " + " | ".join(f"{m.get(k, 0):+.0f}" for k in keys) + " |")

    out += ["", "## Reference: every candidate over the full period (in-sample, NOT a test)", ""] + HEADER
    out.append(summary("LIVE RULES", list(zip(dates, base)))[0])
    out += [summary(name(c), list(zip(dates, runs[name(c)])))[0] for c in cfgs]

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write("\n".join(out) + "\n")
    return out


if __name__ == "__main__":
    main()
