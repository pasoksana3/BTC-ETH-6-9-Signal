# Offline logic test for the critical #6=#7=#8=#9 rule.
# This file does not contact MEXC or Telegram.

from bot import candle_color

def c(color, ts):
    if color == "GREEN":
        return {"ts": ts, "open": 1.0, "close": 2.0}
    return {"ts": ts, "open": 2.0, "close": 1.0}

# Valid LONG pattern: Start GREEN, #6-#9 RED, then #10 GREEN => WIN.
seq = [
    c("GREEN", 0),   # Start
    c("RED", 600),   # #1
    c("GREEN", 1200),# #2
    c("GREEN", 1800),# #3
    c("RED", 2400),  # #4
    c("GREEN", 3000),# #5
    c("RED", 3600),  # #6
    c("RED", 4200),  # #7
    c("RED", 4800),  # #8
    c("RED", 5400),  # #9
    c("GREEN", 6000),# #10 => WIN
]

assert [candle_color(x) for x in seq[6:10]] == ["RED"] * 4
assert candle_color(seq[10]) == "GREEN"

# Invalid pattern: #9 changes color, so there must be NO signal.
bad = seq[:9] + [c("GREEN", 5400)]
assert not (
    candle_color(bad[6])
    == candle_color(bad[7])
    == candle_color(bad[8])
    == candle_color(bad[9])
)

print("OK: critical #6=#7=#8=#9 validation passed.")
