#!/bin/sh
set -e
echo '=== BTC + ETH 10m 6-9 BOT ==='
echo "Date: $(date -u)"
echo 'Launching bot...'
exec python -u /app/bot.py
