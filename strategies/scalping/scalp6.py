"""
Strategy 6 — Asian Range Boundary Reaction
===========================================
Targets institutional liquidity raids at Asian session range boundaries.
Every day the Asian session (00:00–09:00 UTC) builds a range. Institutions
raid the stops beyond those boundaries before reversing. S6 catches that.

Hard gates (any failure = immediate exit, evaluated in order):
  1. Asian session active (00:00–09:00 UTC, includes Tokyo-London overlap)
  2. Asian range established (high + low, ≥ 5p wide)
  3. Price within 12 pips of the nearest Asian boundary
  4. SESSION COOLDOWN — S6 has not already fired on this boundary today
  5. SWEEP confirmation — wick ≥ 2p beyond boundary, close back inside
  6. 4H must NOT strongly trend against the reversal direction
  7. 1H must support reversal direction (neutral ok, against = reject)
  8. S/R level OR zone within 10p of boundary (raw Asian level alone = reject)
  9. 5M CHoCH in reversal direction within 30 min, body ≥ 60%

Scoring (max 140, min to fire 75):
  Session window           25   (required)
  Price proximity          25 (≤5p) / 15 (≤12p)
  1H alignment             20 (aligned) / 10 (neutral)
  S/R confluence           20
  Zone bonus               10   (on top of S/R)
  4H alignment bonus       10
  First-test bonus          5   (no prior sweep of this boundary in lookback)
  Sweep freshness bonus     5   (sweep < 10 min ago)
  5M CHoCH confirm         20

Session cooldown (module-level, self-contained):
  Tracks per (symbol, boundary_side) per UTC date.
  Resets automatically at midnight UTC (new Asian session = new day).
  No changes required in dashboard_server.py or __init__.py.
"""

import sys, os, math, time as _time
from datetime import datetime, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import config


# ─────────────────────────────────────────────────────────────────────────────
# Session cooldown — module-level, persists across cycles, resets at midnight UTC
# Keys:   (symbol, boundary_side)  e.g. ("USD/JPY", "high")
# Values: "YYYY-MM-DD" UTC date string of the last fire
# ─────────────────────────────────────────────────────────────────────────────

import json as _json
import os as _os

_COOLDOWN_FILE = _os.path.join(_os.path.dirname(__file__), "..", "..", "data", "s6_cooldown.json")
_fired_today: dict = {}
_cooldown_lock = __import__('threading').Lock()


def _load_cooldown() -> None:
    global _fired_today
    try:
        if _os.path.exists(_COOLDOWN_FILE):
            with open(_COOLDOWN_FILE, "r") as f:
                _fired_today = _json.load(f)
    except Exception:
        _fired_today = {}


_backtest_mode: bool = False   # set True by backtest_engine to prevent disk writes

def _save_cooldown() -> None:
    if _backtest_mode:             # Fix B10: do not pollute live cooldown file during backtest
        return
    try:
        with open(_COOLDOWN_FILE, "w") as f:
            _json.dump(_fired_today, f)
    except Exception:
        pass


_load_cooldown()  # load once at import time — not on every cycle


def _already_fired(symbol: str, boundary_side: str,
                   reference_ts: float | None = None) -> bool:
    key  = f"{symbol}|{boundary_side}"
    _now = (datetime.fromtimestamp(reference_ts, tz=timezone.utc)
            if reference_ts else datetime.now(timezone.utc))
    today = _now.strftime("%Y-%m-%d")
    with _cooldown_lock:
        return _fired_today.get(key) == today


def _mark_fired(symbol: str, boundary_side: str,
                reference_ts: float | None = None) -> None:
    key  = f"{symbol}|{boundary_side}"
    _now = (datetime.fromtimestamp(reference_ts, tz=timezone.utc)
            if reference_ts else datetime.now(timezone.utc))
    today = _now.strftime("%Y-%m-%d")
    with _cooldown_lock:
        # NOTE: do NOT call _load_cooldown() here — reloading from disk would
        # overwrite the in-memory reset that backtest_engine performs at the
        # start of each run, causing cooldown state from prior live sessions to
        # bleed into backtest results.  In-memory dict is the source of truth;
        # we only persist to disk for cross-process durability.  (Fix B2)
        _fired_today[key] = today
        _save_cooldown()


