import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
"""
Swing Strategy 1 — D1 Fibonacci Golden Zone Reversal
=====================================================
Fires when price has moved into or beyond the D1 Fibonacci golden zone
(38.2%–61.8% retracement of the last D1 swing) and a reversal is confirmed
on both the 4H and 1H timeframes.

Two modes:
  CONTINUATION — D1 bullish/bearish, price pulls back INTO the golden zone,
                 trade fires IN the D1 direction (highest probability)
  EXTENSION   — Price has pushed PAST the 61.8% level (overshoot), a D1-level
                 reversal signal is present, trade fires AGAINST the prior D1
                 direction (the EUR/USD type setup)

Confirmation cascade:
  D1 swing defined + price in/near zone
  → 4H CHoCH or BOS in trade direction
  → 1H BOS or CHoCH as entry trigger
  → SL beyond D1 swing extreme (+ buffer)
  → TP at 127.2% Fibonacci extension (or 38.2% level as fallback)

Scoring (max 100):
  Price inside D1 golden zone                +30
  Price within 30 pips of zone               +20
  D1 bias aligned with trade direction        +15
  D1 reversal setup (extension mode)          +8
  4H CHoCH in direction                       +25
  4H BOS in direction                         +18
  1H CHoCH entry trigger                      +20
  1H BOS entry trigger                        +15
  Tradeable session (London / NY)             +10
  4H bias aligned                             +5
"""

import config
from news_filter_live import is_symbol_blocked 
_fired_swings: dict[str, tuple] = {}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fib_levels(swing_hi: float, swing_lo: float) -> dict:
    """Compute all key Fibonacci levels from a D1 swing."""
    if not swing_hi or not swing_lo or swing_hi <= swing_lo:
        return {}
    rng = swing_hi - swing_lo
    return {
        "0":     round(swing_hi, 5),
        "23.6":  round(swing_hi - 0.236 * rng, 5),
        "38.2":  round(swing_hi - 0.382 * rng, 5),
        "50":    round(swing_hi - 0.500 * rng, 5),
        "61.8":  round(swing_hi - 0.618 * rng, 5),
        "78.6":  round(swing_hi - 0.786 * rng, 5),
        "100":   round(swing_lo, 5),
        "127.2": round(swing_lo - 0.272 * rng, 5),
        "161.8": round(swing_lo - 0.618 * rng, 5),
    }


def _recent_signal(events: list, direction: str, max_bars: int) -> bool:
    """Return True if any event in the tail of the list matches direction."""
    if not events:
        return False
    tail = events[-max_bars:] if len(events) > max_bars else events
    for ev in reversed(tail):
        ev_dir = (ev.get("direction") or ev.get("trend") or "").lower()
        if ev_dir == direction.lower():
            return True
    return False


# ── Main check ─────────────────────────────────────────────────────────────────

