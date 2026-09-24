# BTC + ETH 10m 6-9 Confirmation Bot

Separate signal-only bot for BTCUSDT and ETHUSDT Futures.

Rule:
- Start is the last candle of a same-color run.
- Candle 6 must change color relative to the start.
- Candles 7, 8 and 9 must all match candle 6.
- Signal only after candle 9 closes.
- Public MEXC market data only; no MEXC API keys.
- Environment: TELEGRAM_BOT_TOKEN, CHAT_ID, LEVERAGE=30, SCAN_SECONDS=30.