# ─────────────────────────────────────────────────────────────────────────────
# Asian session window (DST-aware — Tokyo does not observe DST, UTC is fixed)
# ─────────────────────────────────────────────────────────────────────────────

def _in_asian_session(reference_ts: float | None = None) -> bool:
    """
    00:00–09:00 UTC.  Upper bound 09:00 captures late Asian boundary
    reactions during the Tokyo-London overlap hour.
    """
    now_utc = (datetime.fromtimestamp(reference_ts, tz=timezone.utc)
               if reference_ts else datetime.now(timezone.utc))
    mins    = now_utc.hour * 60 + now_utc.minute
    return 0 <= mins < 9 * 60


# ─────────────────────────────────────────────────────────────────────────────
# Asian range validation
# ─────────────────────────────────────────────────────────────────────────────

def _get_asian_range(state: dict) -> tuple:
    """
    Returns (asia_high, asia_low, is_established).
    is_established only when both high+low exist and range is ≥ 5p wide.
    """
    # Fix B13: state["asia_range"] is explicitly None when unset; "or {}" handles that
    asia      = state.get("asia_range") or {}
    asia_high = asia.get("high")
    asia_low  = asia.get("low")

    if not asia_high or not asia_low:
        return None, None, False

    pip = config.get_symbol_cfg(state.get("symbol"))["pip_size"]
    if (asia_high - asia_low) < 5 * pip:
        return asia_high, asia_low, False

    return asia_high, asia_low, True


# ─────────────────────────────────────────────────────────────────────────────
# Sweep confirmation + repeat-test detection
# ─────────────────────────────────────────────────────────────────────────────

def _swept_boundary(candles_5m: list, boundary: float,
                    direction: str, pip: float,
                    min_sweep_pips: float = 2.0,
                    lookback: int = 12) -> tuple:
    """
    Returns (sweep_candle | None, is_first_test: bool).

    sweep_candle — most recent candle where:
        bearish (sell at Asian HIGH):
            high >= boundary + 2p  AND  close < boundary
        bullish (buy at Asian LOW):
            low  <= boundary - 2p  AND  close > boundary

    is_first_test — True when no earlier sweep candle exists in the
        lookback window. Counts prior sweep candles to detect repeat raids.
    """
    threshold    = min_sweep_pips * pip
    sweep_found  = None
    prior_sweeps = 0

    for c in reversed(candles_5m[-lookback:]):
        if not isinstance(c, dict):
            continue
        h  = c.get("high",  0)
        l  = c.get("low",   0)
        cl = c.get("close", 0)

        is_sweep = False
        if direction == "bearish":
            is_sweep = (h >= boundary + threshold and cl < boundary)
        elif direction == "bullish":
            is_sweep = (l <= boundary - threshold and cl > boundary)

        if is_sweep:
            if sweep_found is None:
                sweep_found = c
            else:
                prior_sweeps += 1

    return sweep_found, (prior_sweeps == 0)


# ─────────────────────────────────────────────────────────────────────────────
# Structural confluence — S/R or zone at boundary (HARD GATE)
# ─────────────────────────────────────────────────────────────────────────────

def _boundary_confluence(boundary: float, state: dict,
                         s5m: dict, s15m: dict, pip: float,
                         threshold_pips: float = 10.0) -> tuple:
    """
    Returns (score, description).
      S/R level within threshold  → 20 pts
      Zone within threshold       → 10 pts
      Nothing                     → (0, "")  — caller uses as hard gate.
    """
    threshold = threshold_pips * pip

    for lvl in (state.get("sr_levels") or []):
        if not isinstance(lvl, dict):
            continue
        lp = lvl.get("price")
        if lp and abs(boundary - lp) <= threshold:
            return 20, f"S/R@{lp:.5f}"

    zones_5m  = s5m.get("zones")  or []
    zones_15m = s15m.get("zones") or []
    zones_1h  = state.get("1h", {}).get("zones") or []
    if not isinstance(zones_5m,  list): zones_5m  = []
    if not isinstance(zones_15m, list): zones_15m = []
    if not isinstance(zones_1h,  list): zones_1h  = []

    for zone in zones_5m + zones_15m + zones_1h:
        if not isinstance(zone, dict):
            continue
        top    = zone.get("top")    or 0
        bottom = zone.get("bottom") or 0
        if top == 0 and bottom == 0:
            continue
        if (bottom - threshold) <= boundary <= (top + threshold):
            return 10, f"zone[{bottom:.5f}-{top:.5f}]"

    return 0, ""


