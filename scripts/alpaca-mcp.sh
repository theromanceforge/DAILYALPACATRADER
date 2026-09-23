#!/usr/bin/env bash
# Starts Alpaca's official MCP server (read-only use, PAPER account only)
# for Claude Code chats in this repo. It is NOT used by the trading bot.
#
# Reads this bot's paper keys from APCA_API_KEY_ID / APCA_API_SECRET_KEY
# (the same names as the GitHub secrets). Refuses anything that isn't a
# paper key (PK...), and pins the paper endpoint. Order-placing and
# position-closing tools are blocked in .claude/settings.json.
set -euo pipefail

key="${APCA_API_KEY_ID:-}"
secret="${APCA_API_SECRET_KEY:-}"
if [[ "$key" != PK* || -z "$secret" ]]; then
  echo "alpaca-mcp: refusing to start: need this bot's PAPER keys (PK...) in APCA_API_KEY_ID / APCA_API_SECRET_KEY" >&2
  exit 1
fi

export ALPACA_API_KEY="$key"
export ALPACA_SECRET_KEY="$secret"
export ALPACA_PAPER_TRADE=true
unset TRADING_API_URL
# Account, positions/orders (read tools only; writes are denied in
# settings), assets/calendar/clock, stock data, news. No crypto/options.
export ALPACA_TOOLSETS="account,trading,assets,stock-data,news"

exec uvx alpaca-mcp-server==2.3.2
