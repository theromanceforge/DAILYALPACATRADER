"""Daily Trendy Trader: one Alpaca paper cycle. Imported by scheduler.py."""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "desk.log"
HEARTBEAT_PATH = ROOT / "heartbeat.txt"
ALPACA = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"
SYMBOLS = ("SPY", "QQQ")
MAX_NOTIONAL = 2500.0
MAX_RISK = 400.0
# 75 needs BOTH a gap up and price in the upper part of today's range
# (50 + 15 + 15 = 80); either alone tops out at 70.
SCORE_MIN = 75
STOP_PCT = 0.006
TARGET_R = 2.0
# Timed off the real close from Alpaca's /v2/clock, so early-close
# days (13:00) and holidays are handled. Flattening 30 minutes early
# leaves 2-3 more cron runs if one is late or skipped (common on the
# free tier). Entries stop 30 minutes before that.
FLAT_BEFORE_CLOSE_MIN = 30
ENTRY_STOP_BEFORE_CLOSE_MIN = 60
ENTRY_START = "09:45"
MARKET_OPEN = "09:30"
MARKET_CLOSE = "16:00"
EVENTS_PATH = ROOT / "events.json"
TOY_MAX = 100.0
DAILY_HALT = -1500.0
WEEKLY_HALT = -3000.0
HALT_STATE_PATH = ROOT / "halt_state.json"


def now_et() -> datetime:
    return datetime.now(ET)


def hhmm(dt=None) -> str:
    return (dt or now_et()).strftime("%H:%M")


def log(msg: str) -> None:
    line = f"{now_et().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def load_halt_state() -> dict:
    # Sticky halt flags, keyed by calendar day and ISO week so a halt
    # holds even if equity recovers mid-session. Requires the workflow
    # to cache HALT_STATE_PATH between runs (actions/cache) -- on a
    # fresh/uncached checkout this file won't exist and both halts
    # start un-tripped, which is the safe default.
    try:
        return json.loads(HALT_STATE_PATH.read_text())
    except Exception:
        return {}


def save_halt_state(state: dict) -> None:
    try:
        HALT_STATE_PATH.write_text(json.dumps(state))
    except Exception as exc:
        log(f"halt-state write failed {exc}")


def week_key(dt) -> str:
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def keys():
    k = str(os.environ.get("APCA_API_KEY_ID") or "").strip()
    s = str(os.environ.get("APCA_API_SECRET_KEY") or "").strip()
    if k.startswith("PK") and s:
        return k, s
    return None


