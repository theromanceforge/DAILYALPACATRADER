#!/usr/bin/env python3
"""Held-out test of two rule tweaks suggested by the 2022-2026 replay.
Read-only: no orders.

    python3 tune.py 2016-01-04 2022-10-05

The 4-year replay (Oct 2022 - Oct 2026) found almost all of the gain in
QQQ trades and in entries at the first cycle (9:50). Both patterns were
picked after looking at that data, so they are tested here only on days
it never saw. Variants, pre-registered before this run:

  BASE   current engine.py rules
  QQQ    same, but only QQQ can be bought
  EARLY  same, but entries only at the 9:50 cycle (before 10:00)
  BOTH   QQQ only, 9:50 only

Pass bar (decided before the run): a variant must be positive after a
0.02% round-trip cost on the held-out days, with t >= 2 on per-trade
returns, and beat BASE.

Loads a quarter at a time to keep memory small. Prints one line per
trade (T ...) and a summary table.
"""
from __future__ import annotations

import math
import os
import sys
from datetime import date, timedelta

import engine
import replay

COST = 0.0002  # round trip


def variants(sess):
    o, c = sess["open"], sess["close"]
    flat = c - timedelta(minutes=engine.FLAT_BEFORE_CLOSE_MIN)
    full = c - timedelta(minutes=engine.ENTRY_STOP_BEFORE_CLOSE_MIN)
    early = o.replace(hour=9, minute=59)
    rules = lambda end: (engine.score, engine.SCORE_MIN, end, flat, True)
    return (("BASE", rules(full), False), ("QQQ", rules(full), True),
            ("EARLY", rules(early), False), ("BOTH", rules(early), True))


def chunks(first, last):
    a = first
    while a <= last:
        b = min(a + timedelta(days=91), last)
        yield a, b
        a = b + timedelta(days=1)


def main():
    first, last = date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2])
    rets = {}
    ndays = 0
    feeds = set()
    for a, b in chunks(first, last):
        try:
            sessions, feed = replay.load(a, b)
        except SystemExit as exc:
            print(f"skip {a}..{b}: {exc}", flush=True)
            continue
        feeds.add(feed)
        for sess in sessions:
            if not all(sess["mins"][s] and sess["prev_c"][s] for s in engine.SYMBOLS):
                print(f"missing {sess['date']}")
                continue
            ndays += 1
            for name, rules, qqq_only in variants(sess):
                mins = dict(sess["mins"])
                if qqq_only:
                    mins["SPY"] = []
                for tr in replay.simulate(name, rules, mins, sess["prev_c"], sess["open"], sess["close"], []):
                    r = tr["exit"] / tr["entry"] - 1
                    rets.setdefault(name, []).append((sess["date"], r))
                    print(f"T {name} {sess['date']} {tr['sym']} {tr['in']:%H:%M} {tr['at']:%H:%M} "
                          f"{tr['entry']:.4f} {tr['exit']:.4f} {tr['why'].split(' (')[0]}", flush=True)
    out = [f"# Held-out test {first} to {last} ({ndays} trading days, {'/'.join(sorted(feeds)).upper()} bars)", "",
           "Per-trade returns on the money in the trade; net = after 0.02% round-trip cost.", "",
           "| variant | trades | win rate | mean gross | mean net | t (net) | sum net | worst year net |",
           "|---|---|---|---|---|---|---|---|"]
    for name in ("BASE", "QQQ", "EARLY", "BOTH"):
        rs = [r for _, r in rets.get(name, [])]
        n = len(rs)
        if n < 2:
            out.append(f"| {name} | {n} | | | | | | |")
            continue
        net = [r - COST for r in rs]
        m, sd = sum(net) / n, math.sqrt(sum((x - sum(net) / n) ** 2 for x in net) / (n - 1))
        by_year = {}
        for d, r in rets[name]:
            by_year[d[:4]] = by_year.get(d[:4], 0) + r - COST
        out.append(f"| {name} | {n} | {100 * sum(r > 0 for r in rs) / n:.0f}% | {100 * sum(rs) / n:+.3f}% "
                   f"| {100 * m:+.3f}% | {m / (sd / math.sqrt(n)):+.2f} | {100 * sum(net):+.1f}% "
                   f"| {100 * min(by_year.values()):+.1f}% |")
    text = "\n".join(out)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
