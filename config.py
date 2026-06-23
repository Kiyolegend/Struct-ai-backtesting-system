"""
Backtest config — unified for all 10 strategies (SC1-SC6 scalping, SW1-SW4 swing).
Mirrors live config from both repos. Adjust for your backtest needs.
"""
import time as _time

SYMBOL = "USD/JPY"

SYMBOL_CONFIG = {
    "USD/JPY": {"mt5_name": "USDJPYm", "pip_size": 0.01,   "digits": 3, "spread_pips": 1.0, "commission_pips": 1.0, "pip_value_per_lot": 6.50},
    "EUR/USD": {"mt5_name": "EURUSDm", "pip_size": 0.0001, "digits": 5, "spread_pips": 1.0, "commission_pips": 0.8, "pip_value_per_lot": 10.00},
    "GBP/USD": {"mt5_name": "GBPUSDm", "pip_size": 0.0001, "digits": 5, "spread_pips": 1.2, "commission_pips": 1.0, "pip_value_per_lot": 10.00},
    "EUR/JPY": {"mt5_name": "EURJPYm", "pip_size": 0.01,   "digits": 3, "spread_pips": 1.4, "commission_pips": 1.6, "pip_value_per_lot": 6.50},
    "GBP/JPY": {"mt5_name": "GBPJPYm", "pip_size": 0.01,   "digits": 3, "spread_pips": 3.5, "commission_pips": 2.2, "pip_value_per_lot": 6.50},
    "AUD/USD": {"mt5_name": "AUDUSDm", "pip_size": 0.0001, "digits": 5, "spread_pips": 1.2, "commission_pips": 0.9, "pip_value_per_lot": 10.00},
    "USD/CAD": {"mt5_name": "USDCADm", "pip_size": 0.0001, "digits": 5, "spread_pips": 1.5, "commission_pips": 1.4, "pip_value_per_lot": 7.30},
    "USD/CHF": {"mt5_name": "USDCHFm", "pip_size": 0.0001, "digits": 5, "spread_pips": 1.5, "commission_pips": 0.7, "pip_value_per_lot": 11.10},
}

DISABLED_SYMBOLS = {"GBP/JPY", "EUR/JPY", "USD/CAD"}
SCAN_SYMBOLS = [s for s in SYMBOL_CONFIG.keys() if s not in DISABLED_SYMBOLS]

def get_symbol_cfg(symbol: str = None) -> dict:
    return SYMBOL_CONFIG.get(symbol or SYMBOL, SYMBOL_CONFIG["USD/JPY"])

def get_spread_pips(symbol: str = None) -> float:
    return SYMBOL_CONFIG.get(symbol or SYMBOL, SYMBOL_CONFIG["USD/JPY"]).get("spread_pips", 1.0)

def get_commission_pips(symbol: str = None) -> float:
    return SYMBOL_CONFIG.get(symbol or SYMBOL, SYMBOL_CONFIG["USD/JPY"]).get("commission_pips", 0.5)

def get_total_cost_pips(symbol: str = None) -> float:
    return get_spread_pips(symbol) + get_commission_pips(symbol)

MT5_SYMBOL = get_symbol_cfg()["mt5_name"]

# ── Scalping parameters ──────────────────────────────────────────────────────
ACCOUNT_BALANCE      = 135.0
DEFAULT_LOT          = 0.02
MAX_LOT              = 0.05
MAX_RISK_PERCENT     = 0.03
CONTRACT_SIZE        = 100000
MIN_RR               = 2.0
NET_MIN_RR           = 1.6
TARGET_RR            = 2.0
MAX_TRADES_PER_DAY   = 3
MAX_CONSECUTIVE_LOSSES = 2
MIN_CONFIDENCE       = 80
LOOP_INTERVAL        = 12
SIMULATION_MODE      = True
NEAR_LEVEL_PIPS      = 10
PIP_SIZE             = get_symbol_cfg()["pip_size"]
MIN_SL_PIPS          = 7
SL_BUFFER_PIPS       = 5
SWEEP_SL_BUFFER_PIPS = 8
MIN_SWEEP_RECOVERY_PIPS = 5
MAX_ENTRY_DRIFT_PIPS = 3