# ─────────────────────────────────────────────────────────────────────────────
# 5M CHoCH — Asian body filter (≥ 60 %, stricter than London / NY 50 %)
# ─────────────────────────────────────────────────────────────────────────────

def _find_asian_choch(s5m: dict, direction: str,
                      candles_5m: list,
                      max_age_secs: int = 1800,
                      now_sec: int = 0) -> dict | None:
    """
    Most recent 5M CHoCH matching direction, within 30 min, body ≥ 60%.
    Asian micro-structure produces more fake breaks — tighter body filter.
    """
    now_sec  = now_sec or int(_time.time())
    choch_5m = s5m.get("choch", [])
    if not isinstance(choch_5m, list):
        return None

    for c in sorted(choch_5m, key=lambda x: x.get("time", 0), reverse=True):
        if not isinstance(c, dict):
            continue
        if c.get("direction") != direction:
            continue
        if (now_sec - c.get("time", now_sec)) > max_age_secs:
            continue

        c_time = c.get("time")
        candle = next((k for k in candles_5m if k.get("time") == c_time), None)

        if candle is None:
            return None  # cannot verify body strength — reject to avoid weak confirmation

        rng  = candle.get("high", 0) - candle.get("low", 0)
        body = abs(candle.get("close", 0) - candle.get("open", 0))

        if rng > 0 and (body / rng) >= 0.60:
            return c

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Main strategy entry point
# ─────────────────────────────────────────────────────────────────────────────

