"""
SW4 — D1 Macro Exhaustion Reversal

The highest-conviction, highest-R:R setup in the swing engine.
Fires when a multi-week or multi-month D1 trend has pushed price to an
extreme structural level and the first signs of a true trend flip appear.

Unlike SW1–SW3 (which trade corrections or bounces WITHIN the trend),
SW4 trades AGAINST the D1 bias — it is a contrarian macro reversal.

Why this works:
  After an extended trend, price reaches an absolute extreme (a major D1/weekly
  S/R wall). Institutional participants who drove the trend begin taking profit
  and reversing. The 4H shows the first CHoCH — a structural break that signals
  the trend is exhausting. Once that CHoCH is confirmed, the risk/reward is
  asymmetric:
    - SL is placed just beyond the absolute extreme (the level that must NOT be
      broken for the reversal thesis to hold — price almost never returns here)
    - TP is the 38.2%–61.8% retracement of the ENTIRE macro swing

Key differences from SW1–SW3:
  - Counter-trend by design (fires against the D1 bias)
  - Entry must be CLOSE to the extreme (within SW4_NEAR_EXTREME_PIPS)
    so the SL is naturally tight relative to the macro TP distance
  - Minimum R:R is 4.0 (higher than global MIN_RR=3.0)
  - Minimum score is 80 (higher than global MIN_CONFIDENCE=75)
  - Fires very rarely — 2–5 times per year per pair

Real examples this targets:
  USD/JPY D1 at multi-month high (~163): SELL reversal, SL above 163,
    target 38.2% of the bullish macro swing back down.
  EUR/USD D1 at multi-month low (~1.13): BUY reversal, SL below the low,
    target 38.2% of the bearish macro swing back up.

Scoring (max ~130, minimum 80):
  Price within 15 pips of extreme      +35
  Price within 30 pips of extreme      +25
  Price within 50 pips of extreme      +15
  D1 S/R cluster at extreme (2+ lvls)  +15
  D1 S/R cluster at extreme (1 lvl)    +10
  4H CHoCH confirmed (REQUIRED)        +25
  1H CHoCH entry trigger               +20
  1H BOS entry trigger                 +15
  4H bias already flipped              +10
  London/NY session                    +10
  Correlated pair confirmation bonus   +10
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import time as _time
import config
from news_filter_live import is_symbol_blocked

_fired_swings: dict[str, tuple] = {}
_pending_sw4:  dict[str, tuple] = {}   # symbol → (direction, timestamp)

SW4_NEAR_EXTREME_PIPS = 50
SW4_MIN_CONFIDENCE    = 80
SW4_MIN_RR            = 4.0
SW4_SL_BUFFER_PIPS    = 15
SW4_TP_PRIMARY_FIB    = 0.382
SW4_TP_EXTENSION_FIB  = 0.618
SW4_CORR_EXPIRY_SECS  = 600   # correlated-pair signal stays valid for 10 minutes

# Correlated-pair map: symbol → (correlated symbol, expected direction of that pair)
# Logic: USDJPY SELL + EURUSD BUY = both signal USD weakening (macro confirmation)
#        EURUSD BUY + USDJPY SELL = same theme, viewed from the other side
_CORR_MAP: dict[str, tuple[str, str]] = {
    "USD/JPY": ("EUR/USD", "bullish"),   # USDJPY SELL → expect EURUSD BUY
    "EUR/USD": ("USD/JPY", "bearish"),   # EURUSD BUY  → expect USDJPY SELL
    "GBP/USD": ("USD/JPY", "bearish"),   # GBPUSD BUY  → expect USDJPY SELL
    "AUD/USD": ("USD/JPY", "bearish"),   # AUDUSD BUY  → expect USDJPY SELL
}


def _recent_signal(events: list, direction: str, max_bars: int = 8) -> bool:
    """
    Returns True if there is a recent signal in the given direction.
    'max_bars' limits how far back we look (each bar = one candle on that TF).
    Uses list position as a proxy for recency (most recent = last in list).
    """
    if not events or not isinstance(events, list):
        return False
    recent = events[-max_bars:]
    for ev in reversed(recent):
        if not isinstance(ev, dict):
            continue
        if ev.get("direction") == direction:
            return True
    return False


def check(state: dict, debug: bool = False) -> dict | None:
    if not isinstance(state, dict):
        return None

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
            print(f"  [SW4] {symbol}: news block — {news_reason}")
        return None

    # ── Step 1: D1 swing levels ────────────────────────────────────────────────
    d1_data  = state.get("d1") or {}
    swing_hi = d1_data.get("swing_hi")
    swing_lo = d1_data.get("swing_lo")

    if not swing_hi or not swing_lo or swing_hi <= swing_lo:
        if debug:
            print(f"  [SW4] {symbol}: No valid D1 swing levels (hi={swing_hi} lo={swing_lo})")
        return None

    macro_range = swing_hi - swing_lo
    if macro_range / pip < 200:
        if debug:
            print(f"  [SW4] {symbol}: macro swing only {macro_range/pip:.0f}p — too small for SW4 (need 200+p)")
        return None

    # ── Step 2: D1 bias must be clear — SW4 fades it ─────────────────────────
    if bias_d1 not in ("bullish", "bearish"):
        if debug:
            print(f"  [SW4] {symbol}: D1 bias neutral — no macro trend to fade")
        return None

    direction  = "bearish" if bias_d1 == "bullish" else "bullish"
    trade_type = "SELL"    if direction == "bearish" else "BUY"

    # ── Step 3: Price must be CLOSE to the D1 swing extreme ──────────────────
    # For a D1 bullish trend:  extreme = swing_hi (we fade from the top)
    # For a D1 bearish trend:  extreme = swing_lo (we fade from the bottom)
    if bias_d1 == "bullish":
        absolute_extreme = swing_hi
        dist_to_extreme  = (swing_hi - price) / pip
    else:
        absolute_extreme = swing_lo
        dist_to_extreme  = (price - swing_lo) / pip

    if dist_to_extreme > SW4_NEAR_EXTREME_PIPS:
        if debug:
            print(
                f"  [SW4] {symbol}: price {dist_to_extreme:.1f}p from extreme "
                f"({absolute_extreme:.5f}) — need ≤{SW4_NEAR_EXTREME_PIPS}p"
            )
        return None

    if debug:
        print(f"  [SW4] {symbol}: {dist_to_extreme:.1f}p from D1 extreme {absolute_extreme:.5f}")

    # ── Step 4: S/R confluence at the extreme ─────────────────────────────────
    sr_levels = state.get("sr_levels") or []
    extreme_levels = [
        lvl for lvl in sr_levels
        if isinstance(lvl, dict)
        and abs(lvl.get("price", 0) - absolute_extreme) / pip <= 50
        and lvl.get("timeframe") in ("d1", "4h", "weekly", "w1")
    ]

    if not extreme_levels:
        if debug:
            print(
                f"  [SW4] {symbol}: no D1/4H S/R within 50p of extreme "
                f"{absolute_extreme:.5f} — raw swing level not enough"
            )
        return None

    # ── Step 5: 4H CHoCH in reversal direction — REQUIRED ────────────────────
    choch_4h = (state.get("4h") or {}).get("choch") or []
    bos_4h   = (state.get("4h") or {}).get("bos")   or []
    choch_1h = (state.get("1h") or {}).get("choch") or []
    bos_1h   = (state.get("1h") or {}).get("bos")   or []

    has_4h_choch = _recent_signal(choch_4h, direction, max_bars=12)
    has_1h_choch = _recent_signal(choch_1h, direction, max_bars=6)
    has_1h_bos   = _recent_signal(bos_1h,   direction, max_bars=6)

    if not has_4h_choch:
        if debug:
            print(f"  [SW4] {symbol}: no 4H {direction} CHoCH — required for macro reversal entry")
        return None

    if not (has_1h_choch or has_1h_bos):
        if debug:
            print(f"  [SW4] {symbol}: no 1H {direction} signal — need CHoCH or BOS as entry trigger")
        return None

    # ── Step 6: Scoring ───────────────────────────────────────────────────────
    score   = 0
    reasons = []

    if dist_to_extreme <= 15:
        score += 35
        reasons.append(f"price {dist_to_extreme:.0f}p from D1 extreme (very close)")
    elif dist_to_extreme <= 30:
        score += 25
        reasons.append(f"price {dist_to_extreme:.0f}p from D1 extreme (close)")
    else:
        score += 15
        reasons.append(f"price {dist_to_extreme:.0f}p from D1 extreme (near)")

    if len(extreme_levels) >= 2:
        score += 15
        reasons.append(f"{len(extreme_levels)} D1/4H S/R levels stacked at extreme")
    else:
        score += 10
        reasons.append("D1/4H S/R confluence at extreme")

    score   += 25
    reasons.append("4H CHoCH confirmed")

    if has_1h_choch:
        score += 20
        reasons.append("1H CHoCH entry trigger")
    elif has_1h_bos:
        score += 15
        reasons.append("1H BOS entry trigger")

    if bias_4h == direction:
        score += 10
        reasons.append("4H bias already flipped")

    if state.get("tradeable_session"):
        score += 10
        reasons.append("London/NY session")

    # ── Correlated-pair confirmation bonus ────────────────────────────────────
    # If a paired symbol already fired a SW4 in the opposite USD direction
    # within the last 10 minutes, the macro theme is confirmed from both sides.
    corr = _CORR_MAP.get(symbol)
    if corr:
        corr_symbol, corr_dir = corr
        corr_entry = _pending_sw4.get(corr_symbol)
        if (
            corr_entry
            and corr_entry[0] == corr_dir
            and ((state.get("reference_ts") or _time.time()) - corr_entry[1]) < SW4_CORR_EXPIRY_SECS
        ):
            score += 10
            reasons.append(
                f"{corr_symbol} SW4 {'BUY' if corr_dir == 'bullish' else 'SELL'} "
                f"confirms macro USD theme (+10)"
            )
            if debug:
                print(f"  [SW4] {symbol}: correlated pair {corr_symbol} confirms → +10")

    if debug:
        print(
            f"  [SW4] {symbol}: score={score} dir={trade_type} "
            f"dist={dist_to_extreme:.1f}p extreme={absolute_extreme:.5f} "
            f"reasons={reasons}"
        )

    if score < SW4_MIN_CONFIDENCE:
        if debug:
            print(f"  [SW4] {symbol}: score {score} < SW4_MIN_CONFIDENCE {SW4_MIN_CONFIDENCE} — skip")
        return None

    # ── Step 7: SL — just beyond the absolute extreme ─────────────────────────
    # The extreme is THE invalidation level. If price exceeds it, the macro
    # reversal thesis is wrong. SL sits just beyond it with a small buffer.
    # This SL is almost never hit during a genuine macro reversal — it would
    # require an unexpected event to push through a multi-month structural level.
    buf = SW4_SL_BUFFER_PIPS * pip

    if trade_type == "SELL":
        sl = round(absolute_extreme + buf, 5)
        if sl <= price:
            if debug:
                print(f"  [SW4] {symbol}: SL {sl} not above entry {price} for SELL — skip")
            return None
    else:
        sl = round(absolute_extreme - buf, 5)
        if sl >= price:
            if debug:
                print(f"  [SW4] {symbol}: SL {sl} not below entry {price} for BUY — skip")
            return None

    sl_pips = abs(price - sl) / pip
    if sl_pips < 10:
        if debug:
            print(f"  [SW4] {symbol}: SL too tight ({sl_pips:.1f}p) — skip")
        return None

    # ── Step 8: TP — Fibonacci retracement of the FULL macro swing ────────────
    # Primary:   38.2% retracement — institutional first profit-taking zone
    # Extension: 61.8% retracement — if primary TP doesn't meet R:R minimum
    sl_dist = abs(price - sl)

    if trade_type == "SELL":
        tp_primary   = round(swing_hi - SW4_TP_PRIMARY_FIB   * macro_range, 5)
        tp_extension = round(swing_hi - SW4_TP_EXTENSION_FIB * macro_range, 5)
        if tp_primary >= price:
            tp_primary = None
        if tp_extension >= price:
            tp_extension = None
    else:
        tp_primary   = round(swing_lo + SW4_TP_PRIMARY_FIB   * macro_range, 5)
        tp_extension = round(swing_lo + SW4_TP_EXTENSION_FIB * macro_range, 5)
        if tp_primary <= price:
            tp_primary = None
        if tp_extension <= price:
            tp_extension = None

    tp  = None
    rr  = 0.0
    fib = "—"

    if tp_primary and sl_dist > 0:
        rr_primary = round(abs(tp_primary - price) / sl_dist, 2)
        if rr_primary >= SW4_MIN_RR:
            tp  = tp_primary
            rr  = rr_primary
            fib = "38.2%"

    if tp is None and tp_extension and sl_dist > 0:
        rr_ext = round(abs(tp_extension - price) / sl_dist, 2)
        if rr_ext >= SW4_MIN_RR:
            tp  = tp_extension
            rr  = rr_ext
            fib = "61.8%"

    if tp is None:
        if debug:
            print(
                f"  [SW4] {symbol}: R:R too low even at 61.8% retracement "
                f"— entry too far from extreme ({dist_to_extreme:.1f}p), "
                f"need price closer to swing extreme"
            )
        return None

    # ── Step 9: Duplicate guard (before final return) ─────────────────────────
    swing_key = (round(swing_hi, 5), round(swing_lo, 5))
    if _fired_swings.get(symbol) == swing_key:
        if debug:
            print(f"  [SW4] {symbol}: already fired on this D1 swing — skip")
        return None

    if rr < SW4_MIN_RR:
        if debug:
            print(f"  [SW4] {symbol}: R:R {rr:.2f} < SW4_MIN_RR {SW4_MIN_RR} — skip")
        return None

    _pending_sw4[symbol]  = (direction, state.get("reference_ts") or _time.time())
    _fired_swings[symbol] = swing_key

    macro_pips = round(macro_range / pip)
    return {
        "trade":          True,
        "type":           trade_type,
        "strategy":       "SW4 Macro Exhaustion Reversal",
        "confidence":     min(score, 100),
        "reason": (
            f"D1 {bias_d1} extreme @ {absolute_extreme:.5f} | "
            f"{dist_to_extreme:.1f}p from extreme | "
            f"macro swing {macro_pips}p | "
            f"SL beyond extreme+{SW4_SL_BUFFER_PIPS}p | "
            f"TP={fib} retracement | "
            f"4H CHoCH + {'1H CHoCH' if has_1h_choch else '1H BOS'} | "
            f"4H={bias_4h} | "
            + " | ".join(reasons)
        ),
        "entry":          round(price, 5),
        "sl":             sl,
        "tp":             tp,
        "rr":             rr,
        "swing_hi":       swing_hi,
        "swing_lo":       swing_lo,
    }
