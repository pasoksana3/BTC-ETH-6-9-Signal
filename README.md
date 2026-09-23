# BTC + ETH 10m 6→9 PRE-2MIN + WIN bot

Separate Railway service. Does not modify the existing bot.

- BTCUSDT and ETHUSDT Futures
- 10m candles built from 5m MEXC candles
- Start candle is the last candle of its same-color run
- #6 must change color from Start
- #7, #8 and #9 must match #6
- Direction comes from Start color: GREEN=LONG, RED=SHORT
- Pre-signal is sent once during the 90–150 second window before #9 closes, only while current #9 still matches #6
- Confirmed signal is sent only after #9 closes
- WIN/LOSS result is checked after candles #10–#13

## Railway variables
TELEGRAM_BOT_TOKEN=...
CHAT_ID=-5370612713
LEVERAGE=30
SCAN_SECONDS=30
WIN_MOVE_PCT=0.005
WIN_CHECK_CANDLES=4

The WIN criterion is configurable: default is a 0.50% move in the signal direction based on the close of #9.