def check(state: dict, debug: bool = False) -> dict | None:
    if not isinstance(state, dict):
        return None

    bias  = state.get("bias", {})
    price = state.get("current_price")
    s5m   = state.get("5m",  {})
    s15m  = state.get("15m", {})

    if not price or not isinstance(price, (int, float)) or not math.isfinite(price):
        return None

    symbol  = state.get("symbol", "")
    pip     = config.get_symbol_cfg(symbol)["pip_size"]
    broker_ts = config.get_broker_ts(state)
    now_sec   = broker_ts

    if not _in_asian_session(reference_ts=broker_ts):
        if debug: print("    [S6] skip: outside Asian session (00:00–09:00 UTC)")
        return None

    # ── Gate 2: Asian range established ──────────────────────────────────
    asia_high, asia_low, range_ok = _get_asian_range(state)
    if not range_ok:
        if debug: print("    [S6] skip: Asian range not established (need high+low, ≥5p wide)")
        return None

    # ── Gate 3: Price proximity ───────────────────────────────────────────
    MAX_PIPS  = 12
    dist_high = abs(price - asia_high) / pip
    dist_low  = abs(price - asia_low)  / pip

    if dist_high <= dist_low:
        boundary, direction, trade_type, dist_pips = asia_high, "bearish", "SELL", dist_high
        boundary_side = "high"
    else:
        boundary, direction, trade_type, dist_pips = asia_low,  "bullish", "BUY",  dist_low
        boundary_side = "low"

    if dist_pips > MAX_PIPS:
        if debug:
            print(f"    [S6] skip: price {dist_pips:.1f}p from boundary "
                  f"(H={asia_high:.5f} L={asia_low:.5f}) — need ≤{MAX_PIPS}p")
        return None

    # ── Gate 4: Session cooldown ──────────────────────────────────────────
    if _already_fired(symbol, boundary_side, reference_ts=broker_ts):
        if debug:
            print(f"    [S6] skip: already fired on {symbol} Asian {boundary_side} today "
                  f"— cooldown active until midnight UTC")
        return None

    # ── Gate 5: Sweep confirmation (liquidity raid) ───────────────────────
    candles_5m             = s5m.get("candles", [])
    sweep_candle, is_first = _swept_boundary(
        candles_5m, boundary, direction, pip, min_sweep_pips=2.0, lookback=12
    )

    if sweep_candle is None:
        if debug:
            print(f"    [S6] skip: no sweep of Asian {boundary_side} @ {boundary:.5f} "
                  f"— need wick ≥2p beyond boundary then close back inside")
        return None

    sweep_wick     = (sweep_candle.get("high", boundary) if direction == "bearish"
                      else sweep_candle.get("low",  boundary))
    sweep_depth    = abs(sweep_wick - boundary) / pip
    sweep_time     = sweep_candle.get("time", 0)
    sweep_age_secs = now_sec - sweep_time if sweep_time else 9999
    sweep_age_min  = round(sweep_age_secs / 60)

    if debug:
        print(f"    [S6] sweep ok: wick={sweep_wick:.5f} ({sweep_depth:.1f}p) "
              f"{sweep_age_min}min ago  first_test={is_first}")

    # ── Gate 6: 4H contradiction guard ───────────────────────────────────
    b4h = bias.get("4h", "neutral")
    b1h = bias.get("1h", "neutral")

    if direction == "bullish" and b4h == "bearish":
        if debug: print("    [S6] skip: 4H bearish — cannot buy at Asian low against HTF trend")
        return None
    if direction == "bearish" and b4h == "bullish":
        if debug: print("    [S6] skip: 4H bullish — cannot sell at Asian high against HTF trend")
        return None

    # ── Gate 7: 1H alignment ─────────────────────────────────────────────
    if direction == "bullish" and b1h == "bearish":
        if debug: print("    [S6] skip: 1H bearish contradicts BUY at Asian low")
        return None
    if direction == "bearish" and b1h == "bullish":
        if debug: print("    [S6] skip: 1H bullish contradicts SELL at Asian high")
        return None

    align_score = 20 if (
        (direction == "bullish" and b1h == "bullish") or
        (direction == "bearish" and b1h == "bearish")
    ) else 10

    # ── Gate 8: Structural confluence at boundary (HARD GATE) ────────────
    confluence_score, confluence_desc = _boundary_confluence(
        boundary, state, s5m, s15m, pip, threshold_pips=10.0
    )

    if confluence_score == 0:
        if debug:
            print(f"    [S6] skip: no S/R or zone within 10p of boundary @ {boundary:.5f} "
                  f"— raw Asian levels insufficient")
        return None

    zone_bonus = 0
    if confluence_score == 20:
        zthresh = 10 * pip
        for zone in (s5m.get("zones") or []) + (s15m.get("zones") or []):
            if not isinstance(zone, dict): continue
            top    = zone.get("top")    or 0
            bottom = zone.get("bottom") or 0
            if top == 0 and bottom == 0: continue
            if (bottom - zthresh) <= boundary <= (top + zthresh):
                zone_bonus = 10
                break

    # ── Gate 9: 5M CHoCH (body ≥ 60%, within 30 min) ─────────────────────
    choch_event = _find_asian_choch(s5m, direction, candles_5m, max_age_secs=1800,
                                    now_sec=now_sec)

    if choch_event is None:
        if debug:
            print(f"    [S6] skip: no {direction} 5M CHoCH with body≥60% within 30 min")
        return None

    choch_age_min = round((now_sec - choch_event.get("time", now_sec)) / 60)

    # ── Scoring ───────────────────────────────────────────────────────────
    session_score     = 25
    prox_score        = 25 if dist_pips <= 5 else 15
    htf_bonus         = 10 if (
                            (direction == "bullish" and b4h == "bullish") or
                            (direction == "bearish" and b4h == "bearish")
                        ) else 0
    first_test_bonus  = 5 if is_first else 0
    sweep_fresh_bonus = 5 if sweep_age_secs <= 600 else 0
    confirm_score     = 20

    total_score = (session_score + prox_score + align_score +
                   confluence_score + zone_bonus + htf_bonus +
                   first_test_bonus + sweep_fresh_bonus + confirm_score)

    if debug:
        print(
            f"    [S6] {direction} @ Asian {boundary_side} {boundary:.5f} | "
            f"sess={session_score} "
            f"prox={prox_score}({dist_pips:.1f}p) "
            f"1H={align_score}({b1h}) "
            f"conf={confluence_score}({confluence_desc}) "
            f"zone={zone_bonus} "
            f"4H={htf_bonus}({b4h}) "
            f"first={first_test_bonus}({'yes' if is_first else 'no'}) "
            f"fresh={sweep_fresh_bonus}({sweep_age_min}min) "
            f"choch={confirm_score}({choch_age_min}min ago) "
            f"→ TOTAL {total_score}/140"
        )

    if total_score < max(75, config.MIN_CONFIDENCE):
        if debug: print(f"    [S6] skip: score {total_score} < {max(75, config.MIN_CONFIDENCE)} minimum")
        return None

    # ── SL / TP ───────────────────────────────────────────────────────────
    buf = config.SWEEP_SL_BUFFER_PIPS * pip  # 8 pips beyond sweep wick extreme

    if direction == "bullish":
        sl = sweep_wick - buf
        if sl >= price:
            if debug: print("    [S6] skip: SL not below entry for BUY")
            return None
    else:
        sl = sweep_wick + buf
        if sl <= price:
            if debug: print("    [S6] skip: SL not above entry for SELL")
            return None

    sl_dist = abs(price - sl)

    if sl_dist < config.MIN_SL_PIPS * pip:
        if debug:
            print(f"    [S6] REJECTED: SL too tight ({sl_dist/pip:.1f}p < {config.MIN_SL_PIPS}p min)")
        return None

    # FIX B-FIB-TP: use scalp_extension_tp() which caps the fib TP at SCALP_MAX_RR.
    # Raw fib_extension_tp() uses D1/4H swing ranges (400-2000 pips for USDJPY),
    # making TP unreachable for a scalp with a 15-40 pip SL (actual RR 10-40:1).
    # Cap at 3× SL; fall back to TARGET_RR × SL when fib is too far.
    _fib = config.scalp_extension_tp(state, direction, price, sl_dist)
    tp   = _fib if _fib is not None else (
               (price + sl_dist * config.TARGET_RR) if direction == "bullish"
               else (price - sl_dist * config.TARGET_RR))

    rr      = round(abs(tp - price) / sl_dist, 2) if sl_dist > 0 else config.TARGET_RR
    total_cost_pips = config.get_total_cost_pips(symbol)
    spread_pips     = config.get_spread_pips(symbol)   # kept for logging
    cost_amount     = total_cost_pips * pip
    net_tp_dist     = max(abs(tp - price) - cost_amount, 0.0)
    net_sl_dist     = sl_dist + cost_amount
    net_rr          = round(net_tp_dist / net_sl_dist, 2) if net_sl_dist > 0 else 0

    if abs(tp - price) / sl_dist < 2.0:
        if debug: print("    [S6] REJECTED: raw RR < 2.0")
        return None

    if net_rr < config.NET_MIN_RR:
        if debug: print(f"    [S6] REJECTED: net RR {net_rr} < {config.NET_MIN_RR} minimum")
        return None
    
    

    reason = (
        f"Asian {boundary_side}={boundary:.5f} | "
        f"sweep wick={sweep_wick:.5f} ({sweep_depth:.1f}p raid, {sweep_age_min}min ago) | "
        f"first_test={'yes' if is_first else 'no'} | "
        f"dist={dist_pips:.1f}p | "
        f"1H={b1h} 4H={b4h} | "
        f"confluence={confluence_desc}{'+zone' if zone_bonus else ''} | "
        f"5M CHoCH {choch_age_min}min ago | "
        f"spread={spread_pips}pip netRR={net_rr} | "
        f"score={total_score}/140"
    )

    _mark_fired(symbol, boundary_side, reference_ts=broker_ts)

    return {
        "trade":       True,
        "type":        trade_type,
        "boundary_side": boundary_side,
        "confidence":  total_score,
        "strategy":    "Asian Range Boundary Reaction",
        "reason":      reason,
        "entry":       round(price, 5),
        "sl":          round(sl,    5),
        "tp":          round(tp,    5),
        "rr":          rr,
        "net_rr":      net_rr,
        "spread_pips": spread_pips,
        "total_cost_pips":  total_cost_pips,
    }