"""
Strategy 5 — Session Open Momentum Scalp
=========================================
The most frequently firing strategy in the engine.

Captures the predictable momentum push that occurs at the start of
London open and NY open. Every trading day these sessions create
directional momentum — this strategy rides that push with minimal
conditions, targeting 1-2 trades per day even in poor market conditions.

Target: $2-3/day on a $135 account at 0.02 lots (USDJPY ~$0.13/pip).
A 10-pip SL targeting 20-pip TP (2R) = ~$2.60 per win.

Timeframe logic:
  4H / 1H  — bias (1H required, 4H adds confirmation bonus)
  15M      — CHoCH guard (counter-structure rejection)
  5M       — BOS trigger + zone/level proximity + volatility check

Firing windows (UTC):
  London open : 07:00 – 08:30  (90 minutes, BST) / 08:00 – 09:30 (GMT)
  NY open     : 12:00 – 13:30  (90 minutes, EDT) / 13:00 – 14:30 (EST)

Scoring (max 110, minimum 85):
  Session open window     — 25   (must be in first 90 min of London or NY)
  1H bias clear           — 20   (required — no neutral bias)
  4H alignment bonus      — 10   (optional — 4H agrees with 1H direction)
  5M BOS within 30 min    — 20   (required — fresh structure break is the trigger)
  Near key level          — 15   (Asian range edge, S/R level, or zone)
  Asian sweep bonus       — 10   (optional — price swept Asian range then BOS confirmed)
  Zone confluence bonus   — 10   (optional — price inside supply/demand zone)

Minimum viable setup (score exactly 85):
  Session window + 1H bias + BOS + near key level → 80
  Fires reliably 1-2× per London, 1× per NY, every trading day.

Guards added (ChatGPT feedback):
  - Dead session volatility check: skips if current 5M ranges are
    < 40% of recent average (holidays, pre-news freeze, dead opens).
  - Asian sweep bonus: rewards setups where price swept Asian high/low
    then confirmed reversal via BOS — strong institutional pattern.
"""

import sys, os, math, time as _time
from datetime import datetime, timezone

# BUG FIX (B-ZONEINFO): ZoneInfo requires the 'tzdata' package on Windows;
# it is built-in on Linux/macOS (system tz database).  Fall back to a
# manual DST-offset lookup so the strategy works on any OS without extras.
try:
    from zoneinfo import ZoneInfo
    _HAS_ZONEINFO = True
except ImportError:
    _HAS_ZONEINFO = False

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import config


def _utc_offset_hours(tz_name: str, dt_utc: datetime) -> int:
    """
    Return the UTC offset in whole hours for a known timezone at dt_utc.
    Used as a fallback when ZoneInfo is unavailable (Windows without tzdata).
    Only supports Europe/London and America/New_York with standard DST rules.
    DST in Europe/London: last Sunday in March → last Sunday in October  (+1h)
    DST in America/New_York: 2nd Sunday in March → 1st Sunday in November (-4h vs -5h)
    """
    import calendar

    def _nth_sunday(year: int, month: int, n: int) -> int:
        """1-indexed: n=1 → first Sunday, n=-1 → last Sunday."""
        cal = calendar.monthcalendar(year, month)
        sundays = [w[6] for w in cal if w[6] != 0]
        return sundays[n - 1] if n > 0 else sundays[n]

    y, m, d = dt_utc.year, dt_utc.month, dt_utc.day

    if tz_name == "Europe/London":
        dst_start = _nth_sunday(y, 3, -1)   # last Sunday March
        dst_end   = _nth_sunday(y, 10, -1)  # last Sunday October
        in_dst = (
            (m > 3 or (m == 3 and d >= dst_start)) and
            (m < 10 or (m == 10 and d < dst_end))
        )
        return 1 if in_dst else 0

    if tz_name == "America/New_York":
        dst_start = _nth_sunday(y, 3, 2)    # 2nd Sunday March
        dst_end   = _nth_sunday(y, 11, 1)   # 1st Sunday November
        in_dst = (
            (m > 3 or (m == 3 and d >= dst_start)) and
            (m < 11 or (m == 11 and d < dst_end))
        )
        return -4 if in_dst else -5

    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Session open window detection
# ─────────────────────────────────────────────────────────────────────────────

