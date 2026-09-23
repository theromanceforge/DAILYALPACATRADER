#!/usr/bin/env python3
"""QQQ noise-area forward test, SHADOW ONLY: logs what the strategy would
have traded. Places no orders.

Pre-registered 2026-09-23, before any of its data existed:
  - Rules: noise.py exactly as tested (Zarattini, Aziz & Barbon 2024
    settings), symbol QQQ, close at the close, 1 bp/side costs, max $2,500.
  - Record starts on the first trading day after registration (START).
  - Review at ~3 months (63 days) and ~6 months (126 days). Move to real
    paper orders only if the 6-month record is positive after costs with a
    daily t-stat of at least 1.5.
Each day is recomputed from Alpaca's 30-minute bars after the close, so a
missed run loses nothing. Used by report.py.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta

import engine
import noise
import replay

ET = engine.ET
SYMBOL = "QQQ"
START = "2026-09-24"
WARMUP_DAYS = 40  # calendar days of history for the 14-day bands and vol


def record(until: date):
    # Shadow results for every trading day START..until.
    first = date.fromisoformat(START) - timedelta(days=WARMUP_DAYS)
    cal = replay.calendar(first, until)
    lo = datetime.combine(first, datetime.min.time(), ET)
    hi = datetime.combine(until + timedelta(days=1), datetime.min.time(), ET)
    bars, _ = replay.bars([SYMBOL], "30Min", lo, hi, adjustment="all")
    days = noise.run(noise.sessions_for(cal, bars[SYMBOL]))
    return [d for d in days if START <= d["date"] <= until.isoformat()]


def section(day: date) -> list[str]:
    if day.isoformat() < START:
        return ["", f"**QQQ shadow test:** starts {START} (no orders; logging only)."]
    days = record(day)
    today = [d for d in days if d["date"] == day.isoformat()]
    lines = ["", f"**QQQ noise-area shadow test** (no orders placed; since {START}):"]
    if not today:
        lines.append("- today: no data (not in the record)")
    elif not today[0]["log"]:
        lines.append("- today: no trade (price stayed inside the noise area)")
    else:
        for t in today[0]["log"]:
            side = "long" if t["dir"] > 0 else "short"
            lines.append(f"- today: {side} {t['qty']} {SYMBOL} {t['in']:%H:%M} @ {t['entry']:.2f} -> "
                         f"{t['out']:%H:%M} @ {t['exit']:.2f}: {noise_money(t['pl'])}")
    pls = [d["pl"] for d in days]
    n = len(pls)
    t_stat = None
    if n > 1:
        mean = sum(pls) / n
        sd = (sum((x - mean) ** 2 for x in pls) / (n - 1)) ** 0.5
        t_stat = mean / (sd / n ** 0.5) if sd else None
    lines.append(f"- record: {n} days, {sum(d['trades'] for d in days)} trades, {noise_money(sum(pls))} after costs"
                 + (f", t = {t_stat:+.2f}" if t_stat is not None else "")
                 + " (reviews at 63 and 126 days; bar: positive with t >= 1.5 at 126)")
    return lines


def noise_money(x):
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    d = date.fromisoformat(arg) if arg else engine.now_et().date()
    print("\n".join(section(d)))
