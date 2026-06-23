"""
Custom Strategy Template — CUSTOM1
===================================
Drop your own logic here. The backtest engine calls check() once per bar
on the entry timeframe (1H by default for CUSTOM1).

HOW TO USE
----------
1. Fill in the logic inside check() below.
2. Return a signal dict when you want to open a trade, or None to skip.
3. That's it. The engine handles entry, SL/TP management, PnL, stats.

HOW TO REGISTER A SECOND CUSTOM STRATEGY
-----------------------------------------
- Copy this file to e.g. strategies/custom/custom2.py
- Add to STRATEGY_MAP in backtest_engine.py:
      "CUSTOM2": ("custom", "custom2"),
- Add "CUSTOM2" to STRATEGY_CODES list in dashboard/app.py

ENTRY TIMEFRAME NOTE
---------------------
CUSTOM1 does NOT start with "SC" so the engine auto-selects 1H as the
entry timeframe (same as swing strategies). Your check() is called once
per 1H candle. You can still READ 5M/15M data from state — it is always
available — but the engine OPENS trades on 1H bar closes.

To force 5M entry (scalp-style), rename the code to start with "SC",
e.g. "SC7": ("custom", "custom_template") in STRATEGY_MAP.

═══════════════════════════════════════════════════════════════════════
STATE DICT — everything available to your strategy each bar
═══════════════════════════════════════════════════════════════════════

state["symbol"]            str   e.g. "USD/JPY"
state["current_price"]     float close of the finest loaded TF (5M or 1H)
state["reference_ts"]      int   unix timestamp of the current bar
state["tradeable_session"] bool  True when London or New York is active
state["sessions"]          list  e.g. ["london", "new_york"]
state["bias"]              dict  {"d1": "bullish"|"bearish"|"neutral",
                                  "4h": ..., "1h": ..., "15m": ...}

Per-timeframe keys (all lowercase): "d1", "4h", "1h", "15m", "5m"
Each is a dict:

  state["1h"]["candles"]    list[dict]   up to 200 bars, each:
                                         {"time": unix_ts, "open": float,
                                          "high": float, "low": float,
                                          "close": float, "volume": float}
                                         candles[-1] = most recent closed bar
  state["1h"]["bos"]        list[dict]   Break-of-Structure events
  state["1h"]["choch"]      list[dict]   Change-of-Character events
  state["1h"]["structure"]  list         structure swing labels
  state["1h"]["zones"]      list[dict]   supply / demand zones
  state["1h"]["swing_hi"]   float|None   most recent swing high price
  state["1h"]["swing_lo"]   float|None   most recent swing low price

BOS / CHoCH event dict fields (from detect_bos / detect_choch engines):
  event["direction"]   "bullish" or "bearish"
  event["price"]       float — level that was broken
  event["ts"]          int   — unix timestamp of the break candle

═══════════════════════════════════════════════════════════════════════
SIGNAL DICT — what check() must return to open a trade
═══════════════════════════════════════════════════════════════════════

Return None (or any falsy value) → no trade this bar.

Return a dict with these keys to open a trade:

  REQUIRED
  --------
  "trade"       any truthy value, e.g. True
  "type"        "BUY" or "SELL"
  "sl"          float   stop-loss price
  "tp"          float   take-profit price

  OPTIONAL (used in stats + dashboard display)
  --------------------------------------------
  "entry"       float   entry price (defaults to current bar close)
  "rr"          float   reward-to-risk ratio, e.g. 2.0
  "confidence"  float   0–100 score
  "reason"      str     short label shown in results, e.g. "1H BOS + D1 bull"
  "strategy"    str     display name, e.g. "My Custom Strategy"

ENGINE SANITY CHECKS (trade skipped if these fail)
  BUY:   sl < entry < tp   (SL below entry, TP above)
  SELL:  tp < entry < sl   (TP below entry, SL above)
"""

# ---------------------------------------------------------------------------
# Pip helpers — works for JPY pairs (0.01 pip) and standard pairs (0.0001)
# ---------------------------------------------------------------------------
def _pip(symbol: str) -> float:
    return 0.01 if "JPY" in symbol.upper() else 0.0001


# ---------------------------------------------------------------------------
# check() — called once per entry-TF bar by the backtest engine
# ---------------------------------------------------------------------------
def check(state: dict, debug: bool = False) -> dict | None:
    """
    Return a signal dict to open a trade, or None to pass this bar.
    """

    # ── Convenience aliases ────────────────────────────────────────────────
    symbol  = state["symbol"]
    price   = state["current_price"]
    pip     = _pip(symbol)

    d1      = state.get("d1",  {})
    h4      = state.get("4h",  {})
    h1      = state.get("1h",  {})
    m15     = state.get("15m", {})
    m5      = state.get("5m",  {})

    bias_d1 = state["bias"].get("d1",  "neutral")
    bias_h4 = state["bias"].get("4h",  "neutral")
    bias_h1 = state["bias"].get("1h",  "neutral")

    bos_h1  = h1.get("bos",   [])
    bos_h4  = h4.get("bos",   [])

    tradeable = state.get("tradeable_session", False)

    # ══════════════════════════════════════════════════════════════════════
    # YOUR LOGIC GOES HERE
    # ══════════════════════════════════════════════════════════════════════
    #
    # EXAMPLE: 3-TF confluence BOS entry
    # Condition: D1 bullish bias + 4H bullish bias + fresh 1H bullish BOS
    # Entry: current price (market order at bar close)
    # SL:    20 pips below entry
    # TP:    40 pips above entry (1:2 RR)
    #
    # Delete or replace everything in this block with your own rules.
    # ──────────────────────────────────────────────────────────────────────

    if not tradeable:
        return None

    if not bos_h1:
        return None

    latest_bos = bos_h1[-1]
    direction  = latest_bos.get("direction", "")

    # ── BUY setup ─────────────────────────────────────────────────────────
    if (direction == "bullish"
            and bias_d1 == "bullish"
            and bias_h4 == "bullish"):

        sl = round(price - 20 * pip, 5)
        tp = round(price + 40 * pip, 5)
        if not (sl < price < tp):
            return None

        if debug:
            print(f"  [CUSTOM1] BUY  entry={price:.5f}  sl={sl:.5f}  tp={tp:.5f}"
                  f"  d1={bias_d1}  4h={bias_h4}")

        return {
            "trade":      True,
            "type":       "BUY",
            "entry":      price,
            "sl":         sl,
            "tp":         tp,
            "rr":         2.0,
            "confidence": 60.0,
            "reason":     "1H BOS bull + D1/4H bull",
            "strategy":   "Custom Template",
        }

    # ── SELL setup ────────────────────────────────────────────────────────
    if (direction == "bearish"
            and bias_d1 == "bearish"
            and bias_h4 == "bearish"):

        sl = round(price + 20 * pip, 5)
        tp = round(price - 40 * pip, 5)
        if not (tp < price < sl):
            return None

        if debug:
            print(f"  [CUSTOM1] SELL entry={price:.5f}  sl={sl:.5f}  tp={tp:.5f}"
                  f"  d1={bias_d1}  4h={bias_h4}")

        return {
            "trade":      True,
            "type":       "SELL",
            "entry":      price,
            "sl":         sl,
            "tp":         tp,
            "rr":         2.0,
            "confidence": 60.0,
            "reason":     "1H BOS bear + D1/4H bear",
            "strategy":   "Custom Template",
        }

    # ── No signal ─────────────────────────────────────────────────────────
    return None