def _in_session_open_window( reference_ts: float | None = None) -> tuple[bool, str]:
    """
    Returns (True, session_name) if current time is within the first
    90 minutes of London or NY open. DST-aware.

    London open : 08:00 local (07:00 UTC in BST, 08:00 UTC in GMT)
    NY open     : 08:00 local (12:00 UTC in EDT, 13:00 UTC in EST)
    """
    now_utc = (datetime.fromtimestamp(reference_ts, tz=timezone.utc)
                if reference_ts else datetime.now(timezone.utc))

    mins = now_utc.hour * 60 + now_utc.minute

    # BUG FIX (B-ZONEINFO): Use ZoneInfo when available, fall back to manual DST
    # calculation on Windows where tzdata package may not be installed.
    if _HAS_ZONEINFO:
        lo = int(now_utc.astimezone(ZoneInfo("Europe/London")).utcoffset().total_seconds() // 3600)
        ny = int(now_utc.astimezone(ZoneInfo("America/New_York")).utcoffset().total_seconds() // 3600)
    else:
        lo = _utc_offset_hours("Europe/London",    now_utc)
        ny = _utc_offset_hours("America/New_York", now_utc)

    lo_open = (8 - lo) * 60      # 07:00 UTC in BST, 08:00 UTC in GMT
    ny_open = (8 - ny) * 60      # 12:00 UTC in EDT, 13:00 UTC in EST

    if lo_open <= mins < lo_open + 90:
        return True, "London"
    if ny_open <= mins < ny_open + 90:
        return True, "NY"
    return False, ""


# ─────────────────────────────────────────────────────────────────────────────
# Dead session volatility guard
# ─────────────────────────────────────────────────────────────────────────────

def _session_is_active(candles_5m: list) -> bool:
    """
    Returns False if the market is clearly dead — holiday, pre-news freeze,
    or unusually low-liquidity open.

    Method: compare average range of the last 10 candles (current activity)
    vs the 20 candles before that (baseline normal activity).

    If current activity is less than 40% of baseline, the session is dead.
    This threshold is deliberately loose — it only blocks obviously frozen markets.
    """
    if len(candles_5m) < 30:
        return True  # not enough data to judge — allow through

    recent_ranges   = [c.get("high", 0) - c.get("low", 0) for c in candles_5m[-10:]]
    baseline_ranges = [c.get("high", 0) - c.get("low", 0) for c in candles_5m[-30:-10]]

    avg_recent   = sum(recent_ranges)   / len(recent_ranges)
    avg_baseline = sum(baseline_ranges) / len(baseline_ranges)

    if avg_baseline == 0:
        return True

    return (avg_recent / avg_baseline) >= 0.40


# ─────────────────────────────────────────────────────────────────────────────
# Asian sweep detection
# ─────────────────────────────────────────────────────────────────────────────

def _asian_sweep_bonus(candles_5m: list, direction: str, state: dict) -> int:
    """
    Returns +10 if price swept the Asian session high or low in the last
    4 candles and the BOS direction confirms the post-sweep move.

    Bullish setup: swept Asian low then reversed upward — strong institutional
    pattern (stop hunt on sell-side then continuation up).
    Bearish setup: swept Asian high then reversed downward — buy-side stop hunt
    then continuation down.

    This is one of the most reliable London open patterns.
    Returns 0 if no sweep detected or Asia range is unavailable.
    """
    asia      = state.get("asia_range", {})
    asia_high = asia.get("high")
    asia_low  = asia.get("low")

    recent = candles_5m[-4:]

    if direction == "bullish" and asia_low:
        if any(c.get("low", asia_low + 1) < asia_low for c in recent):
            return 10  # swept sell-side liquidity, now bullish BOS confirmed

    if direction == "bearish" and asia_high:
        if any(c.get("high", asia_high - 1) > asia_high for c in recent):
            return 10  # swept buy-side liquidity, now bearish BOS confirmed

    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Key level proximity check
# ─────────────────────────────────────────────────────────────────────────────

def _near_key_level(price: float, state: dict, pip: float,
                    threshold_pips: float = 20.0) -> bool:
    """
    Returns True if price is within threshold_pips of any of:
      1. Asian session range high or low
      2. Any S/R level from STRUCT.ai
      3. Any supply/demand zone on 5M or 15M
    """
    threshold = threshold_pips * pip

    asia      = state.get("asia_range", {})
    asia_high = asia.get("high")
    asia_low  = asia.get("low")

    if asia_high and abs(price - asia_high) <= threshold:
        return True
    if asia_low  and abs(price - asia_low)  <= threshold:
        return True

    sr_levels = state.get("sr_levels", [])
    if isinstance(sr_levels, list):
        for lvl in sr_levels:
            if isinstance(lvl, dict):
                lp = lvl.get("price") or lvl.get("level")
                if isinstance(lp, (int, float)) and abs(price - lp) <= threshold:
                    return True

    for tf in ("5m", "15m"):
        zones = state.get(tf, {}).get("zones", [])
        if not isinstance(zones, list):
            continue
        for zone in zones:
            if not isinstance(zone, dict):
                continue
            top    = zone.get("top",    0)
            bottom = zone.get("bottom", 0)
            if top == 0 and bottom == 0:
                continue
            if (bottom - threshold) <= price <= (top + threshold):
                return True

    return False


def _in_zone(price: float, state: dict, pip: float,
             threshold_pips: float = 10.0) -> bool:
    """Returns True if price is inside a supply/demand zone."""
    threshold = threshold_pips * pip
    for tf in ("5m", "15m"):
        zones = state.get(tf, {}).get("zones", [])
        if not isinstance(zones, list):
            continue
        for zone in zones:
            if not isinstance(zone, dict):
                continue
            top    = zone.get("top",    0)
            bottom = zone.get("bottom", 0)
            if top == 0 and bottom == 0:
                continue
            if (bottom - threshold) <= price <= (top + threshold):
                return True
    return False

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

    sym_cfg    = config.get_symbol_cfg(state.get("symbol"))
    pip        = sym_cfg["pip_size"]
    candles_5m = s5m.get("candles", [])

    # ── Step 1: Session open window gate ──────────────────────────────────
    broker_ts = config.get_broker_ts(state)
    in_window, window_name = _in_session_open_window(reference_ts=broker_ts)

    if not in_window:
        if debug: print("    [S5] skip: not in London/NY open window (first 90 min of each session open)")
        return None  # FIX 1: gate now actually stops execution outside session window

    session_score = 25

    if debug:
        print(f"    [S5] {window_name} open window active")

    # ── Step 2: Dead session volatility guard ─────────────────────────────
    if not _session_is_active(candles_5m):
        if debug: print("    [S5] skip: dead session — current volatility < 40% of baseline")
        return None

    if debug:
        print("    [S5] session volatility: active")

    # ── Step 3: 1H bias — required, must be clear ─────────────────────────
    b1h = bias.get("1h", "neutral")
    b4h = bias.get("4h", "neutral")

    if b1h not in ("bullish", "bearish"):
        if debug: print("    [S5] skip: 1H bias is neutral — need clear direction")
        return None

    direction  = b1h
    trade_type = "BUY" if direction == "bullish" else "SELL"
    bias_score = 20

    # Reject if 4H is actively opposing (not just non-aligning)
    if b4h != "neutral" and b4h != direction:
        if debug:
            print(f"    [S5] skip: 4H {b4h} actively opposes {direction} — counter-HTF rejected")
        return None

    alignment_bonus = 10 if b4h == direction else 0

    if debug:
        print(f"    [S5] direction={direction} 1H={b1h} 4H={b4h} "
              f"bias={bias_score} align_bonus={alignment_bonus}")

    # ── Step 4: Counter CHoCH guard on 15M ───────────────────────────────
    now_sec           = broker_ts
    choch_15m         = s15m.get("choch", [])
    counter_direction = "bearish" if direction == "bullish" else "bullish"

    recent_counter = [
        c for c in choch_15m
        if isinstance(c, dict)
        and c.get("direction") == counter_direction
        and (now_sec - c.get("time", now_sec)) <= 2 * 3600
    ]

    if recent_counter:
        if debug:
            print(f"    [S5] skip: counter {counter_direction} CHoCH on 15M within 2h")
        return None

    # ── Step 5: 5M BOS trigger — must be within last 30 minutes ──────────
    bos_5m    = s5m.get("bos", [])
    fresh_bos = [
        b for b in bos_5m[-8:]
        if isinstance(b, dict)
        and b.get("direction") == direction
        and (now_sec - b.get("time", 0)) <= 30 * 60
    ]

    if not fresh_bos:
        if debug: print(f"    [S5] skip: no {direction} BOS on 5M within 30 min")
        return None

    bos_score = 20

     # ── Post-BOS structure must hold 

    recent_bos_sorted = sorted(fresh_bos, key=lambda x: x.get("time", 0), reverse=True)
    confirm_time_bos  = recent_bos_sorted[0].get("time", 0) if recent_bos_sorted else 0
    if confirm_time_bos > 0 and candles_5m:
        if not _structure_holds(candles_5m, confirm_time_bos, direction):
            if debug:
                print(f"    [S5] skip: post-BOS structure violated "
                      f"— {direction} momentum already reversed, waiting for fresh BOS")
            return None


    if debug:
        print(f"    [S5] fresh BOS: {len(fresh_bos)} event(s) within 30min")

    # ── Step 6: Near a key level ──────────────────────────────────────────
    near_level = _near_key_level(price, state, pip, threshold_pips=15.0)

    if not near_level:
        if debug: print("    [S5] skip: price not within 15 pips of Asian range / S/R / zone")
        return None

    level_score = 15

    # ── Step 7: Asian sweep bonus ─────────────────────────────────────────
    sweep_bonus = _asian_sweep_bonus(candles_5m, direction, state)

    if debug and sweep_bonus:
        print(f"    [S5] Asian sweep detected — +{sweep_bonus} bonus")

    # ── Step 8: Zone confluence bonus ─────────────────────────────────────
    in_zone_ok = _in_zone(price, state, pip, threshold_pips=10.0)
    zone_bonus = 10 if in_zone_ok else 0

    # ── Total score ───────────────────────────────────────────────────────
    total_score = (session_score + bias_score + alignment_bonus +
                   bos_score + level_score + sweep_bonus + zone_bonus)

    if debug:
        print(f"    [S5] {direction} | sess={session_score} bias={bias_score} "
              f"align={alignment_bonus} bos={bos_score} level={level_score} "
              f"sweep={sweep_bonus} zone={zone_bonus} → {total_score}")

    effective_min = max(80, config.MIN_CONFIDENCE)
    if total_score < effective_min:
        if debug: print(f"    [S5] skip: score {total_score} < {effective_min} (S5_MIN=80, MIN_CONFIDENCE={config.MIN_CONFIDENCE})")  # Fix B9: was unguarded + wrong indent
        return None

    # ── SL / TP ───────────────────────────────────────────────────────────
    buf         = config.SL_BUFFER_PIPS * pip
    s5m_struct  = s5m.get("structure",  [])
    s15m_struct = s15m.get("structure", [])

    def _last_label(structure, label):
        for s in reversed(structure[-15:]):
            if isinstance(s, dict) and s.get("label") == label:
                return s.get("price")
        return None

    if direction == "bullish":
        sl_5m     = _last_label(s5m_struct,  "HL") or _last_label(s5m_struct,  "EQL")
        sl_15m    = _last_label(s15m_struct, "HL") or _last_label(s15m_struct, "EQL")
        sl_anchor = sl_5m if sl_5m is not None else sl_15m

        if sl_anchor is None:
            sl_anchor = state.get("asia_range", {}).get("low")
        if sl_anchor is None:
            if debug: print("    [S5] skip: no SL anchor found")
            return None

        sl = sl_anchor - buf
        if sl >= price:
            if debug: print("    [S5] skip: SL not below entry for BUY")
            return None
    else:
        sl_5m     = _last_label(s5m_struct,  "LH") or _last_label(s5m_struct,  "EQH")
        sl_15m    = _last_label(s15m_struct, "LH") or _last_label(s15m_struct, "EQH")
        sl_anchor = sl_5m if sl_5m is not None else sl_15m

        if sl_anchor is None:
            sl_anchor = state.get("asia_range", {}).get("high")
        if sl_anchor is None:
            if debug: print("    [S5] skip: no SL anchor found")
            return None

        sl = sl_anchor + buf
        if sl <= price:
            if debug: print("    [S5] skip: SL not above entry for SELL")
            return None

    sl_dist = abs(price - sl)
    # FIX B-FIB-TP: use scalp_extension_tp() which caps the fib TP at SCALP_MAX_RR.
    # Raw fib_extension_tp() uses D1/4H swing ranges (400-2000 pips for USDJPY),
    # making TP unreachable for a scalp with a 15-40 pip SL (actual RR 10-40:1).
    # Cap at 3× SL; fall back to TARGET_RR × SL when fib is too far.
    _fib = config.scalp_extension_tp(state, direction, price, sl_dist)
    tp   = _fib if _fib is not None else (
               (price + sl_dist * config.TARGET_RR) if direction == "bullish"
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
        if debug: print(f"    [S5] REJECTED: SL too tight ({sl_dist/pip:.1f}p < {config.MIN_SL_PIPS})")  # Fix B9
        return None

    if abs(tp - price) / sl_dist < 2.0:
        if debug: print(f"    [S5] REJECTED: raw RR < 2.0")  # Fix B9
        return None

    if net_rr < config.NET_MIN_RR:
        if debug: print(f"    [S5] REJECTED: net RR {net_rr} < {config.NET_MIN_RR}")  # Fix B9
        return None

    asia      = state.get("asia_range", {})
    asia_high = asia.get("high")
    asia_low  = asia.get("low")
    asia_ref  = (f"Asia H={asia_high:.3f} L={asia_low:.3f}"
                 if asia_high and asia_low else "no Asia range")

    sweep_tag = "sweep+BOS" if sweep_bonus else "no-sweep"

    reason = (
        f"{window_name} open momentum | "
        f"1H={b1h} 4H={b4h} | "
        f"5M BOS {len(fresh_bos)}x within 30min | "
        f"near_level=✓ zone={'✓' if in_zone_ok else '✗'} {sweep_tag} | "
        f"{asia_ref} | "
        f"spread={spread_pips}pip netRR={net_rr} score={total_score}/110"
    )

    return {
        "trade":       True,
        "type":        trade_type,
        "confidence":  total_score,
        "strategy":    "Session Open Momentum Scalp",
        "reason":      reason,
        "entry":       price,
        "sl":          sl,
        "tp":          tp,
        "rr":          rr,
        "net_rr":      net_rr,
        "spread_pips": spread_pips,
        "total_cost_pips":  total_cost_pips,
    }