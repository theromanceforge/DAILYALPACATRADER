#!/usr/bin/env python3
"""Restart scheduler.py if the heartbeat is stale. Paper only."""
from __future__ import annotations

import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
HEARTBEAT = ROOT / "heartbeat.txt"
LOG = ROOT / "desk.log"
SCHEDULER = ROOT / "scheduler.py"


def alive() -> bool:
    try:
        raw = HEARTBEAT.read_text().strip()
        last = datetime.fromisoformat(raw)
        if last.tzinfo is None:
            last = last.replace(tzinfo=ET)
        return datetime.now(ET) - last < timedelta(minutes=15)
    except Exception:
        return False


def running() -> bool:
    try:
        out = subprocess.check_output(["pgrep", "-f", str(SCHEDULER)], text=True)
        return bool(out.strip())
    except subprocess.CalledProcessError:
        return False


def stop() -> None:
    # A process that's running but not heartbeating is hung; kill it
    # before starting a fresh one so two loops never trade at once.
    subprocess.run(["pkill", "-f", str(SCHEDULER)], check=False)
    time.sleep(5)


def start() -> None:
    with LOG.open("a") as log:
        log.write(f"{datetime.now(ET).isoformat(timespec='seconds')} watchdog start scheduler\n")
        subprocess.Popen(
            ["python3", str(SCHEDULER)],
            cwd=str(ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


if __name__ == "__main__":
    while True:
        if not running():
            start()
        elif not alive():
            stop()
            start()
        time.sleep(120)
