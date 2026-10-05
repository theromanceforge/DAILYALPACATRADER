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
# With stay_on, wake this long before the open so the first cycle is ready.
PRE_OPEN_SECONDS = 600
# Cycles land this many seconds after each :00/:10/:20... mark, so a
# cycle falls just after 15:30 and the flatten isn't up to 10 min late.
GRID_OFFSET_SECONDS = 5


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


def until_next_mark(now) -> float:
    # Seconds until the next 10-minute clock mark (plus the offset).
    into = (now.minute % 10) * 60 + now.second + now.microsecond / 1e6
    wait = (GRID_OFFSET_SECONDS - into) % INTERVAL_SECONDS
    return wait if wait >= 1 else wait + INTERVAL_SECONDS


def wait_for_open(now):
    # Seconds to sleep so the next cycle lands PRE_OPEN_SECONDS before
    # Alpaca's next regular open; 0 while the market is open or the open
    # is close; None if the clock can't be read.
    st, body = api(ALPACA, "/v2/clock")
    if st == 200 and isinstance(body, dict):
        try:
            if body["is_open"]:
                return 0.0
            next_open = datetime.fromisoformat(body["next_open"]).astimezone(ET)
            return max(0.0, (next_open - now).total_seconds() - PRE_OPEN_SECONDS)
        except (KeyError, TypeError, ValueError):
            pass
    return None


def run_session(budget_minutes: float, stay_on: bool = False) -> bool:
    """Run cycles until the session is over or the budget is spent.

    With stay_on, the run doesn't stop at the close: it sleeps until
    shortly before the next open and carries on, handing over to a new
    run each time the budget runs out. One trigger then keeps the bot
    running across nights and weekends, so it no longer needs an
    on-time trigger each morning.

    Returns True if the budget ran out and the caller should start a
    fresh run to carry on.
    """
    start = time.monotonic()
    while True:
        safe_cycle()
        if not stay_on:
            if session_over(now_et()):
                log("session over; loop done")
                return False
            wait = until_next_mark(now_et())
        else:
            idle = wait_for_open(now_et())
            if idle:
                log(f"market closed; next cycle in {idle / 60:.0f} min, before the next open")
                wait = idle
            else:
                # Open, opening soon, or clock unreadable: normal cadence,
                # on the 10-minute clock marks.
                wait = until_next_mark(now_et())
        left = budget_minutes * 60 - (time.monotonic() - start)
        if wait > left:
            # Sleep out this run's budget first, so an overnight wait
            # takes a few long runs rather than a burst of short ones.
            time.sleep(max(0.0, left))
            log(f"budget spent after {budget_minutes:.0f} min; handing over to a new run")
            return True
        time.sleep(wait)


if __name__ == "__main__":
    while True:
        safe_cycle()
        time.sleep(INTERVAL_SECONDS)
