"""
Strategy 4 — Volatility Compression Breakout
=============================================
Market compresses into tight structure → liquidity builds →
displacement breakout occurs → retest → micro confirmation → enter.

Works best during:
  - London open (Asian range break)
  - NY open (London consolidation break)
  - Post-news continuation
  - Intraday range compression releases

Timeframe logic:
  4H + 1H  — bias direction
  5M       — compression, displacement, FVG retest, micro BOS/CHoCH

Scoring breakdown (max 115):
  HTF alignment        — up to 25  (4H+1H agree=25, 1H only=12, conflict=reject)
  Compression on 5M    — 20        (ranges contracting vs 15-candle baseline)
  Liquidity pool       — 15        (proper swing high/low clusters as draw)
  Displacement candle  — 20        (body ≥ 70% AND range ≥ 1.5× avg, within 1h)
  Proximity to origin  — up to 15  (how close price still is to breakout zone)
  FVG + micro confirm  — 10        (FVG retest + 5M BOS/CHoCH in direction)
  Session timing       — 10        (London/NY=10, Asia=immediate reject)

Hard gates (any failure = skip, evaluated in order):
  1. London or NY session only
  2. Clear HTF bias (1H minimum, 4H preferred)
  3. Compression confirmed (recent range ≤ 65% of baseline)
  4. Displacement candle (body≥70%, range≥1.5×avg, within 1h, breaks zone)
  5. Zone/OB/S/R anchor within 15 pips of breakout edge
  6. Price within 30 pips of breakout edge (not chasing)
  7. Micro BOS/CHoCH confirmation (FVG retest bonus if also in FVG)

Minimum score: 80
Entry = market price
SL    = beyond compression zone edge + buffer
TP    = 2R from SL
"""

import sys, os, math, time as _time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import config


# ─────────────────────────────────────────────────────────────────────────────
# Compression detection (FIX 5: larger 15-candle baseline)
# ─────────────────────────────────────────────────────────────────────────────