# ── Swing parameters (override MIN_RR / MIN_SL_PIPS for swing strategies) ──
SWING_MIN_RR         = 3.0
SWING_NET_MIN_RR     = 2.5
SWING_MIN_SL_PIPS    = 25
SWING_SL_BUFFER_PIPS = 12
SWING_MIN_CONFIDENCE = 75
SWING_TARGET_RR      = 4.0
SWING_MAX_TRADES_PER_DAY = 5

# ── SW4 parameters ───────────────────────────────────────────────────────────
SW4_NEAR_EXTREME_PIPS = 50
SW4_MIN_CONFIDENCE    = 80
SW4_MIN_RR            = 4.0
SW4_SL_BUFFER_PIPS    = 15
SW4_TP_PRIMARY_FIB    = 0.382
SW4_TP_EXTENSION_FIB  = 0.618


def get_broker_ts(state: dict) -> int:
    """In backtest mode, returns the bar timestamp from state."""
    ts = state.get("reference_ts")
    if ts:
        return int(ts)
    try:
        candles = (state.get("5m") or {}).get("candles") or []
        if candles:
            t = int(candles[-1]["time"])
            if t > 1_000_000_000:
                return t
    except Exception:
        pass
    return int(_time.time())


def fib_extension_tp(state: dict, direction: str, entry: float) -> float | None:
    """
    127.2% Fibonacci extension TP.
    BUG FIX (B-FIB): Previously used independent `or` chains for hi/lo, which
    allowed D1 swing_hi to be paired with 4H swing_lo (or vice-versa), producing
    a range measured across two different timeframes and therefore a wrong TP price.
    Fix: try the D1 pair first (both hi AND lo from D1); if either is missing fall
    back to the full 4H pair.  Never mix hi from one TF with lo from another.
    """
    try:
        d1 = state.get("d1") or {}
        h4 = state.get("4h") or {}

        # Try D1 pair first (swing-grade TP for SW strategies)
        hi = d1.get("swing_hi")
        lo = d1.get("swing_lo")

        # If D1 pair is incomplete, fall back to the 4H pair (scalp-grade TP)
        if not hi or not lo:
            hi = h4.get("swing_hi")
            lo = h4.get("swing_lo")

        if not hi or not lo or hi <= lo:
            return None
        rng = hi - lo
        tp  = (hi + 0.272 * rng) if direction == "bullish" else (lo - 0.272 * rng)
        if direction == "bullish" and tp <= entry: return None
        if direction == "bearish" and tp >= entry: return None
        return round(tp, 5)
    except Exception:
        return None


# ── Scalping TP cap ───────────────────────────────────────────────────────────
# FIX B-FIB-TP: The raw fib_extension_tp() is based on D1/4H swing ranges that
# span 400–2000 pips for major pairs.  For a scalp trade with a 15–40 pip SL
# this produces TPs 150–500+ pips away that are virtually never reached, driving
# win rate toward zero while every SL hits immediately.
#
# Scalping strategies must cap their TP at SCALP_MAX_RR × SL distance.
# If the fib extension would require the price to travel more than 3× the SL
# distance, it is rejected and the caller falls back to the plain 2× SL TP.
# (A 3× RR scalp with a 30-pip SL = 90-pip TP — still ambitious but reachable.)
SCALP_MAX_RR = 3.0


def scalp_extension_tp(
    state: dict, direction: str, entry: float, sl_dist: float
) -> float | None:
    """
    Fib extension TP capped at SCALP_MAX_RR × SL distance.

    Returns the fib TP only when it represents a realistic intraday target
    (≤ SCALP_MAX_RR × sl_dist from entry).  Returns None otherwise so the
    strategy falls back to the standard TARGET_RR × SL TP.

    Use this in all SC1–SC6 scalping strategies instead of fib_extension_tp().
    Swing strategies (SW1–SW4) can continue using fib_extension_tp() directly
    because their SLs are already 25–60 pips, making the fib target proportionate.
    """
    if sl_dist <= 0:
        return None
    _fib = fib_extension_tp(state, direction, entry)
    if _fib is None:
        return None
    if abs(_fib - entry) / sl_dist > SCALP_MAX_RR:
        return None   # fib target is too far for scalping — caller uses fallback
    return _fib
