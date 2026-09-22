# BTC + ETH 10m 6→9 Confirmation Bot V4

Railway signal-only bot for BTCUSDT and ETHUSDT Futures.

## Logic
- 10m candles are built from 5m MEXC candles.
- Start candle is the last candle in a same-color run.
- GREEN Start => LONG.
- RED Start => SHORT.
- Candle 6 must change color from Start.
- Candles 7, 8, 9 must remain the same color as candle 6.
- Final signal is sent only after candle 9 closes.
- About 2 minutes before candle 9 closes, if the currently forming candle 9 still matches candle 6, a pre-signal warning is sent.
- If candle 9 changes color before close, no final signal is sent.
- Warning and final signal are deduplicated.
- Display leverage is 30x by default.
- No MEXC API keys are required; only public market data is used.

## Railway variables
TELEGRAM_BOT_TOKEN=your_bot_token
CHAT_ID=-5370612713
LEVERAGE=30
SCAN_SECONDS=30
HISTORY_5M=240
