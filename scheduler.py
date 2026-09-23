#!/usr/bin/env python3
"""Alpaca PAPER desk. Actions imports cycle() from here.

Run directly (self-hosted, under watchdog.py) it loops forever, one
cycle every 10 minutes, matching the Actions cadence. Each cycle
writes heartbeat.txt, which watchdog.py uses to detect a hung loop.
"""
import time
import traceback

from engine import cycle, hhmm, log

INTERVAL_SECONDS = 600

if __name__ == "__main__":
    while True:
        try:
            cycle()
        except Exception:
            log("cycle crashed " + traceback.format_exc().replace("\n", " | "))
        time.sleep(INTERVAL_SECONDS)