def api(host, path, payload=None, method=None):
    pair = keys()
    if not pair:
        return 0, "no paper keys"
    headers = {
        "APCA-API-KEY-ID": pair[0],
        "APCA-API-SECRET-KEY": pair[1],
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(host + path, data=body, headers=headers, method=method or ("POST" if body else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            raw = res.read().decode()
            return res.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw
    except Exception as exc:
        return 0, str(exc)


def flatten(symbol: str, why: str, market_open: bool) -> bool:
    # Bracket legs (take-profit/stop) reserve the position's shares, so
    # closing the position while they're open fails with "insufficient
    # qty available". Cancel this symbol's open orders first -- with
    # nested=false Alpaca lists the legs as their own entries -- then
    # close, retrying briefly since cancels settle asynchronously.
    # Every result is checked and logged instead of assumed.
    #
    # Outside regular hours, leave the position alone: cancelling the
    # GTC stop/target would strip its overnight protection, and the
    # close would only queue for the next open anyway.
    if not market_open:
        log(f"{why} {symbol} skipped, market closed; GTC stop/target stay on")
        return False
    st, orders = api(ALPACA, f"/v2/orders?status=open&symbols={symbol}&nested=false")
    if st == 200 and isinstance(orders, list):
        for o in orders:
            oid = o.get("id") if isinstance(o, dict) else None
            if oid:
                cst, cbody = api(ALPACA, f"/v2/orders/{oid}", method="DELETE")
                if not (200 <= cst < 300 or cst == 422):
                    log(f"{why} cancel {symbol} {oid} failed {cst} {cbody}")
    else:
        log(f"{why} order list {symbol} failed {st} {orders}")
    for attempt in range(3):
        st, body = api(ALPACA, f"/v2/positions/{symbol}", method="DELETE")
        if 200 <= st < 300:
            log(f"{why} {symbol}")
            return True
        if st == 404:
            log(f"{why} {symbol} already flat")
            return True
        time.sleep(2)
    log(f"{why} {symbol} FAILED {st} {body}")
    return False


def blackout(now):
    # Returns the event name if `now` falls inside an events.json
    # blackout window, else None. Only blocks new entries; open
    # positions keep their stop/target. A missing or broken file logs
    # and allows trading rather than silently halting the desk.
    try:
        cfg = json.loads(EVENTS_PATH.read_text())
    except Exception as exc:
        log(f"events.json unreadable {exc}; no blackout applied")
        return None
    before = timedelta(minutes=int(cfg.get("blackout_minutes_before", 60)))
    after = timedelta(minutes=int(cfg.get("blackout_minutes_after", 90)))
    latest = None
    for ev in cfg.get("events") or []:
        try:
            at = datetime.strptime(f"{ev['date']} {ev['time']}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
        except (KeyError, TypeError, ValueError):
            log(f"events.json bad entry {ev}")
            continue
        latest = at if latest is None or at > latest else latest
        if at - before <= now <= at + after:
            return ev.get("name") or "event"
    if latest is None or latest < now:
        log("events.json has no upcoming events; add the next quarter's dates")
    return None


def market_clock(now):
    # (is_open, close_dt) from Alpaca's clock, which knows holidays and
    # early closes. If the clock call fails, fall back to a plain
    # weekday 09:30-16:00 session so the close-flatten still runs.
    st, body = api(ALPACA, "/v2/clock")
    if st == 200 and isinstance(body, dict):
        try:
            close_dt = datetime.fromisoformat(body["next_close"]).astimezone(ET)
            return bool(body["is_open"]), close_dt
        except (KeyError, TypeError, ValueError):
            pass
    log(f"clock unavailable {st} {body}; assuming regular 09:30-16:00 session")
    close_dt = now.replace(hour=16, minute=0, second=0, microsecond=0)
    is_open = now.weekday() < 5 and MARKET_OPEN <= hhmm(now) < MARKET_CLOSE
    return is_open, close_dt


def entered_today(now):
    # True if any buy in SYMBOLS was placed today, asked of Alpaca
    # directly rather than the Actions cache (which can miss or race).
    # Limits the desk to one entry per day: a stopped-out day is done.
    # Returns None if Alpaca can't answer, so the caller fails closed.
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    query = urllib.parse.urlencode({
        "status": "all",
        "after": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "symbols": ",".join(SYMBOLS),
        "limit": 100,
    })
    st, body = api(ALPACA, f"/v2/orders?{query}")
    if st != 200 or not isinstance(body, list):
        log(f"order history unavailable {st} {body}")
        return None
    for o in body:
        if not isinstance(o, dict) or o.get("side") != "buy":
            continue
        # A buy that was rejected/canceled without any fill never
        # opened a position, so it doesn't use up the day's entry.
        filled = float(o.get("filled_qty") or 0)
        if filled > 0 or o.get("status") not in ("canceled", "rejected", "expired"):
            return True
    return False


def week_pl():
    # Weekly equity change via Alpaca's own portfolio history, rather
    # than reconstructing it from individual fills. ASSUMPTION (not
    # verified against Alpaca's docs at call time): period=1W aligns
    # to the current calendar week, not a rolling 7 days. If it's
    # rolling, this halt effectively measures "trailing week" instead
    # of "this trading week" -- close enough for a loss guardrail
    # either way, but worth confirming if the exact boundary matters.
    st, body = api(ALPACA, "/v2/account/portfolio/history?period=1W&timeframe=1D")
    if st != 200 or not isinstance(body, dict):
        return None
    equity = body.get("equity") or []
    equity = [e for e in equity if isinstance(e, (int, float)) and e]
    if len(equity) < 2:
        return None
    return float(equity[-1]) - float(equity[0])


def snapshot(symbol):
    st, body = api(DATA, f"/v2/stocks/{symbol}/snapshot")
    if st != 200 or not isinstance(body, dict):
        return None
    trade = body.get("latestTrade") or {}
    day = body.get("dailyBar") or {}
    prev = body.get("prevDailyBar") or {}
    last = float(trade.get("p") or day.get("c") or 0)
    if last <= 0:
        return None
    prev_c = float(prev.get("c") or last)
    open_p = float(day.get("o") or last)
    high = float(day.get("h") or last)
    low = float(day.get("l") or last)
    span = max(high - low, 0.01)
    # FIX 4: gap is the actual opening gap (open vs prior close), not
    # live price vs prior close. The old version drifted with intraday
    # movement and duplicated what `loc` already measures.
    gap = (open_p - prev_c) / prev_c if prev_c else 0.0
    return {"symbol": symbol, "last": last, "loc": (last - low) / span, "gap": gap, "range_pct": span / last}


def score(q):
    # Long-only trend filter. The old version passed SCORE_MIN on almost
    # any day (a gap up alone scored 66, a 0.8% range alone 58) and
    # rewarded price near the day's LOW, i.e. buying into weakness.
    # Now a buy needs a gap up AND price holding the upper part of the
    # range; weakness is penalized, and range only adds a small bonus.
    n = 50
    if q["gap"] < -0.012:
        n -= 20
    elif q["gap"] > 0.001:
        n += 15
    if q["loc"] >= 0.6:
        n += 15
    elif q["loc"] <= 0.34:
        n -= 15
    if q["range_pct"] > 0.008:
        n += 5
    return max(0, min(100, n))


def cycle() -> None:
    # FIX 7: hard-stop if the environment ever claims a non-paper base
    # URL. ALPACA is hardcoded to the paper endpoint above, but nothing
    # previously verified that at runtime -- a bad edit to that
    # constant would have gone live with no independent check. This is
    # inert on Actions today (APCA_API_BASE_URL isn't set there), and
    # only bites if someone sets it to something other than paper.
    env_base = os.environ.get("APCA_API_BASE_URL", "").strip()
    if env_base and env_base.rstrip("/") != ALPACA.rstrip("/"):
        log(f"refusing non-paper base url {env_base!r}")
        return

    clock = hhmm()
    HEARTBEAT_PATH.write_text(now_et().isoformat(timespec="seconds"))
    st, acct = api(ALPACA, "/v2/account")
    if st != 200:
        log(f"alpaca down {st} {acct}")
        return
    now = now_et()
    is_open, close_dt = market_clock(now)
    st, pos = api(ALPACA, "/v2/positions")
    # FIX 1: check the positions call status explicitly. Previously a
    # failed call silently became an empty list via the isinstance
    # filter, which the cycle read as "zero open positions" — risking
    # a second entry on top of a real, unseen one ("no double beta").
    if st != 200:
        log(f"alpaca positions down {st} {pos}")
        return
    held = [p for p in (pos or []) if isinstance(pos, list) and p.get("symbol") in SYMBOLS]

    # FIX 5/6: sticky daily (-$1,500) and weekly (-$3,000) loss halts.
    # Sticky = once tripped, stays tripped for the rest of the day/week
    # even if equity recovers, via halt_state.json. That file only
    # survives between Actions runs if the workflow caches it
    # (actions/cache keyed on today's date / this ISO week) -- without
    # that step this degrades to "checked fresh each run," which still
    # catches a halt condition present *at run time* but won't hold
    # once equity ticks back above the line.
    today = now_et().strftime("%Y-%m-%d")
    wk = week_key(now_et())
    state = load_halt_state()
    day_halted = state.get("day") == today and state.get("day_halted")
    week_halted = state.get("week") == wk and state.get("week_halted")

    if not day_halted:
        try:
            day_pl = float(acct.get("equity")) - float(acct.get("last_equity"))
        except (TypeError, ValueError):
            day_pl = None
        if day_pl is not None and day_pl <= DAILY_HALT:
            day_halted = True
            state["day"], state["day_halted"] = today, True
            log(f"daily-halt tripped {day_pl:.2f}")

    if not week_halted:
        wk_pl = week_pl()
        if wk_pl is not None and wk_pl <= WEEKLY_HALT:
            week_halted = True
            state["week"], state["week_halted"] = wk, True
            log(f"weekly-halt tripped {wk_pl:.2f}")

    if day_halted or week_halted:
        save_halt_state(state)
        for p in held:
            flatten(p["symbol"], "halt flatten", is_open)
        log(f"halted day={day_halted} week={week_halted} seats {len(held)}")
        return

    # Always persist state, even on a normal (non-halted) cycle, so the
    # cache file exists for the workflow's "save halt state" step to
    # find. Without this, halt_state.json only ever gets created on the
    # (hopefully rare) day a halt actually trips, and the cache-save
    # step fails with nothing to save on every ordinary run.
    state["day"], state["week"] = today, wk
    save_halt_state(state)
    for p in list(held):
        mv = abs(float(p.get("market_value") or 0))
        if mv < TOY_MAX:
            if flatten(p["symbol"], f"flatten-toy ${mv:.2f}", is_open):
                held = [x for x in held if x.get("symbol") != p.get("symbol")]
    if is_open and now >= close_dt - timedelta(minutes=FLAT_BEFORE_CLOSE_MIN):
        for p in held:
            flatten(p["symbol"], "flatten", is_open)
        return
    entry_end = close_dt - timedelta(minutes=ENTRY_STOP_BEFORE_CLOSE_MIN)
    if not is_open or clock < ENTRY_START or now > entry_end:
        log(f"outside {clock} open={is_open} close={close_dt:%H:%M} seats {len(held)}")
        return
    if held:
        log(f"hold {[p.get('symbol') for p in held]}")
        return
    done = entered_today(now)
    if done is None:
        log("skip entry, can't confirm today's order history")
        return
    if done:
        log("already entered today, no re-entry")
        return
    ev = blackout(now_et())
    if ev:
        log(f"blackout {ev} {clock}")
        return
    cards = []
    for sym in SYMBOLS:
        q = snapshot(sym)
        if q:
            q["score"] = score(q)
            cards.append(q)
    cards.sort(key=lambda x: x["score"], reverse=True)
    log("scores " + " ".join(f"{c['symbol']}:{c['score']}" for c in cards))
    for q in cards:
        if q["score"] < SCORE_MIN:
            continue
        last = q["last"]
        stop = round(last * (1 - STOP_PCT), 2)
        target = round(last + TARGET_R * (last - stop), 2)
        # Alpaca rejects fractional qty on bracket orders (422), so size
        # in whole shares, rounding down to stay inside both caps.
        qty = int(min(MAX_NOTIONAL / last, MAX_RISK / max(last - stop, 0.01)))
        if qty < 1:
            continue
        st, body = api(ALPACA, "/v2/orders", {
            "symbol": q["symbol"],
            "qty": str(qty),
            "side": "buy",
            "type": "market",
            # GTC so the stop/target legs survive past the close if every
            # flatten run misses; flatten() cancels them before closing.
            "time_in_force": "gtc",
            "order_class": "bracket",
            "take_profit": {"limit_price": str(target)},
            "stop_loss": {"stop_price": str(stop)},
        })
        oid = body.get("id") if isinstance(body, dict) else body
        log(f"order {q['symbol']} {st} {oid}")
        # FIX 3: only stop trying candidates once an order actually
        # succeeds. A failed POST used to `break` unconditionally,
        # burning the whole cycle instead of falling back to the
        # next-ranked symbol.
        if 200 <= st < 300:
            break