def _detect_compression(candles: list,
                         compress_window: int = 10,
                         baseline_window: int = 15) -> dict | None:
    needed = compress_window + baseline_window
    if len(candles) < needed:
        return None

    recent   = candles[-compress_window:]
    baseline = candles[-(compress_window + baseline_window):-compress_window]

    def avg_range(cs):
        return sum(c.get("high", 0) - c.get("low", 0) for c in cs) / len(cs)

    baseline_avg = avg_range(baseline)
    if baseline_avg == 0:
        return None

    recent_avg = avg_range(recent)
    ratio      = recent_avg / baseline_avg

    return {
        "confirmed":     ratio <= 0.70,
        "high":          max(c.get("high", 0) for c in recent),
        "low":           min(c.get("low", 0) for c in recent),
        "avg_range":     recent_avg,
        "baseline_avg":  baseline_avg,
        "ratio":         round(ratio, 3),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Liquidity pool detection — proper swing highs/lows (FIX 4 + FIX 1)
# ─────────────────────────────────────────────────────────────────────────────

def _find_swing_highs(candles: list, pip: float,
                       tolerance_pips: float = 3.0,
                       min_spacing: int = 3) -> float | None:
    tolerance = tolerance_pips * pip
    swing_highs = []
    for i in range(1, len(candles) - 1):
        if (candles[i].get("high", 0) > candles[i - 1].get("high", 0) and
                candles[i].get("high", 0) > candles[i + 1].get("high", 0)):
            if not swing_highs or (i - swing_highs[-1][0]) >= min_spacing:
                swing_highs.append((i, candles[i].get("high", 0)))

    if len(swing_highs) < 2:
        return None

    levels = [sh[1] for sh in swing_highs]
    for ref in levels:
        cluster = [v for v in levels if abs(v - ref) <= tolerance]
        if len(cluster) >= 2:
            return round(sum(cluster) / len(cluster), 5)

    return None


def _find_swing_lows(candles: list, pip: float,
                      tolerance_pips: float = 3.0,
                      min_spacing: int = 3) -> float | None:
    tolerance = tolerance_pips * pip
    swing_lows = []
    for i in range(1, len(candles) - 1):
        if (candles[i].get("low", 0) < candles[i - 1].get("low", 0) and
                candles[i].get("low", 0) < candles[i + 1].get("low", 0)):
            if not swing_lows or (i - swing_lows[-1][0]) >= min_spacing:
                swing_lows.append((i, candles[i].get("low", 0)))

    if len(swing_lows) < 2:
        return None

    levels = [sl[1] for sl in swing_lows]
    for ref in levels:
        cluster = [v for v in levels if abs(v - ref) <= tolerance]
        if len(cluster) >= 2:
            return round(sum(cluster) / len(cluster), 5)

    return None


def _detect_liquidity_pools(candles: list, direction: str,
                              pip: float) -> dict:
    pool_window = candles[-25:]

    if direction == "bullish":
        level = _find_swing_highs(pool_window, pip, tolerance_pips=3.0, min_spacing=3)
        return {"pool_ok": level is not None, "pool_level": level}
    else:
        level = _find_swing_lows(pool_window, pip, tolerance_pips=3.0, min_spacing=3)
        return {"pool_ok": level is not None, "pool_level": level}


# ─────────────────────────────────────────────────────────────────────────────
# Displacement breakout detection (FIX 2 + FIX 3)
# ─────────────────────────────────────────────────────────────────────────────

def _detect_displacement(candles: list, comp_high: float, comp_low: float,
                          pip: float, avg_range: float,
                          max_age_secs: int = 3600, now_sec: int = 0) -> dict | None:
    now          = now_sec or int(_time.time())

    min_range    = avg_range * 1.5

    for c in reversed(candles[-8:]):
        c_time = c.get("time", 0)
        if (now - c_time) > max_age_secs:
            continue

        o    = c.get("open",  0)
        h    = c.get("high",  0)
        l    = c.get("low",   0)
        cl   = c.get("close", 0)
        rng  = h - l
        body = abs(cl - o)

        if rng <= 0:
            continue
        if rng < min_range:
            continue
        if (body / rng) < 0.70:
            continue

        if cl > o and cl > comp_high + pip:
            return {"direction": "bullish", "candle": c,
                    "time": c_time, "body_pct": round(body / rng, 3),
                    "range_pips": round(rng / pip, 1)}

        if cl < o and cl < comp_low - pip:
            return {"direction": "bearish", "candle": c,
                    "time": c_time, "body_pct": round(body / rng, 3),
                    "range_pips": round(rng / pip, 1)}

    return None


# ─────────────────────────────────────────────────────────────────────────────
# FVG retest + micro BOS/CHoCH confirmation (FIX 6)
# ─────────────────────────────────────────────────────────────────────────────

def _detect_fvg(candles: list, direction: str, price: float, pip: float = 0.0001) -> bool:
    if len(candles) < 3:
        return False

    for i in range(1, min(len(candles) - 1, 6)):
        candle_a = candles[-(i + 2)]
        candle_c = candles[-i]

        if direction == "bullish":
            gap_bottom = candle_a.get("high", 0)
            gap_top    = candle_c.get("low",   0)
            if gap_top > gap_bottom and (gap_top - gap_bottom) >= 3 * pip and gap_bottom <= price <= gap_top:
                return True

        elif direction == "bearish":
            gap_top    = candle_a.get("low",  0)
            gap_bottom = candle_c.get("high",  0)
            if gap_bottom < gap_top and (gap_top - gap_bottom) >= 3 * pip and gap_bottom <= price <= gap_top:
                return True

    return False


def _detect_micro_confirmation(s5m: dict, direction: str,
    max_age_secs: int = 1800, now_sec: int = 0) -> dict | None:
    now      = now_sec or int(_time.time())
    bos_5m   = s5m.get("bos",   [])
    choch_5m = s5m.get("choch", [])

    for events in (choch_5m, bos_5m):
        for e in sorted(events, key=lambda x: x.get("time", 0), reverse=True)[:6]:
            if not isinstance(e, dict):
                continue
            if (e.get("direction") == direction and
                    (now - e.get("time", 0)) <= max_age_secs):
                return e

    return None

def _structure_holds(candles_5m: list, confirm_time: int, direction: str) -> bool:
    post = [c for c in candles_5m if c.get("time", 0) > confirm_time]
    if len(post) < 2:
        return True
    anchor_close = post[0].get("close", 0)
    subsequent   = post[1:]
    if direction == "bullish":
        return not any(c.get("low", 0) < anchor_close for c in subsequent)
    else:
        return not any(c.get("high", float("inf")) > anchor_close for c in subsequent)


# ─────────────────────────────────────────────────────────────────────────────
# Proximity scoring (FIX 7)
# ─────────────────────────────────────────────────────────────────────────────

def _proximity_score(price: float, comp_high: float, comp_low: float,
                      direction: str, pip: float) -> int:
    if direction == "bullish":
        dist_pips = (price - comp_high) / pip
    else:
        dist_pips = (comp_low - price) / pip

    if dist_pips < 0:
        return 0

    if dist_pips <= 5:
        return 15
    elif dist_pips <= 15:
        return 10
    elif dist_pips <= 30:
        return 5
    else:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Zone / OB / S/R anchor at breakout edge — structural gate
# ───────────────────────────────────────────────────────────────────────────

def _ob_at_level(candles: list, level: float, pip: float,
                 threshold_pips: float = 15.0) -> bool:
    """
    Returns True if an unmitigated Order Block (bullish or bearish) sits
    within threshold_pips of `level`.
      Bullish OB: bearish candle followed by an impulsive up-break.
      Bearish OB: bullish candle followed by an impulsive down-break.
    Mitigation: no subsequent candle closes through the OB mid-point.
    """
    n         = len(candles)
    threshold = threshold_pips * pip
    min_size  = 3 * pip
    if n < 6:
        return False
    try:
        for i in range(1, n - 3):
            c        = candles[i]
            lookback = candles[max(0, i - 8):i]
            avg_rng  = (
                sum(x["high"] - x["low"] for x in lookback) / len(lookback)
                if lookback else 0
            )
            if avg_rng == 0:
                continue
            fwd = candles[i + 1: min(i + 5, n)]

            if c["close"] < c["open"] and (c["high"] - c["low"]) >= min_size:
                future_high = max((x["close"] for x in fwd), default=0)
                if future_high > c["high"]:
                    brk = max(fwd, key=lambda x: x["high"] - x["low"], default=None)
                    if brk and (brk["high"] - brk["low"]) >= 1.5 * avg_rng:
                        center = (c["high"] + c["low"]) / 2
                        if abs(center - level) <= threshold:
                            ob_mid = (c["open"] + c["close"]) / 2
                            if not any(fc["close"] < ob_mid for fc in candles[i + 1:]):
                                return True

            elif c["close"] > c["open"] and (c["high"] - c["low"]) >= min_size:
                future_low = min((x["close"] for x in fwd), default=float("inf"))
                if future_low < c["low"]:
                    brk = max(fwd, key=lambda x: x["high"] - x["low"], default=None)
                    if brk and (brk["high"] - brk["low"]) >= 1.5 * avg_rng:
                        center = (c["high"] + c["low"]) / 2
                        if abs(center - level) <= threshold:
                            ob_mid = (c["open"] + c["close"]) / 2
                            if not any(fc["close"] > ob_mid for fc in candles[i + 1:]):
                                return True

    except (KeyError, TypeError):
        return False
    return False



def _has_zone_anchor(breakout_edge: float, state: dict, s5m: dict,
                     pip: float, threshold_pips: float = 15.0) -> tuple[bool, str]:
    """
    Returns (True, description) if there is a supply/demand zone, S/R level,
    or Order Block within threshold_pips of the compression zone breakout edge.

    This is the gate that gives S4 a structural reason for the breakout
    level to matter — ensuring the engine doesn't enter in open air.

    Without this gate, S4 fires whenever there is compression + a displacement
    candle, regardless of whether institutions have any reason to defend that
    specific price. With this gate, the compression must occur at a documented
    institutional level (OB, zone, or tested S/R) to qualify.

    Checks (in order):
      1. S/R levels from STRUCT.ai (state["sr_levels"])
      2. Supply/demand zones on 5M and 15M
      3. Order blocks on 5M and 15M (inert until STRUCT.ai API provides OB data)
    """
    threshold = threshold_pips * pip
    s15m      = state.get("15m", {})

    # ── 1. S/R levels ─────────────────────────────────────────────────────
    for lvl in (state.get("sr_levels") or []):
        if not isinstance(lvl, dict):
            continue
        lp = lvl.get("price") or lvl.get("level")
        if isinstance(lp, (int, float)) and abs(breakout_edge - lp) <= threshold:
            return True, f"S/R@{lp:.5f}"

    # ── 2. Supply/demand zones (5M + 15M) ─────────────────────────────────
    for tf_data in (s5m, s15m):
        zones = tf_data.get("zones") or []
        if not isinstance(zones, list):
            continue
        for zone in zones:
            if not isinstance(zone, dict):
                continue
            top    = zone.get("top")    or 0
            bottom = zone.get("bottom") or 0
            if top == 0 and bottom == 0:
                continue
            center = (top + bottom) / 2
            if abs(breakout_edge - center) <= threshold:
                return True, f"zone[{bottom:.5f}-{top:.5f}]"

    
    # ── 3. Order blocks on 5M and 15M candles
    candles_5m  = s5m.get("candles", [])
    candles_15m = s15m.get("candles", [])
    for tf_candles, tf_label in ((candles_5m, "5M-OB"), (candles_15m, "15M-OB")):
        if tf_candles and _ob_at_level(tf_candles, breakout_edge, pip, threshold_pips):
            return True, f"{tf_label}@{breakout_edge:.5f}"
    return False, ""


# ─────────────────────────────────────────────────────────────────────────────
# Main strategy entry point
# ─────────────────────────────────────────────────────────────────────────────

def check(state: dict, debug: bool = False) -> dict | None:
    if not isinstance(state, dict):
        return None

    bias  = state.get("bias", {})
    price = state.get("current_price")
    s5m   = state.get("5m", {})

    if not price or not isinstance(price, (int, float)) or not math.isfinite(price):
        return None

    sym_cfg = config.get_symbol_cfg(state.get("symbol"))
    now_sec = config.get_broker_ts(state)
    pip     = sym_cfg["pip_size"]

    # ── Step 1: Session gate — London or NY only ──────────────────────────
    sessions       = state.get("sessions", [])
    sessions_lower = [s.lower() for s in sessions]
    in_session     = any(s in sessions_lower for s in ["london", "ny", "new york"])

    if not in_session:
        if debug: print("    [S4] skip: Asia session — S4 fires at London/NY only")
        return None

    session_score = 10

    # ── Step 2: HTF bias alignment ────────────────────────────────────────
    b4h = bias.get("4h", "neutral")
    b1h = bias.get("1h", "neutral")

    if b4h == b1h and b4h in ("bullish", "bearish"):
        htf_direction = b4h
        bias_score    = 25
    elif b4h in ("neutral", "") and b1h in ("bullish", "bearish"):
        htf_direction = b1h
        bias_score    = 12
    else:
        if debug: print("    [S4] skip: no clear HTF bias or 4H/1H conflict")
        return None

    # ── Step 3: Compression detection (FIX 5: 15-candle baseline) ─────────
    candles_5m = s5m.get("candles", [])
    if len(candles_5m) < 30:
        if debug: print("    [S4] skip: not enough 5M candles")
        return None

    comp = _detect_compression(candles_5m, compress_window=10, baseline_window=15)
    if comp is None or not comp["confirmed"]:
        ratio = comp["ratio"] if comp else "N/A"
        if debug: print(f"    [S4] skip: no compression (ratio={ratio})")
        return None

    compression_score = 20
    comp_high = comp["high"]
    comp_low  = comp["low"]

    if debug:
        print(f"    [S4] compression — zone={comp_low:.5f}-{comp_high:.5f} "
              f"ratio={comp['ratio']} avg={comp['avg_range']/pip:.1f}p "
              f"baseline={comp['baseline_avg']/pip:.1f}p")

    # ── Step 4: Displacement breakout (FIX 2 + FIX 3) ────────────────────
    displacement = _detect_displacement(
        candles_5m, comp_high, comp_low, pip,
        avg_range=comp["baseline_avg"],
        max_age_secs=3600,
        now_sec=now_sec,
    )

    if displacement is None:
        if debug: print("    [S4] skip: no displacement (body≥70%, range≥1.5×avg, within 1h)")
        return None

    if displacement["direction"] != htf_direction:
        if debug:
            print(f"    [S4] skip: breakout={displacement['direction']} "
                  f"vs HTF={htf_direction}")
        return None

    displacement_score = 20

    if debug:
        print(f"    [S4] displacement: {displacement['direction']} "
              f"body={displacement['body_pct']*100:.0f}% "
              f"range={displacement['range_pips']}p")

    # ── Step 4.5: Zone / OB / S/R anchor at breakout edge — structural gate
    breakout_edge           = comp_high if htf_direction == "bullish" else comp_low
    anchor_ok, anchor_desc  = _has_zone_anchor(breakout_edge, state, s5m, pip,
                                               threshold_pips=15.0)

    if not anchor_ok:
        if debug:
            print(f"    [S4] skip: no zone/OB/S/R within 15p of breakout edge "
                  f"@ {breakout_edge:.5f} — structural anchor required")
        return None

    if debug:
        print(f"    [S4] anchor: {anchor_desc} @ breakout edge {breakout_edge:.5f}")

    # ── Step 5: Liquidity pool (FIX 1 + FIX 4) ───────────────────────────
    pool = _detect_liquidity_pools(candles_5m, htf_direction, pip)
    pool_score = 15 if pool["pool_ok"] else 0

    if debug:
        print(f"    [S4] liquidity pool: {'✓ level=' + str(pool['pool_level']) if pool['pool_ok'] else '✗'} "
              f"score={pool_score}")

    # ── Step 6: Proximity scoring (FIX 7) ────────────────────────────────
    prox_score = _proximity_score(price, comp_high, comp_low, htf_direction, pip)

    if prox_score == 0:
        if debug: print("    [S4] skip: price too far from compression zone (>30p)")
        return None

    if debug:
        if htf_direction == "bullish":
            dist = (price - comp_high) / pip
        else:
            dist = (comp_low - price) / pip
        print(f"    [S4] proximity: {dist:.1f}p from breakout edge → score={prox_score}")

    # ── Step 7: FVG retest + micro BOS/CHoCH confirmation (FIX 6) ─────────
    fvg_ok   = _detect_fvg(candles_5m, htf_direction, price, pip)
    micro_ok = _detect_micro_confirmation(s5m, htf_direction, max_age_secs=1800, now_sec=now_sec)

    if fvg_ok and micro_ok:
        confirm_score = 10
        entry_model   = "FVG-retest+BOS"
    elif fvg_ok and not micro_ok:
        if debug: print("    [S4] skip: in FVG but awaiting micro BOS/CHoCH confirmation")
        return None
    else:
        if not micro_ok:
            if debug: print("    [S4] skip: no FVG and no micro BOS/CHoCH confirmation")
            return None
        confirm_score = 0
        entry_model   = "aggressive+BOS"

    if debug:
        print(f"    [S4] confirmation: FVG={'✓' if fvg_ok else '✗'} "
              f"microBOS={'✓' if micro_ok else '✗'} entry={entry_model}")

    # ── Total score ───────────────────────────────────────────────────────
    total_score = (bias_score + compression_score + pool_score +
                   displacement_score + prox_score + confirm_score + session_score)

    if debug:
        print(f"    [S4] {htf_direction} | bias={bias_score} comp={compression_score} "
              f"pool={pool_score} disp={displacement_score} prox={prox_score} "
              f"conf={confirm_score} sess={session_score} → {total_score}")
        

    # ── GATE: Post-confirmation structure must hold ───────────────────────
    confirm_time = micro_ok.get("time", 0) if isinstance(micro_ok, dict) else 0
    if confirm_time > 0 and candles_5m:
        if not _structure_holds(candles_5m, confirm_time, htf_direction):
            if debug:
                print(f"    [S4] skip: post-BOS structure violated "
                      f"— {htf_direction} breakout already failed, waiting for fresh setup")
            return None

    if total_score < max(config.MIN_CONFIDENCE, 80):
        if debug: print(f"    [S4] skip: score {total_score} < {max(config.MIN_CONFIDENCE, 80)} (S4 local min=80)")
        return None

    # ── SL / TP ───────────────────────────────────────────────────────────
    buf        = config.SL_BUFFER_PIPS * pip
    trade_type = "BUY" if htf_direction == "bullish" else "SELL"

    if htf_direction == "bullish":
        sl = comp_low - buf
        if sl >= price:
            if debug: print("    [S4] skip: SL not below entry for BUY")
            return None
    else:
        sl = comp_high + buf
        if sl <= price:
            if debug: print("    [S4] skip: SL not above entry for SELL")
            return None

    sl_dist = abs(price - sl)
    # FIX B-FIB-TP: use scalp_extension_tp() which caps the fib TP at SCALP_MAX_RR.
    # Raw fib_extension_tp() uses D1/4H swing ranges (400-2000 pips for USDJPY),
    # making TP unreachable for a scalp with a 15-40 pip SL (actual RR 10-40:1).
    # Cap at 3× SL; fall back to TARGET_RR × SL when fib is too far.
    _fib = config.scalp_extension_tp(state, htf_direction, price, sl_dist)
    tp   = _fib if _fib is not None else (
               (price + sl_dist * config.TARGET_RR) if htf_direction == "bullish"
               else (price - sl_dist * config.TARGET_RR))
    rr      = round(abs(tp - price) / sl_dist, 2) if sl_dist > 0 else config.TARGET_RR
    sl      = round(sl, 5)
    tp      = round(tp, 5)

    total_cost_pips = config.get_total_cost_pips(state.get("symbol"))
    spread_pips     = config.get_spread_pips(state.get("symbol"))   # kept for logging
    cost_amount     = total_cost_pips * pip
    net_tp_dist     = max(abs(tp - price) - cost_amount, 0.0)
    net_sl_dist     = sl_dist + cost_amount
    net_rr          = round(net_tp_dist / net_sl_dist, 2) if net_sl_dist > 0 else 0

    # ── Post filters ──────────────────────────────────────────────────────
    if sl_dist < config.MIN_SL_PIPS * pip:
        if debug: print(f"    [S4] REJECTED: SL too tight ({sl_dist/pip:.1f}p < {config.MIN_SL_PIPS})")  # Fix B8
        return None

    if abs(tp - price) / sl_dist < 2.0:
        if debug: print(f"    [S4] REJECTED: raw RR < 2.0")  # Fix B8
        return None

    if net_rr < config.NET_MIN_RR:
        if debug: print(f"    [S4] REJECTED: net RR {net_rr} < {config.NET_MIN_RR}")  # Fix B8
        return None

    comp_range_pips = round((comp_high - comp_low) / pip, 1)
    disp_age_min = round((now_sec - displacement["time"]) / 60)

    reason = (
        f"HTF={htf_direction}(4H:{b4h}/1H:{b1h}) | "
        f"compression ratio={comp['ratio']*100:.0f}% "
        f"zone={comp_low:.5f}-{comp_high:.5f}({comp_range_pips}p) | "
        f"displacement={htf_direction} body={displacement['body_pct']*100:.0f}% "
        f"range={displacement['range_pips']}p age={disp_age_min}min | "
        f"anchor={anchor_desc} | "
        f"pool={'✓' if pool['pool_ok'] else '✗'} "
        f"prox={prox_score}pts entry={entry_model} | "
        f"sess={sessions} spread={spread_pips}pip netRR={net_rr} score={total_score}/115"
    )

    return {
        "trade":       True,
        "type":        trade_type,
        "confidence":  total_score,
        "strategy":    "Volatility Compression Breakout",
        "reason":      reason,
        "entry":       price,
        "sl":          sl,
        "tp":          tp,
        "rr":          rr,
        "net_rr":      net_rr,
        "spread_pips": spread_pips,
        "total_cost_pips":  total_cost_pips,
    }