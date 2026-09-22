# BTC + ETH 10m 6-9 Confirmation Bot

Railway-ready, signal-only MEXC Futures Telegram bot.

## Data
MEXC public Futures OHLCV via CCXT. No MEXC API keys are required.

## 10m construction
MEXC 5m candles are paired into exact UTC 10m candles: 00+05, 10+15, 20+25, 30+35, 40+45, 50+55. Only fully closed 10m candles are used.

## Algorithm
1. Start = last candle of a same-color run.
2. Candle #6 must change color from start.
3. Candles #7, #8 and #9 must all match candle #6.
4. Signal only after #9 closes.
5. Start GREEN -> #6-#9 RED = SHORT.
6. Start RED -> #6-#9 GREEN = LONG.
7. Candles #1-#5 have no color requirement.
8. Doji cannot confirm.

## Railway variables
Required:
TELEGRAM_BOT_TOKEN=...
CHAT_ID=...

Optional:
LEVERAGE=30
SCAN_SECONDS=30
HISTORY_5M=240

Do not commit the Telegram token to GitHub.
