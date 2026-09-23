# Daily Trendy Trader

Intraday day-trading bot for SPY/QQQ on an **Alpaca PAPER** account
(repo: DAILYALPACATRADER). A research sandbox, not live cash.

It started as the intraday desk in
[ALBOT](https://github.com/theromanceforge/ALBOT), which now runs a daily
SPY trend allocator instead. This repo carries the day-trading version on
separately so it can be developed and tested without touching that account.

## What it does (`engine.py`)

GitHub Actions runs one cycle every 10 minutes on weekdays (`daytrader`
workflow). Times come from Alpaca's market clock, so holidays and 1:00 PM
early closes are handled.

- **Entries:** 09:45 to close − 60 min. Buys only when a symbol gapped up
  **and** is trading in the upper part of today's range (score ≥ 75).
- **At most one entry per day**, one position at a time, sized to whole
  shares within a $2,500 notional cap.
- **Exits:** bracket order with a 0.6% stop and a 1.2% target (GTC legs), and
  everything is flattened at close − 30 min (3:30 PM normally).
- **No new entries** inside the `events.json` blackout windows (FOMC, jobs,
  CPI). Add new dates when the log says the list has run out.
- **Loss halts:** stops trading for the day at −$100 and for the week at
  −$250. A normal stop-out is about −$15, so these only trip if something
  is badly wrong (a gap through the stop, a runaway order).
- **Paper only:** refuses any key that doesn't start with `PK` and any
  non-paper base URL.

## Honest baseline

Replaying these exact rules over 8 months (Jan–Sep 2026, 168 trading days)
gave **84 trades, 42 wins / 42 losses, +$12.72 total**. That's roughly
break-even. The target hit only 3 times; most trades ended at the 3:30 sell.
Treat this as a starting point for testing ideas, not a money-maker.

## Test ideas before running them (`replay.py`)

Actions → **replay** → Run workflow:

- `date`: one day, with cycle-by-cycle scores and trades
- `date` + `end`: a range, with summary stats and a per-day table
- `days`: the last N trading days (e.g. `63` ≈ 3 months, `168` ≈ 8 months)

Read-only: it places no orders. It compares the current rules with the older
ones. Change `engine.py` on a branch, replay it, and only merge changes that
hold up on months they weren't tuned on.

## How we work (the playbook)

1. **The live paper bot is the baseline.** It keeps running the current
   rules unchanged while ideas are researched, so live results can be
   compared with what the replay predicted.
2. **Every idea goes through the same steps:**
   1. Build it on a branch. Nothing live changes.
   2. Replay it over the 8-month history (`days=168`).
   3. Check it on months it wasn't tuned on (hold out the most recent
      ~2 months until the end).
   4. Score it against the current rules: net P&L, trades, win rate,
      average win / average loss, worst drawdown.
   5. **Promote only if** it beats the baseline in-sample *and* on the
      held-out months, with roughly 50+ trades. Then PR, and merge only
      when the owner says so.
3. **Research direction: trend-following.** Ride strong intraday moves,
   possibly both long and short, possibly more than one trade a day.
   The current gap-up rule stays live until something beats it.
4. **Reporting:**
   - **Daily:** the `report` workflow comments on the `daily-report`
     issue after each close (trades, day P&L, equity, total since start).
     Watch that issue to get it as a notification.
   - **Weekly (Fridays after the close):** live week vs. replay of the
     same days, plus research progress.
5. **Infrastructure follows edge.** 10-minute GitHub Actions cycles are
   fine for research. Real-time data (websocket) and an always-on host
   come only once a strategy shows an edge that needs them.
6. **Real money is out of scope** unless a strategy beats the SPY
   allocator (ALBOT) on paper, after costs, for several months. (The old
   pattern-day-trader rule and its $25k minimum were retired on 2026-06-04;
   Alpaca now uses its Intraday Margin framework. Margin and short selling
   still need at least $2,000 of equity.)

## Setup

1. In Alpaca, create a **separate paper account** for this bot (Alpaca
   allows several) and generate its API keys. **Don't reuse ALBOT's paper
   keys:** this bot sells SPY every afternoon, which would undo the allocator.
2. In this repo: Settings → Secrets and variables → Actions:
   - `APCA_API_KEY_ID` (must start with `PK`)
   - `APCA_API_SECRET_KEY`
3. Actions → **daytrader** → Run workflow to test. A healthy run logs a line
   like `outside …` or `scores …`. `no paper keys` means the secrets are
   missing.

The schedule runs every 10 minutes from about 7 AM to 5:50 PM ET on weekdays
(it covers both daylight and standard time). Runs outside market hours do
nothing.

Do not commit keys.

### External timer (why the bot doesn't rely on GitHub's schedule alone)

GitHub's `schedule` trigger never fired for this repo after setup (zero
scheduled runs, even after moving the cron off the busy :00 slots). So an
outside timer (cron-job.org) starts the workflows through GitHub's API:

- `daytrader`: every 10 minutes, Mon–Fri 12:00–21:59 UTC, `POST
  /repos/theromanceforge/DAILYALPACATRADER/actions/workflows/daytrader.yml/dispatches`
  with body `{"ref":"main"}`.
- `report`: Mon–Fri 21:37 UTC, same for `report.yml` with
  `{"ref":"main","inputs":{"date":""}}`.
- Auth: a fine-grained GitHub token limited to this repo with **Actions:
  read and write** only, stored only in cron-job.org. Renew it before it
  expires; if it lapses the jobs return 401 and the bot stops running.

The GitHub cron stays as a backup. Running both is safe: the `daytrader`
concurrency group runs one cycle at a time, and `entered_today()` checks
Alpaca's order history, so a second cycle can't make a second entry. To
turn the timer off, pause the jobs in cron-job.org.

## Read-only Alpaca access in Claude chats (optional)

`.mcp.json` starts Alpaca's official MCP server (`scripts/alpaca-mcp.sh`) so
Claude can look at this bot's paper account (account, positions, orders,
history, quotes, bars, news) directly in chat. It is not used by the bot.

- Needs this bot's **paper** keys as environment variables
  `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` in the Claude environment, and
  network access to `paper-api.alpaca.markets` and `data.alpaca.markets`.
- The launcher refuses non-paper keys and pins the paper endpoint.
- Every tool that can place, change or cancel orders, close positions,
  exercise options or change account settings is denied in
  `.claude/settings.json`. Never give it ALBOT's keys.

## Files

| file | purpose |
|---|---|
| `engine.py` | one trading cycle (scoring, entries, brackets, flatten, halts) |
| `scheduler.py` | entry point; loops every 10 min if self-hosted |
| `watchdog.py` | restarts `scheduler.py` if self-hosted and it stalls |
| `events.json` | macro event blackout dates |
| `replay.py` | read-only replay of past days through the rules |
| `report.py` | end-of-day report posted to the `daily-report` issue |
| `noise.py` | noise-area momentum rules (Zarattini et al. 2024) and 10-year test |
| `shadow.py` | QQQ noise-area forward test, logging only (in the daily report) |
