import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
"""
Swing Strategy 2 — D1 Structure Pullback at Key S/R Level
==========================================================
Fires when a clear D1 trend has pushed price into a major D1 or 4H
support/resistance level, and both 4H and 1H confirm a reversal is
starting in the CORRECTION direction.

This is the USD/JPY setup: D1 clearly bullish, price runs into a stacked
resistance wall, 4H starts breaking down. The trade is a SELL from the
resistance level, targeting the D1 Fibonacci golden zone below.

Equally valid in the opposite direction: D1 clearly bearish, price drops
into major support, 4H CHoCH bullish → BUY correction to golden zone above.

Confirmation cascade:
  D1 bias clearly bullish or bearish (not neutral)
  → Price within NEAR_SR_PIPS of a D1 or 4H S/R level in the opposing direction
  → 4H CHoCH (preferred) or 4H BOS in the correction direction
  → 1H BOS or CHoCH as entry confirmation
  → SL beyond the S/R level (+ buffer)
  → TP at the D1 Fibonacci golden zone (61.8% for SELL, 38.2% for BUY)

Scoring (max 100):
  D1 bias clearly established                 +20
  D1-timeframe S/R level hit                  +25
  4H-timeframe S/R level hit                  +18
  Multiple stacked S/R levels (2+)            +8
  4H CHoCH in correction direction            +25
  4H BOS in correction direction              +18
  1H CHoCH entry trigger                      +20
  1H BOS entry trigger                        +15
  Tradeable session (London / NY)             +10
  4H bias already flipped (confirms reversal) +8
"""

import config
from news_filter_live import is_symbol_blocked 
_fired_swings: dict[str, tuple] = {}


# ── Helpers ────────────────────────────────────────────────────────────────────

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


def _fib_zone(swing_hi: float, swing_lo: float) -> tuple[float, float] | tuple[None, None]:
    """Return (zone_top, zone_bottom) — the 38.2%–61.8% golden zone."""
    if not swing_hi or not swing_lo or swing_hi <= swing_lo:
        return None, None
    rng = swing_hi - swing_lo
    return (
        round(swing_hi - 0.382 * rng, 5),
        round(swing_hi - 0.618 * rng, 5),
    )


# ── Main check ─────────────────────────────────────────────────────────────────

