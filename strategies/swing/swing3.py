import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
"""
Swing Strategy 3 — D1 Golden Zone Bounce Continuation
======================================================
Fires when price has ALREADY bounced from the D1 Fibonacci golden zone
(38.2%–61.8%) and both 4H and 1H confirm the original D1 trend is resuming.

Highest conviction of the three SW setups:
  SW1 fires INTO the zone (anticipating the bounce)
  SW2 fires at a resistance wall (expecting correction to the zone)
  SW3 fires AFTER the zone has held and the trend is visibly resuming

SL is tight (just beyond the proven zone), still achieves 3R+ because
the zone is close and the target is the full D1 swing extreme.
"""

import config
from news_filter_live import is_symbol_blocked
_fired_swings: dict[str, tuple] = {}


def _recent_signal(events: list, direction: str, max_bars: int) -> bool:
    if not events:
        return False
    tail = events[-max_bars:] if len(events) > max_bars else events
    for ev in reversed(tail):
        ev_dir = (ev.get("direction") or ev.get("trend") or "").lower()
        if ev_dir == direction.lower():
            return True
    return False


def check(state: dict, debug: bool = False) -> dict | None:
    """SW3: D1 Golden Zone Bounce Continuation."""
    bias_d1 = state.get("bias", {}).get("d1", "neutral")
    bias_4h = state.get("bias", {}).get("4h", "neutral")
    price   = state.get("current_price", 0)
    symbol  = state.get("symbol", config.SYMBOL)
    pip     = config.get_symbol_cfg(symbol)["pip_size"]

    if not price or price <= 0:
        return None

    if bias_d1 not in ("bullish", "bearish"):
        if debug:
            print(f"  [SW3] {symbol}: D1 neutral — skip")
        return None

    # ── News filter ───────────────────────────────────────────────────────────
    news_blocked, news_reason = is_symbol_blocked(symbol, reference_ts=state.get("reference_ts"))
    if news_blocked:
        if debug:
            print(f"  [SW3] {symbol}: news block — {news_reason}")
        return None

    # ── Step 1: D1 swing and zone ─────────────────────────────────────────────
    d1_data  = state.get("d1") or {}
    swing_hi = d1_data.get("swing_hi")
    swing_lo = d1_data.get("swing_lo")

    if not swing_hi or not swing_lo or swing_hi <= swing_lo:
        if debug:
            print(f"  [SW3] {symbol}: No D1 swing levels — skip")
        return None

    rng         = swing_hi - swing_lo
    zone_top    = round(swing_hi - 0.382 * rng, 5)   # 38.2%
    zone_bottom = round(swing_hi - 0.618 * rng, 5)   # 61.8%

    # ── Step 2: Zone must have been visited and price must have left it ────────
    zone_visited = d1_data.get("zone_visited", False)

    if not zone_visited:
        if debug:
            print(f"  [SW3] {symbol}: Zone not yet visited this swing — skip")
        return None

    direction  = "bullish" if bias_d1 == "bullish" else "bearish"
    trade_type = "BUY"     if direction == "bullish" else "SELL"

    ZONE_CLEAR_PIPS  = 20
    MAX_DIST_PIPS    = 120

    if trade_type == "BUY":
        if price <= zone_top + ZONE_CLEAR_PIPS * pip:
            if debug:
                print(f"  [SW3] {symbol}: Price not clear of zone yet — skip")
            return None
        dist_from_zone = (price - zone_top) / pip
    else:
        if price >= zone_bottom - ZONE_CLEAR_PIPS * pip:
            if debug:
                print(f"  [SW3] {symbol}: Price not clear of zone yet — skip")
            return None
        dist_from_zone = (zone_bottom - price) / pip

    if dist_from_zone > MAX_DIST_PIPS:
        if debug:
            print(f"  [SW3] {symbol}: {dist_from_zone:.0f}p from zone — entry window closed")
        return None

    # ── Step 3: 4H and 1H confirmation ────────────────────────────────────────
    choch_4h = (state.get("4h") or {}).get("choch") or []
    bos_4h   = (state.get("4h") or {}).get("bos")   or []
    choch_1h = (state.get("1h") or {}).get("choch") or []
    bos_1h   = (state.get("1h") or {}).get("bos")   or []

    has_4h_choch = _recent_signal(choch_4h, direction, max_bars=12)
    has_4h_bos   = _recent_signal(bos_4h,   direction, max_bars=10)
    has_1h_choch = _recent_signal(choch_1h, direction, max_bars=6)
    has_1h_bos   = _recent_signal(bos_1h,   direction, max_bars=6)

    if not (has_4h_choch or has_4h_bos):
        if debug:
            print(f"  [SW3] {symbol}: No 4H {direction} confirmation — skip")
        return None

    if not (has_1h_choch or has_1h_bos):
        if debug:
            print(f"  [SW3] {symbol}: No 1H entry trigger — skip")
        return None

    # ── Step 4: Score ─────────────────────────────────────────────────────────
    score   = 0
    reasons = []

    score += 30
    reasons.append(f"zone held ({zone_bottom:.5f}–{zone_top:.5f}), {dist_from_zone:.0f}p clear")

    score += 20
    reasons.append(f"D1 {bias_d1} continuing")

    if has_4h_choch:
        score += 25
        reasons.append(f"4H CHoCH {direction}")
    elif has_4h_bos:
        score += 18
        reasons.append(f"4H BOS {direction}")

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
        score += 8
        reasons.append("4H bias realigned")

    if debug:
        print(f"  [SW3] {symbol}: score={score}  type={trade_type}  dist={dist_from_zone:.0f}p")

    if score < config.SWING_MIN_CONFIDENCE:  # Fix: swing uses 75 not scalping 80
        return None

    # ── Step 5: SL and TP ─────────────────────────────────────────────────────
    entry = price

    if trade_type == "BUY":
        sl           = round(zone_bottom - config.SL_BUFFER_PIPS * pip, 5)
        tp_primary   = swing_hi
        tp_stretch   = round(swing_hi + 0.272 * rng, 5)
    else:
        sl           = round(zone_top + config.SL_BUFFER_PIPS * pip, 5)
        tp_primary   = swing_lo
        tp_stretch   = round(swing_lo - 0.272 * rng, 5)

    sl_pips = abs(entry - sl) / pip
    if sl_pips < config.MIN_SL_PIPS:
        return None

    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return None

    rr_primary = round(abs(tp_primary - entry) / sl_dist, 2)
    tp = tp_primary if rr_primary >= config.SWING_MIN_RR else tp_stretch  # Fix B4: was MIN_RR (2.0)
    rr = round(abs(tp - entry) / sl_dist, 2)

        # ── Duplicate guard ───────────────────────────────────────────────────────
    swing_key = (round(swing_hi, 5), round(swing_lo, 5))
    if _fired_swings.get(symbol) == swing_key:
        if debug:
            print(f"  [SW3] {symbol}: already fired on this D1 swing — skip")
        return None
    

    if rr < config.SWING_MIN_RR:  # Fix B4: was MIN_RR (2.0), swing requires SWING_MIN_RR (3.0)
        if debug:
            print(f"  [SW3] {symbol}: R:R {rr:.2f} < SWING_MIN_RR {config.SWING_MIN_RR} — skip")
        return None
    _fired_swings[symbol] = swing_key  

    return {
        "trade":      True,
        "type":       trade_type,
        "strategy":   "SW3 Zone Bounce Continuation",
        "confidence": min(score, 100),
        "reason":     " | ".join(reasons),
        "entry":      round(entry, 5),
        "sl":         sl,
        "tp":         tp,
        "rr":         rr,
        "swing_hi":   swing_hi,
        "swing_lo":   swing_lo,
    }