def check(state: dict, debug: bool = False) -> dict | None:
    """
    SW1: D1 Fibonacci Golden Zone Reversal.
    Returns a signal dict on success, None if conditions not met.
    """
    bias_d1 = state.get("bias", {}).get("d1", "neutral")
    bias_4h = state.get("bias", {}).get("4h", "neutral")
    price   = state.get("current_price", 0)
    symbol  = state.get("symbol", config.SYMBOL)
    pip     = config.get_symbol_cfg(symbol)["pip_size"]

    if not price or price <= 0:
        return None

    # ── News filter ───────────────────────────────────────────────────────────
    news_blocked, news_reason = is_symbol_blocked(symbol, reference_ts=state.get("reference_ts"))
    if news_blocked:
        if debug:
            print(f"  [SW1] {symbol}: news block — {news_reason}")
        return None

    # ── Step 1: D1 swing levels ────────────────────────────────────────────────
    d1_data  = state.get("d1") or {}
    swing_hi = d1_data.get("swing_hi")
    swing_lo = d1_data.get("swing_lo")

    if not swing_hi or not swing_lo or swing_hi <= swing_lo:
        if debug:
            print(f"  [SW1] {symbol}: No valid D1 swing levels (hi={swing_hi} lo={swing_lo})")
        return None

    fib = _fib_levels(swing_hi, swing_lo)
    if not fib:
        return None

    zone_top    = fib["38.2"]   # closer to swing high
    zone_bottom = fib["61.8"]   # closer to swing low

    # ── Step 2: Where is price relative to the zone? ───────────────────────────
    in_zone    = zone_bottom <= price <= zone_top
    above_zone = price > zone_top     # hasn't pulled back to zone yet
    below_zone = price < zone_bottom  # overshoot below zone

    if above_zone:
        dist_pips = (price - zone_top)    / pip
    elif below_zone:
        dist_pips = (zone_bottom - price) / pip
    else:
        dist_pips = 0.0

    MAX_PROXIMITY_PIPS = 30
    if not in_zone and dist_pips > MAX_PROXIMITY_PIPS:
        if debug:
            print(f"  [SW1] {symbol}: {dist_pips:.0f}p from D1 zone (max {MAX_PROXIMITY_PIPS}p) — skip")
        return None

    # ── Step 3: Trade direction ────────────────────────────────────────────────
    # CONTINUATION mode: price near/inside zone, trade WITH D1 trend
    # EXTENSION mode: price has shot past 61.8% into overshoot territory,
    #                 trade expects a snap-back in the opposite direction
    if in_zone or above_zone:
        direction  = bias_d1 if bias_d1 in ("bullish", "bearish") else "bullish"
        mode_label = "continuation"
    else:
        if bias_d1 == "bearish":
            if debug:
                print(f"  [SW1] {symbol}: below zone in bearish D1 — extension mode skipped")
            return None
        direction  = "bullish"
        mode_label = "extension reversal"

    trade_type = "BUY" if direction == "bullish" else "SELL"

    # ── Step 4: 4H and 1H confirmation ────────────────────────────────────────
    choch_4h = (state.get("4h") or {}).get("choch") or []
    bos_4h   = (state.get("4h") or {}).get("bos")   or []
    choch_1h = (state.get("1h") or {}).get("choch") or []
    bos_1h   = (state.get("1h") or {}).get("bos")   or []

    has_4h_choch = _recent_signal(choch_4h, direction, max_bars=10)
    has_4h_bos   = _recent_signal(bos_4h,   direction, max_bars=8)
    has_1h_choch = _recent_signal(choch_1h, direction, max_bars=5)
    has_1h_bos   = _recent_signal(bos_1h,   direction, max_bars=5)

    # Fresh zone entry = require CHoCH not just BOS (tighter filter)
    zone_visited = state.get("d1", {}).get("zone_visited", False)
    if not zone_visited and not has_4h_choch:
        if debug:
            print(f"  [SW1] {symbol}: fresh zone entry — 4H CHoCH required, BOS alone not accepted — skip")
        return None

    if not (has_4h_choch or has_4h_bos):
        if debug:
            print(f"  [SW1] {symbol}: No 4H {direction} CHoCH/BOS — skip")
        return None

    if not (has_1h_choch or has_1h_bos):
        if debug:
            print(f"  [SW1] {symbol}: No 1H {direction} BOS/CHoCH — skip")
        return None

    # ── Step 5: Score ─────────────────────────────────────────────────────────
    score   = 0
    reasons = []

    if in_zone:
        score += 30
        reasons.append(f"inside D1 golden zone ({zone_bottom:.5f}–{zone_top:.5f})")
    else:
        score += 20
        reasons.append(f"near D1 zone ({dist_pips:.0f}p, {mode_label})")

    if bias_d1 == direction:
        score += 15
        reasons.append(f"D1 {bias_d1} aligned")
    elif bias_d1 != "neutral":
        score += 8
        reasons.append(f"D1 {bias_d1} → {mode_label} trade {trade_type}")

    if has_4h_choch:
        score += 25
        reasons.append("4H CHoCH confirmed")
    elif has_4h_bos:
        score += 18
        reasons.append("4H BOS confirmed")

    if has_1h_choch:
        score += 20
        reasons.append("1H CHoCH entry")
    elif has_1h_bos:
        score += 15
        reasons.append("1H BOS entry")

    if state.get("tradeable_session"):
        score += 10
        reasons.append("London/NY session")

    if bias_4h == direction:
        score += 5
        reasons.append("4H bias aligned")

    if debug:
        print(f"  [SW1] {symbol}: score={score}  dir={trade_type}  mode={mode_label}  reasons={reasons}")

    if score < config.SWING_MIN_CONFIDENCE:  # Fix: swing uses 75 not scalping 80
        if debug:
            print(f"  [SW1] {symbol}: score {score} < SWING_MIN_CONFIDENCE {config.SWING_MIN_CONFIDENCE}")
        return None

    # ── Step 6: SL and TP ─────────────────────────────────────────────────────
    entry = price

    if trade_type == "BUY":
        raw_sl      = swing_lo - config.SL_BUFFER_PIPS * pip
        sl          = round(raw_sl, 5)
        tp_ext      = config.fib_extension_tp(state, "bullish", entry)
        tp_fallback = round(zone_top + (zone_top - zone_bottom), 5)
        tp          = tp_ext if (tp_ext and tp_ext > entry) else tp_fallback
    else:
        raw_sl      = swing_hi + config.SL_BUFFER_PIPS * pip
        sl          = round(raw_sl, 5)
        tp_ext      = config.fib_extension_tp(state, "bearish", entry)
        tp_fallback = round(fib["78.6"] - (zone_top - zone_bottom), 5)
        tp          = tp_ext if (tp_ext and tp_ext < entry) else tp_fallback

    sl_pips = abs(entry - sl) / pip
    if sl_pips < config.MIN_SL_PIPS:
        if debug:
            print(f"  [SW1] {symbol}: SL {sl_pips:.1f}p < MIN_SL_PIPS {config.MIN_SL_PIPS} — skip")
        return None

    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    rr      = round(tp_dist / sl_dist, 2) if sl_dist > 0 else 0

        # ── Duplicate guard ───────────────────────────────────────────────────────
    swing_key = (round(swing_hi, 5), round(swing_lo, 5))
    if _fired_swings.get(symbol) == swing_key:
        if debug:
            print(f"  [SW1] {symbol}: already fired on this D1 swing — skip")
        return None
    

    if rr < config.SWING_MIN_RR:  # Fix B4: was MIN_RR (2.0), swing requires SWING_MIN_RR (3.0)
        if debug:
            print(f"  [SW1] {symbol}: R:R {rr:.2f} < SWING_MIN_RR {config.SWING_MIN_RR} — skip")
        return None
    _fired_swings[symbol] = swing_key  
    return {
        "trade":      True,
        "type":       trade_type,
        "strategy":   "SW1 D1 Fib Reversal",
        "confidence": min(score, 100),
        "reason":     " | ".join(reasons),
        "entry":      round(entry, 5),
        "sl":         sl,
        "tp":         tp,
        "rr":         rr,
        "swing_hi":   swing_hi,
        "swing_lo":   swing_lo,
    }