def check(state: dict, debug: bool = False) -> dict | None:
    """
    SW2: D1 Structure Pullback at Key S/R Level.
    Returns a signal dict on success, None if conditions not met.
    """
    bias_d1 = state.get("bias", {}).get("d1", "neutral")
    bias_4h = state.get("bias", {}).get("4h", "neutral")
    price   = state.get("current_price", 0)
    symbol  = state.get("symbol", config.SYMBOL)
    pip     = config.get_symbol_cfg(symbol)["pip_size"]

    if not price or price <= 0:
        return None

    if bias_d1 not in ("bullish", "bearish"):
        if debug:
            print(f"  [SW2] {symbol}: D1 bias neutral — skip")
        return None

    # ── News filter ───────────────────────────────────────────────────────────
    news_blocked, news_reason = is_symbol_blocked(symbol, reference_ts=state.get("reference_ts"))
    if news_blocked:
        if debug:
            print(f"  [SW2] {symbol}: news block — {news_reason}")
        return None

    # ── Step 1: Correction direction ───────────────────────────────────────────
    correction  = "bearish" if bias_d1 == "bullish" else "bullish"
    trade_type  = "SELL"    if correction == "bearish" else "BUY"
    target_kind = "resistance" if trade_type == "SELL" else "support"

    # ── Step 2: Find strong D1/4H S/R level near price ────────────────────────
    sr_levels = state.get("sr_levels") or []

    NEAR_D1_PIPS = 40
    NEAR_4H_PIPS = 25

    matching_levels = []
    for lvl in sr_levels:
        lvl_price = lvl.get("price", 0)
        lvl_kind  = lvl.get("kind", "")
        lvl_tf    = lvl.get("timeframe", "")

        if lvl_kind != target_kind:
            continue
        if lvl_tf not in ("d1", "4h"):
            continue

        dist_pips = abs(lvl_price - price) / pip
        proximity = NEAR_D1_PIPS if lvl_tf == "d1" else NEAR_4H_PIPS

        if dist_pips <= proximity:
            matching_levels.append({**lvl, "_dist_pips": dist_pips})

    if not matching_levels:
        if debug:
            print(f"  [SW2] {symbol}: No D1/4H {target_kind} within range — skip")
        return None

    matching_levels.sort(key=lambda l: (
        0 if l.get("timeframe") == "d1" else 1,
        l.get("_dist_pips", 999),
    ))
    best_level  = matching_levels[0]
    level_price = best_level.get("price", price)
    level_tf    = best_level.get("timeframe", "4h")
    stacked     = len(matching_levels) >= 2

    
    # ── Step 2b: Verify price is extended enough from the golden zone ──────────
    _d1 = state.get("d1") or {}
    _zone_top, _zone_bottom = _fib_zone(_d1.get("swing_hi"), _d1.get("swing_lo"))
    SW2_MIN_EXTENSION_PIPS = 30
    if correction == "bearish" and _zone_top:
        _dist = (price - _zone_top) / pip
        if _dist < SW2_MIN_EXTENSION_PIPS:
            if debug:
                print(f"  [SW2] {symbol}: only {_dist:.1f}p above golden zone — not extended, skip")
            return None
    elif correction == "bullish" and _zone_bottom:
        _dist = (_zone_bottom - price) / pip
        if _dist < SW2_MIN_EXTENSION_PIPS:
            if debug:
                print(f"  [SW2] {symbol}: only {_dist:.1f}p below golden zone — not extended, skip")
            return None

    if debug:
        print(f"  [SW2] {symbol}: {len(matching_levels)} {target_kind} levels, best={level_tf} @ {level_price:.5f}")

    # ── Step 3: 4H and 1H confirmation ────────────────────────────────────────
    choch_4h = (state.get("4h") or {}).get("choch") or []
    bos_4h   = (state.get("4h") or {}).get("bos")   or []
    choch_1h = (state.get("1h") or {}).get("choch") or []
    bos_1h   = (state.get("1h") or {}).get("bos")   or []

    has_4h_choch = _recent_signal(choch_4h, correction, max_bars=8)
    has_4h_bos   = _recent_signal(bos_4h,   correction, max_bars=6)
    has_1h_choch = _recent_signal(choch_1h, correction, max_bars=5)
    has_1h_bos   = _recent_signal(bos_1h,   correction, max_bars=5)

    if not (has_4h_choch or has_4h_bos):
        if debug:
            print(f"  [SW2] {symbol}: No 4H {correction} signal — skip")
        return None

    if not (has_1h_choch or has_1h_bos):
        if debug:
            print(f"  [SW2] {symbol}: No 1H confirmation — skip")
        return None

    # ── Step 4: Score ─────────────────────────────────────────────────────────
    score   = 0
    reasons = []

    score += 20
    reasons.append(f"D1 {bias_d1} trend established")

    if level_tf == "d1":
        score += 25
        reasons.append(f"D1 {target_kind} @ {level_price:.5f}")
    else:
        score += 18
        reasons.append(f"4H {target_kind} @ {level_price:.5f}")

    if stacked:
        score += 8
        reasons.append(f"{len(matching_levels)} levels stacked")

    if has_4h_choch:
        score += 25
        reasons.append(f"4H CHoCH {correction}")
    elif has_4h_bos:
        score += 18
        reasons.append(f"4H BOS {correction}")

    if has_1h_choch:
        score += 20
        reasons.append("1H CHoCH entry")
    elif has_1h_bos:
        score += 15
        reasons.append("1H BOS entry")

    if state.get("tradeable_session"):
        score += 10
        reasons.append("London/NY session")

    if bias_4h == correction:
        score += 8
        reasons.append(f"4H bias flipped {correction}")

    if debug:
        print(f"  [SW2] {symbol}: score={score}  correction={trade_type}  stacked={stacked}")

    if score < config.SWING_MIN_CONFIDENCE:  # Fix: swing uses 75 not scalping 80
        if debug:
            print(f"  [SW2] {symbol}: score {score} < SWING_MIN_CONFIDENCE {config.SWING_MIN_CONFIDENCE}")
        return None

    # ── Step 5: SL and TP ─────────────────────────────────────────────────────
    entry    = price
    d1_data  = state.get("d1") or {}
    swing_hi = d1_data.get("swing_hi")
    swing_lo = d1_data.get("swing_lo")

    zone_top, zone_bottom = _fib_zone(swing_hi, swing_lo)

    if trade_type == "SELL":
        highest_level = max(matching_levels, key=lambda l: l.get("price", 0))
        sl = round(highest_level.get("price", level_price) + config.SL_BUFFER_PIPS * pip, 5)
        tp = zone_bottom if (zone_bottom and zone_bottom < entry) else round(entry - 150 * pip, 5)
    else:
        lowest_level = min(matching_levels, key=lambda l: l.get("price", 999))
        sl = round(lowest_level.get("price", level_price) - config.SL_BUFFER_PIPS * pip, 5)
        tp = zone_top if (zone_top and zone_top > entry) else round(entry + 150 * pip, 5)

    sl_pips = abs(entry - sl) / pip
    if sl_pips < config.MIN_SL_PIPS:
        if debug:
            print(f"  [SW2] {symbol}: SL {sl_pips:.1f}p < MIN_SL_PIPS {config.MIN_SL_PIPS} — skip")
        return None

    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)

    if sl_dist <= 0:
        return None

    rr = round(tp_dist / sl_dist, 2)

        # ── Duplicate guard ───────────────────────────────────────────────────────
    swing_key = (round(swing_hi, 5), round(swing_lo, 5)) if swing_hi and swing_lo else (level_price, 0)
    if _fired_swings.get(symbol) == swing_key:
        if debug:
            print(f"  [SW2] {symbol}: already fired on this D1 swing — skip")
        return None
    

    if rr < config.SWING_MIN_RR:  # Fix B4: was MIN_RR (2.0), swing requires SWING_MIN_RR (3.0)
        if debug:
            print(f"  [SW2] {symbol}: R:R {rr:.2f} < SWING_MIN_RR {config.SWING_MIN_RR} — skip")
        return None
    _fired_swings[symbol] = swing_key  

    return {
        "trade":      True,
        "type":       trade_type,
        "strategy":   "SW2 D1 Pullback",
        "confidence": min(score, 100),
        "reason":     " | ".join(reasons),
        "entry":      round(entry, 5),
        "sl":         sl,
        "tp":         tp,
        "rr":         rr,
        "swing_hi":   swing_hi,
        "swing_lo":   swing_lo,
    }