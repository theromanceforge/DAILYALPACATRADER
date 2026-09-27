#!/usr/bin/env python3
"""Alpaca PAPER desk. Actions calls run_session() from here.

run_session() keeps running cycles every 10 minutes until today's session
is over (after the close, or no session today), or until its time budget
runs out. So a single trigger at any time of day covers the rest of it,
including the close-30 flatten; GitHub's scheduled triggers alone proved
far too sparse (about two a day) to rely on for every cycle.

Run directly (self-hosted, under watchdog.py) it loops forever, one
cycle every 10 minutes. Each cycle writes heartbeat.txt, which
watchdog.py uses to detect a hung loop.
"""
import time
import traceback
from datetime import datetime

from engine import ALPACA, ET, api, cycle, hhmm, log, now_et

INTERVAL_SECONDS = 600


def safe_cycle():
    try:
        cycle()
    except Exception:
        log("cycle crashed " + traceback.format_exc().replace("\n", " | "))


def session_over(now) -> bool:
    # True once the market has no more regular trading today: after the
    # close, or a weekend/holiday. Pre-open today returns False so an
    # early trigger waits for the open.
    st, body = api(ALPACA, "/v2/clock")
    if st == 200 and isinstance(body, dict):
        try:
            if body["is_open"]:
                return False
            next_open = datetime.fromisoformat(body["next_open"]).astimezone(ET)
            return next_open.date() > now.date()
        except (KeyError, TypeError, ValueError):
            pass
    log(f"clock unavailable {st} {body}; using 16:05 ET as the end of session")
    return now.weekday() >= 5 or hhmm(now) >= "16:05"


def run_session(budget_minutes: float) -> bool:
    """Run cycles until the session is over or the budget is spent.

    Returns True if the budget ran out while the session is still going,
    i.e. the caller should start a fresh run to carry on.
    """
    start = time.monotonic()
    while True:
        began = time.monotonic()
        safe_cycle()
        if session_over(now_et()):
            log("session over; loop done")
            return False
        elapsed = (time.monotonic() - start) / 60
        wait = max(0.0, INTERVAL_SECONDS - (time.monotonic() - began))
        if elapsed + wait / 60 > budget_minutes:
            log(f"budget spent after {elapsed:.0f} min; handing over to a new run")
            return True
        time.sleep(wait)


if __name__ == "__main__":
    while True:
        safe_cycle()
        time.sleep(INTERVAL_SECONDS)
