"""
Bar-by-Bar Replay Engine  (v2 — walk-forward + Monte Carlo + portfolio)
=======================================================================
Fixes applied vs v1:
  F1  build_state() — np.searchsorted O(log n) slice instead of O(n) boolean mask
  F2  Module-level state cleared at run start (scalp6 cooldown, SW1-4 _fired_swings, SW4 _pending)
  F3  Trade creation: guard against None entry/sl/tp from strategy
  F4  _compute_stats() profit factor: division-by-zero guard
  F5  zone_visited: computed from correctly scoped current_price
  F6  build_state() — initialise all TF keys before engine loop (avoids KeyErrors in strategies)
  F7  trade open/close: TP/SL both-same-bar — TP checked first (benefit of the doubt)
  F8  SL/TP ordering sanity: BUY must have SL < entry < TP; SELL vice-versa

New in v2:
  N1  Walk-forward validation — 70/30 split; in-sample & out-of-sample run independently
  N2  Monte Carlo simulation — 1000 shuffles, 5th/50th/95th percentile equity bands
  N3  _replay_bars() refactor — shared loop used by full run, walk-forward segments, etc.
  N4  Entry TF auto-detect logic fix — removed redundant compound condition
  N5  All scalp strategy files: confirmed no critical logic bugs (news filter indent is cosmetic)
"""

import sqlite3
import os
import sys
import json
import importlib.util
import datetime
import random
from typing import Optional
from bisect import bisect_right

import pandas as pd
import numpy as np

# ── Engine imports ───────────────────────────────────────────────────────────
ENGINE_DIR = os.path.join(os.path.dirname(__file__), "engines")
sys.path.insert(0, ENGINE_DIR)
sys.path.insert(0, os.path.dirname(__file__))   # config + news_filter_live

from zigzag_engine    import detect_swings, FRACTAL_N
from structure_engine import classify_structure
from bos_engine       import detect_bos
from choch_engine     import detect_choch
from trend_engine     import detect_trend
from zones_engine     import detect_zones

import config as bt_config

STRATEGIES_ROOT = os.path.join(os.path.dirname(__file__), "strategies")
SCALPING_DIR    = os.path.join(STRATEGIES_ROOT, "scalping")
SWING_DIR       = os.path.join(STRATEGIES_ROOT, "swing")
DB_PATH      = os.path.join(os.path.dirname(__file__), "data", "market_data.db")
FRACTAL_GUARD = FRACTAL_N

# FIX B5: Timeframes whose engine outputs are cached between bars.
# D1/4H/1H/15M structure doesn't change on every 5M bar — only recompute when
# the latest bar timestamp for that TF actually changes.
# 15M changes every 3 5M bars → 67% cache hit rate (free speedup vs original).
_CACHE_TFS = {"D1", "4H", "1H", "15M"}

# PERF: 5M engines re-run at most once per this many seconds (3 bars × 5 min = 15 min).
# Within a 15-min window the BOS/CHoCH/zone analysis is reused; candles are always fresh.
# Staleness is 15 min max — well within every strategy's 30-min freshness windows.
_5M_STEP_SECS = 15 * 60

STRATEGY_MAP = {
    "SC1": ("scalping", "scalp1"),
    "SC2": ("scalping", "scalp2"),
    "SC3": ("scalping", "scalp3"),
    "SC4": ("scalping", "scalp4"),
    "SC5": ("scalping", "scalp5"),
    "SC6": ("scalping", "scalp6"),
    "SW1": ("swing",    "swing1"),
    "SW2": ("swing",    "swing2"),
    "SW3": ("swing",    "swing3"),
    "SW4": ("swing",    "swing4"),
    # Custom / user-defined strategies — add your own entries here:
    "CUSTOM1": ("custom", "custom_template"),
}


# ─────────────────────────────────────────────────────────────────────────────
# SQLite loader
# ─────────────────────────────────────────────────────────────────────────────

def load_bars(symbol: str, timeframe: str) -> pd.DataFrame:
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(
            f"Database not found: {DB_PATH}\n"
            "Run: start.bat --collect   to download market data first."
        )
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT ts AS ts_unix, open, high, low, close, volume "
        "FROM ohlcv WHERE symbol=? AND timeframe=? ORDER BY ts",
        conn, params=(symbol, timeframe)
    )
    conn.close()
    df["time"] = pd.to_datetime(df["ts_unix"], unit="s", utc=True)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# State builder — F1: O(log n) slicing via searchsorted
# ─────────────────────────────────────────────────────────────────────────────

def _df_slice_to_candles(df: pd.DataFrame) -> list:
    """
    PERF FIX: Convert a DataFrame slice to candle dicts using vectorized numpy access.
    Replaces the old per-row iloc[i] loop which was the single biggest bottleneck —
    iloc[i] on a pandas DataFrame is ~10-50μs per call; accessing .values arrays
    via zip is ~0.1μs per row.  For 200 candles × 5 TFs × 70k bars the old approach
    consumed the vast majority of wall time.
    """
    ts = df["ts_unix"].values.astype(np.int64)
    o  = df["open"].values
    h  = df["high"].values
    l  = df["low"].values
    c  = df["close"].values
    v  = df["volume"].values
    return [
        {"time": int(t), "open": float(ov), "high": float(hv),
         "low": float(lv), "close": float(cv), "volume": float(vv)}
        for t, ov, hv, lv, cv, vv in zip(ts, o, h, l, c, v)
    ]


def build_state(
    symbol: str,
    current_ts: int,
    tf_data: dict,           # {tf_label: df}
    tf_ts_arrays: dict,      # {tf_label: np.array of ts_unix} — pre-built
    candle_window: int = 200,
    _tf_cache: dict = None,  # FIX B5: shared engine-output cache; key=(tf_label,latest_ts)
) -> dict:
    """
    Build state dict visible at current_ts using strict no-lookahead.
    F1: searchsorted for O(log n) per-TF per-bar slice.
    """
    state: dict = {
        "symbol":            symbol,
        "reference_ts":      current_ts,
        "sessions":          _classify_session(current_ts),
        "tradeable_session": _is_tradeable_session(current_ts),
        "sr_levels":         [],
        "asia_range":        {},
        "bias":              {},
        "current_price":     0.0,
    }

    # F6: pre-initialise all TF keys so strategies never KeyError
    for tf_label in tf_data.keys():
        state[tf_label.lower()] = {
            "candles":   [],
            "bos":       [],
            "choch":     [],
            "structure": [],
            "zones":     [],
            "swing_hi":  None,
            "swing_lo":  None,
        }

    last_close = 0.0

    for tf_label, df_full in tf_data.items():
        ts_arr = tf_ts_arrays[tf_label]     # sorted numpy array

        # F1: O(log n) binary search
        end_idx  = int(np.searchsorted(ts_arr, current_ts, side="right"))
        if end_idx == 0:
            continue

        start_idx = max(0, end_idx - candle_window)

        # PERF: use a view (no copy) for candle building; reset_index only for engines
        slice_view = df_full.iloc[start_idx:end_idx]

        if slice_view.empty:
            continue

        current_close = float(df_full["close"].iloc[end_idx - 1])
        last_close    = current_close
        tf_key        = tf_label.lower()

        # PERF: Determine cache key.
        # For cached TFs (D1/4H/1H/15M): key = (tf_label, latest_bar_ts).
        #   These TFs change once per candle period → 67-99.7% hit rates.
        # For 5M: key = (tf_label, binned_ts) — bin into 15-min windows so
        #   engines re-run at most once per 3 bars instead of every bar.
        latest_slice_ts = int(ts_arr[end_idx - 1])
        if tf_label == "5M":
            _cache_key = ("5M", current_ts // _5M_STEP_SECS)
        else:
            _cache_key = (tf_label, latest_slice_ts)

        if _tf_cache is not None and _cache_key in _tf_cache:
            cached  = _tf_cache[_cache_key]
            # PERF: vectorized candle building (no per-row iloc)
            candles = _df_slice_to_candles(slice_view)
            state[tf_key] = {**cached, "candles": candles}
            if tf_label in ("D1", "4H", "1H", "15M"):
                state["bias"][tf_key] = cached["_trend"]
            continue

        # PERF: reset_index only needed for engine calls (ensures 0-based positional index)
        slice_df = slice_view.reset_index(drop=True)

        # PERF: vectorized candle building
        candles = _df_slice_to_candles(slice_view)

        # Engine computations — no-lookahead guaranteed by searchsorted above
        # FIX B-FRACTAL: match live Struct.ai fractal_n per TF:
        #   D1 / 4H / 1H → 3 bars each side (higher TFs have fewer candles, need tighter lookback)
        #   15M / 5M     → 5 bars each side (default, more candles available)
        _fractal_n = 3 if tf_label in ("D1", "4H", "1H") else 5
        try:
            swings = detect_swings(slice_df, fractal_n=_fractal_n)
        except Exception:
            swings = []

        try:
            labels = classify_structure(swings)
        except Exception:
            labels = []

        try:
            trend_result = detect_trend(labels)
            trend        = trend_result["trend"]
        except Exception:
            trend = "neutral"

        # FIX B-LOOKBACK: match live Struct.ai per-TF lookback tables exactly.
        # Live source: routers/structure.py
        #   _bos_hours   = {"5m": 8, "15m": 48, "1h": 72, "4h": 336, "d1": 8760}
        #   _choch_hours = {"5m": 8, "15m": 24, "1h": 72, "4h": 336, "d1": 4320}
        # Previous backtester values were far too short (D1 was 240h vs 8760h live).
        lookback_h  = {"D1": 8760, "4H": 336, "1H": 72, "15M": 48, "5M": 8}.get(tf_label, 48)
        choch_h     = {"D1": 4320, "4H": 336, "1H": 72, "15M": 24, "5M": 8}.get(tf_label, 24)
        try:
            bos_events = detect_bos(slice_df, swings, labels, trend=trend, lookback_hours=lookback_h)
        except Exception:
            bos_events = []

        try:
            choch_events = detect_choch(slice_df, swings, labels, trend=trend, lookback_hours=choch_h)
        except Exception:
            choch_events = []

        try:
            zones = detect_zones(swings, timeframe=tf_label.lower(), current_price=current_close)
        except Exception:
            zones = []

        swing_hi = swing_lo = None
        for sw in reversed(swings):
            if sw["kind"] == "high" and swing_hi is None:
                swing_hi = sw["price"]
            if sw["kind"] == "low"  and swing_lo is None:
                swing_lo = sw["price"]
            if swing_hi and swing_lo:
                break

        engine_result = {
            "bos":       bos_events,
            "choch":     choch_events,
            "structure": labels,
            "zones":     zones,
            "swing_hi":  swing_hi,
            "swing_lo":  swing_lo,
            "_trend":    trend,   # private — reconstructs bias on cache hit
        }
        # Store in cache for subsequent bars that fall in the same TF period.
        # 5M also stores now (keyed by binned 15-min window) so 2 of every 3
        # 5M bars get a cache hit instead of re-running all six engines.
        if _tf_cache is not None:
            _tf_cache[_cache_key] = engine_result

        state[tf_key] = {**engine_result, "candles": candles}

        # bias dict — used by all strategies
        if tf_label in ("D1", "4H", "1H", "15M"):
            state["bias"][tf_key] = trend

        # track the latest close we see (finest TF wins)
        last_close = current_close

    # current_price: use finest available TF close
    for tf_label in ("5M", "15M", "1H", "4H", "D1"):
        tf_key = tf_label.lower()
        cds = state.get(tf_key, {}).get("candles") or []
        if cds:
            state["current_price"] = cds[-1]["close"]
            break

    if state["current_price"] == 0.0 and last_close:
        state["current_price"] = last_close

    # F5: zone_visited — computed AFTER current_price is set
    d1 = state.get("d1", {})
    if d1 and d1.get("swing_hi") and d1.get("swing_lo"):
        state["d1"]["zone_visited"] = _is_zone_visited(d1, state["current_price"])

    # S/R levels: aggregate zone centres from D1, 4H and 1H
    # Fix: include D1 (required by SW2/SW4 D1-level S/R checks).
    # Fix: assign direction by position relative to current price — zones above
    #      current price are resistance, zones below are support.  The old code
    #      labelled every zone as BOTH kinds, producing false positives.
    sr_levels = []
    cp = state["current_price"]
    for tf_label in ("D1", "4H", "1H"):
        tf_key = tf_label.lower()
        for zone in (state.get(tf_key) or {}).get("zones", []):
            ctr = zone.get("center", 0)
            if ctr and ctr > 0:
                kind = "resistance" if ctr >= cp else "support"
                sr_levels.append({
                    "price":     ctr,
                    "timeframe": tf_key,
                    "kind":      kind,
                    "strength":  zone.get("strength", 1),
                })
    state["sr_levels"] = sr_levels

    # Asia range from 1H bars
    state["asia_range"] = _calc_asia_range(tf_data.get("1H"), tf_ts_arrays.get("1H"), current_ts)

    return state


# ─────────────────────────────────────────────────────────────────────────────
# Session helpers
# ─────────────────────────────────────────────────────────────────────────────

def _classify_session(ts: int) -> list:
    dt   = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    mins = dt.hour * 60 + dt.minute
    sessions = []
    if 0   <= mins < 9 * 60:       sessions.append("asia")
    if 7  * 60 <= mins < 16 * 60:  sessions.append("london")
    if 12 * 60 <= mins < 21 * 60:  sessions.append("ny")
    return sessions


def _is_tradeable_session(ts: int) -> bool:
    sessions = _classify_session(ts)
    return "london" in sessions or "ny" in sessions


def _is_zone_visited(d1_state: dict, current_price: float) -> bool:
    hi = d1_state.get("swing_hi")
    lo = d1_state.get("swing_lo")
    if not hi or not lo or hi <= lo or current_price <= 0:
        return False
    rng = hi - lo
    zone_top = hi - 0.382 * rng
    zone_bot = hi - 0.618 * rng
    return zone_bot <= current_price <= zone_top


def _calc_asia_range(df_1h: Optional[pd.DataFrame],
                     ts_arr_1h: Optional[np.ndarray],
                     current_ts: int) -> dict:
    if df_1h is None or df_1h.empty or ts_arr_1h is None:
        return {}
    dt_c      = datetime.datetime.fromtimestamp(current_ts, tz=datetime.timezone.utc)
    day_start = int(datetime.datetime(dt_c.year, dt_c.month, dt_c.day, 0, 0,
                                      tzinfo=datetime.timezone.utc).timestamp())
    asia_end  = day_start + 9 * 3600

    lo = int(np.searchsorted(ts_arr_1h, day_start, side="left"))
    hi = int(np.searchsorted(ts_arr_1h, asia_end,  side="right"))
    if lo >= hi:
        return {}
    asia_bars = df_1h.iloc[lo:hi]
    return {
        "high": float(asia_bars["high"].max()),
        "low":  float(asia_bars["low"].min()),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Trade P&L
# ─────────────────────────────────────────────────────────────────────────────

def calc_pnl(trade: dict, exit_price: float, symbol: str, lot_size: float) -> float:
    cfg          = bt_config.get_symbol_cfg(symbol)
    pip          = cfg["pip_size"]
    pip_val = (lot_size * 100_000 * pip / exit_price) if "JPY" in symbol else (lot_size * 100_000 * pip)
    total_cost_p = bt_config.get_total_cost_pips(symbol)

    if trade["type"] == "BUY":
        pip_gain = (exit_price - trade["entry"]) / pip - total_cost_p
    else:
        pip_gain = (trade["entry"] - exit_price) / pip - total_cost_p

    return round(pip_gain * pip_val, 4)


# ─────────────────────────────────────────────────────────────────────────────
# Strategy loader
# ─────────────────────────────────────────────────────────────────────────────

def load_strategy(code: str):
    kind, modname = STRATEGY_MAP[code]
    # Resolve folder: "scalping" → strategies/scalping/
    #                 "swing"    → strategies/swing/
    #                 anything   → strategies/<kind>/  (supports custom folders)
    if kind == "scalping":
        folder = SCALPING_DIR
    elif kind == "swing":
        folder = SWING_DIR
    else:
        folder = os.path.join(STRATEGIES_ROOT, kind)
    fpath  = os.path.join(folder, f"{modname}.py")
    spec   = importlib.util.spec_from_file_location(modname, fpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ─────────────────────────────────────────────────────────────────────────────
# F2: Clear module-level state before each run
# ─────────────────────────────────────────────────────────────────────────────

def _reset_strategy_state(modules: dict) -> None:
    """
    Reset module-level cooldown/dedup state in strategies so each backtest
    run starts clean (not contaminated by a previous run or live session).
    """
    sc6 = modules.get("SC6")
    if sc6 and hasattr(sc6, "_fired_today"):
        sc6._fired_today = {}
        sc6._backtest_mode = True   # Fix B10: suppress disk writes during backtest replay

    for code in ("SW1", "SW2", "SW3", "SW4"):
        mod = modules.get(code)
        if mod and hasattr(mod, "_fired_swings"):
            mod._fired_swings = {}

    sw4 = modules.get("SW4")
    if sw4 and hasattr(sw4, "_pending_sw4"):
        sw4._pending_sw4 = {}


# ─────────────────────────────────────────────────────────────────────────────
# Walk-forward helpers
# ─────────────────────────────────────────────────────────────────────────────

def _compute_stats(strategies: list, symbol: str, trades: list,
                   equity_curve: list, total_pnl: float) -> dict:
    # Fix B12: exclude OPEN_AT_END from win/loss counts — those are neither wins nor losses
    closed = [t for t in trades if t.get("result") != "OPEN_AT_END"]
    wins   = [t for t in closed if t.get("result") == "TP"]
    losses = [t for t in closed if t.get("result") == "SL"]
    n      = len(trades)
    nc     = len(closed)
    wr     = (len(wins) / nc * 100) if nc else 0

    # Fix B11: key must be "by_strategy" — viewer JS reads d.by_strategy
    by_strategy = {}
    for code in strategies:
        st  = [t for t in trades if t["strategy"] == code]
        stc = [t for t in st if t.get("result") != "OPEN_AT_END"]
        sw  = [t for t in stc if t.get("result") == "TP"]
        sn  = len(st)
        snc = len(stc)
        by_strategy[code] = {
            "trades":    sn,
            "wins":      len(sw),
            "losses":    snc - len(sw),
            "win_rate":  round(len(sw) / snc * 100, 1) if snc else 0,
            "total_pnl": round(sum(t.get("pnl", 0) or 0 for t in st), 4),
            # BUG FIX (B-AVG-RR-STRAT): same fix as top-level avg_rr — use closed
            # trades only (stc/snc) so OPEN_AT_END theoretical RR doesn't inflate
            "avg_rr":    round(sum(t.get("rr",  0) or 0 for t in stc) / snc, 2) if snc else 0,
        }

    peak = max_dd = 0.0
    for _, pnl in equity_curve:
        if pnl > peak: peak = pnl
        dd = peak - pnl
        if dd > max_dd: max_dd = dd

    gross_win  = sum(t["pnl"] for t in wins  if t.get("pnl"))
    gross_loss = abs(sum(t["pnl"] for t in losses if t.get("pnl")))
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)

    # BUG FIX (B-AVG-RR): avg_rr must exclude OPEN_AT_END trades — those have the
    # signal's theoretical RR (could be 5R+), not a realised RR.  Using all trades
    # inflated avg_rr to nonsensical values (e.g. 333.83 instead of 1.25).
    avg_rr = round(sum(t.get("rr", 0) or 0 for t in closed) / nc, 2) if nc else 0

    return {
        "symbol":         symbol,
        "strategies":     strategies,
        "total_trades":   n,
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       round(wr, 2),
        "total_pnl":      round(total_pnl, 4),
        "avg_rr":         avg_rr,
        "max_drawdown":   round(max_dd, 4),
        "profit_factor":  pf,
        "trades":         trades,
        "equity_curve":   equity_curve,
        "by_strategy":    by_strategy,
    }


def _compute_monte_carlo(trades: list, iterations: int = 1000) -> dict:
    """
    Shuffle the list of closed-trade PnL values N times.
    At each trade-step position, collect the distribution across all shuffles
    and return the 5th, 50th, and 95th percentile equity at that step.
    Returns compact lists (one value per step) for the viewer.
    """
    pnl_values = [t.get("pnl", 0) or 0 for t in trades]
    n = len(pnl_values)
    if n < 2:
        # BUG FIX (B-MC-STUB): Return a complete dict with all expected keys so
        # the viewer JS never gets undefined when accessing final_p5/p50/p95/
        # prob_positive — even if called directly with fewer than 2 trades.
        return {
            "iterations": iterations, "p5": [], "p50": [], "p95": [],
            "final_p5": 0.0, "final_p50": 0.0, "final_p95": 0.0,
            "prob_positive": 0.0,
        }

    rng = random.Random(42)   # reproducible seed

    # Build all curves first (transpose-friendly)
    matrix = []   # shape: [iterations × n]
    for _ in range(iterations):
        shuffled = pnl_values.copy()
        rng.shuffle(shuffled)
        curve = []
        cum = 0.0
        for pnl in shuffled:
            cum += pnl
            curve.append(round(cum, 4))
        matrix.append(curve)

    # At each step, sort across iterations and pick percentiles
    p5_idx  = max(0, int(iterations * 0.05) - 1)
    p50_idx = max(0, int(iterations * 0.50) - 1)
    p95_idx = min(iterations - 1, int(iterations * 0.95))

    p5_vals  = []
    p50_vals = []
    p95_vals = []

    for step in range(n):
        col = sorted(matrix[i][step] for i in range(iterations))
        p5_vals.append(col[p5_idx])
        p50_vals.append(col[p50_idx])
        p95_vals.append(col[p95_idx])

    final_vals = sorted(matrix[i][-1] for i in range(iterations))
    prob_positive = round(sum(1 for v in final_vals if v > 0) / iterations * 100, 1)

    return {
        "iterations":     iterations,
        "p5":             p5_vals,
        "p50":            p50_vals,
        "p95":            p95_vals,
        "final_p5":       round(p5_vals[-1],  4),
        "final_p50":      round(p50_vals[-1], 4),
        "final_p95":      round(p95_vals[-1], 4),
        "prob_positive":  prob_positive,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Backtest Engine
# ─────────────────────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Bar-by-bar replay engine for all 10 strategies.

    Parameters:
        symbol          : "USD/JPY", "EUR/USD", etc.
        strategies      : list of strategy codes ["SC1", "SW1", …]
        lot_size        : position size in lots (default 0.02 for scalping)
        debug           : verbose per-bar output from strategy check()
        entry_tf        : timeframe that drives the replay loop
                          (auto: 5M for scalp, 1H for swing-only)
        walk_forward    : run 70/30 in-sample / out-of-sample split (default True)
        monte_carlo     : run 1000-iteration PnL shuffle simulation (default True)
        mc_iterations   : number of Monte Carlo shuffles (default 1000)
        wf_split        : fraction of bars used as in-sample (default 0.70)
    """

    def __init__(
        self,
        symbol: str = "USD/JPY",
        strategies: list = None,
        lot_size: float = 0.02,
        debug: bool = False,
        entry_tf: str = None,
        walk_forward: bool = True,
        monte_carlo: bool = True,
        mc_iterations: int = 1000,
        wf_split: float = 0.70,
    ):
        self.symbol         = symbol
        self.strategies     = strategies or list(STRATEGY_MAP.keys())
        self.lot_size       = lot_size
        self.debug          = debug
        self.walk_forward   = walk_forward
        self.monte_carlo    = monte_carlo
        self.mc_iterations  = mc_iterations
        self.wf_split       = wf_split

        # N4: fixed auto-detect (removed redundant compound condition)
        if entry_tf:
            self.entry_tf = entry_tf
        else:
            has_scalp = any(s.startswith("SC") for s in self.strategies)
            self.entry_tf = "5M" if has_scalp else "1H"

        self._is_swing_run = all(s.startswith("SW") for s in self.strategies)
        self._tf_labels = ["D1", "4H", "1H"] if self._is_swing_run else ["D1", "4H", "1H", "15M", "5M"]

    # ─────────────────────────────────────────────────────────────────────────
    # Core replay loop (shared by full run and walk-forward segments)
    # ─────────────────────────────────────────────────────────────────────────

    def _replay_bars(
        self,
        entry_df: pd.DataFrame,
        entry_ts_arr: np.ndarray,
        tf_data: dict,
        tf_ts_arrays: dict,
        strategy_modules: dict,
        strategy_fns: dict,
        bar_start: int,
        bar_end: int,
        label: str = "",
    ) -> tuple:
        """
        Replay bars[bar_start : bar_end] with fresh module state.
        Returns (all_trades, equity_curve, cum_pnl).
        """
        _reset_strategy_state(strategy_modules)

        open_trades:  dict = {}
        all_trades:   list = []
        equity_curve: list = []
        cum_pnl = 0.0

        n_segment    = bar_end - bar_start
        report_every = max(1, n_segment // 100)  # PERF: 1% increments (was 10%)

        # FIX B5: per-segment TF engine cache — shared across all bars in this segment.
        # Cleared between FULL/IS/OOS runs so each segment starts clean.
        tf_cache: dict = {}

        for bar_idx in range(bar_start, bar_end):
            row        = entry_df.iloc[bar_idx]
            current_ts = int(entry_ts_arr[bar_idx])
            bar_high   = float(row["high"])
            bar_low    = float(row["low"])
            bar_close  = float(row["close"])

            if (bar_idx - bar_start) % report_every == 0 and label:
                pct    = (bar_idx - bar_start) / n_segment * 100
                dt_str = datetime.datetime.fromtimestamp(current_ts,
                            tz=datetime.timezone.utc).strftime("%Y-%m-%d")
                # FIX B1 (defence-in-depth): flush=True ensures the line reaches the
                # dashboard immediately even if PYTHONUNBUFFERED is not set.
                print(f"  [{label}] {pct:5.1f}%  {dt_str}  "
                      f"cumPnL=${cum_pnl:+.2f}  trades={len(all_trades)}",
                      flush=True)

            # ── Check & close open trades ──────────────────────────────────
            for code in list(open_trades.keys()):
                trade = open_trades[code]

                # F7: TP checked first (benefit of the doubt)
                if trade["type"] == "BUY":
                    if bar_high >= trade["tp"]:
                        pnl = calc_pnl(trade, trade["tp"], self.symbol, self.lot_size)
                        trade.update({"exit_price": trade["tp"], "exit_ts": current_ts,
                                      "pnl": pnl, "result": "TP"})
                        all_trades.append(trade); cum_pnl += pnl
                        del open_trades[code]; continue
                    if bar_low <= trade["sl"]:
                        pnl = calc_pnl(trade, trade["sl"], self.symbol, self.lot_size)
                        trade.update({"exit_price": trade["sl"], "exit_ts": current_ts,
                                      "pnl": pnl, "result": "SL"})
                        all_trades.append(trade); cum_pnl += pnl
                        del open_trades[code]; continue

                elif trade["type"] == "SELL":
                    if bar_low <= trade["tp"]:
                        pnl = calc_pnl(trade, trade["tp"], self.symbol, self.lot_size)
                        trade.update({"exit_price": trade["tp"], "exit_ts": current_ts,
                                      "pnl": pnl, "result": "TP"})
                        all_trades.append(trade); cum_pnl += pnl
                        del open_trades[code]; continue
                    if bar_high >= trade["sl"]:
                        pnl = calc_pnl(trade, trade["sl"], self.symbol, self.lot_size)
                        trade.update({"exit_price": trade["sl"], "exit_ts": current_ts,
                                      "pnl": pnl, "result": "SL"})
                        all_trades.append(trade); cum_pnl += pnl
                        del open_trades[code]; continue

            # ── Build state ────────────────────────────────────────────────
            try:
                state = build_state(
                    symbol=self.symbol,
                    current_ts=current_ts,
                    tf_data=tf_data,
                    tf_ts_arrays=tf_ts_arrays,
                    candle_window=200,
                    _tf_cache=tf_cache,   # FIX B5: pass segment-scoped cache
                )
            except Exception as e:
                if self.debug:
                    print(f"  [WARN] build_state error at bar {bar_idx}: {e}")
                equity_curve.append((current_ts, round(cum_pnl, 4)))
                continue

            # ── Run strategy checks ────────────────────────────────────────
            for code, fn in strategy_fns.items():
                if code in open_trades:
                    continue

                try:
                    signal = fn(state, debug=self.debug)
                except Exception as e:
                    if self.debug:
                        print(f"  [{code}] check() error: {e}")
                    continue

                if not (signal and signal.get("trade")):
                    continue

                # F3: guard against None/invalid SL or TP
                entry = signal.get("entry") or bar_close
                sl    = signal.get("sl")
                tp    = signal.get("tp")

                if sl is None or tp is None:
                    continue
                if not (isinstance(sl, (int, float)) and isinstance(tp, (int, float))):
                    continue

                # F8: sanity — SL/TP must be on correct sides of entry
                trade_type = signal.get("type", "")
                if trade_type == "BUY"  and not (sl < entry < tp):
                    continue
                if trade_type == "SELL" and not (tp < entry < sl):
                    continue

                open_trades[code] = {
                    "strategy":      code,
                    "strategy_name": signal.get("strategy", code),
                    "symbol":        self.symbol,
                    "type":          trade_type,
                    "entry":         round(float(entry), 5),
                    "sl":            round(float(sl),    5),
                    "tp":            round(float(tp),    5),
                    "rr":            float(signal.get("rr", 0) or 0),
                    "confidence":    float(signal.get("confidence", 0) or 0),
                    "reason":        str(signal.get("reason", "")),
                    "entry_ts":      current_ts,
                    "lot":           self.lot_size,
                }

            equity_curve.append((current_ts, round(cum_pnl, 4)))

        # Close trades open at end of segment
        if entry_df is not None and len(entry_df) > 0:
            last_bar   = entry_df.iloc[bar_end - 1]
            last_close = float(last_bar["close"])
            last_ts    = int(entry_ts_arr[bar_end - 1])
            for code, trade in list(open_trades.items()):
                pnl = calc_pnl(trade, last_close, self.symbol, self.lot_size)
                trade.update({"exit_price": last_close, "exit_ts": last_ts,
                              "pnl": pnl, "result": "OPEN_AT_END"})
                all_trades.append(trade)
                cum_pnl += pnl

        return all_trades, equity_curve, cum_pnl

    # ─────────────────────────────────────────────────────────────────────────
    # Main run
    # ─────────────────────────────────────────────────────────────────────────

    def run(self) -> dict:
        print(f"\n{'='*64}", flush=True)
        print(f"  Backtest: {self.symbol}  strategies={self.strategies}", flush=True)
        print(f"  Entry TF: {self.entry_tf}  lot={self.lot_size}", flush=True)
        if self.walk_forward:
            print(f"  Walk-forward: {int(self.wf_split*100)}/{int((1-self.wf_split)*100)} split", flush=True)
        if self.monte_carlo:
            print(f"  Monte Carlo: {self.mc_iterations} iterations", flush=True)
        print(f"{'='*64}\n", flush=True)

        # ── Load data ─────────────────────────────────────────────────────
        print("Loading data from SQLite...", flush=True)
        tf_data     = {}
        tf_ts_arrays = {}
        for tf in self._tf_labels:
            df = load_bars(self.symbol, tf)
            if df.empty:
                print(f"  [WARN] No data for {self.symbol} {tf}")
                continue
            tf_data[tf]      = df
            tf_ts_arrays[tf] = df["ts_unix"].values.astype(np.int64)
            t0 = datetime.datetime.fromtimestamp(int(df["ts_unix"].iloc[0]),  tz=datetime.timezone.utc).strftime("%Y-%m-%d")
            t1 = datetime.datetime.fromtimestamp(int(df["ts_unix"].iloc[-1]), tz=datetime.timezone.utc).strftime("%Y-%m-%d")
            print(f"  {tf}: {len(df):,} bars  {t0} → {t1}")

        if self.entry_tf not in tf_data:
            raise ValueError(
                f"Entry TF {self.entry_tf} has no data.\n"
                "Run: start.bat --collect   to download market data."
            )

        # ── Load strategies ───────────────────────────────────────────────
        print("\nLoading strategies...", flush=True)
        strategy_modules = {}
        strategy_fns     = {}
        for code in self.strategies:
            try:
                mod = load_strategy(code)
                strategy_modules[code] = mod
                strategy_fns[code]     = mod.check
                print(f"  {code}: loaded OK")
            except Exception as e:
                print(f"  {code}: LOAD ERROR — {e}")

        # ── Full run ──────────────────────────────────────────────────────
        entry_df     = tf_data[self.entry_tf]
        entry_ts_arr = tf_ts_arrays[self.entry_tf]
        n_bars       = len(entry_df)
        warmup       = max(200, FRACTAL_GUARD * 3)

        print(f"\nFull run: {n_bars:,} bars on {self.entry_tf}, warmup={warmup}...")
        all_trades, equity_curve, cum_pnl = self._replay_bars(
            entry_df, entry_ts_arr, tf_data, tf_ts_arrays,
            strategy_modules, strategy_fns,
            bar_start=warmup, bar_end=n_bars,
            label="FULL",
        )

        results = _compute_stats(self.strategies, self.symbol, all_trades, equity_curve, cum_pnl)
        results["entry_tf"] = self.entry_tf

        print(f"\n{'='*64}")
        print(f"  FULL RUN COMPLETE")
        print(f"  Trades: {results['total_trades']}  WR: {results['win_rate']:.1f}%  "
              f"PnL: ${results['total_pnl']:+.2f}  DD: ${results['max_drawdown']:.2f}  "
              f"PF: {results['profit_factor']}")
        print(f"{'='*64}")

        # ── Walk-forward ──────────────────────────────────────────────────
        if self.walk_forward:
            effective_bars = n_bars - warmup
            split_offset   = int(effective_bars * self.wf_split)
            is_end         = warmup + split_offset          # in-sample end bar idx
            oos_start      = is_end                         # out-of-sample start
            split_ts       = int(entry_ts_arr[is_end]) if is_end < n_bars else int(entry_ts_arr[-1])

            print(f"\nWalk-forward in-sample: bars {warmup}–{is_end}  ({int(self.wf_split*100)}%)...")
            is_trades, is_curve, is_pnl = self._replay_bars(
                entry_df, entry_ts_arr, tf_data, tf_ts_arrays,
                strategy_modules, strategy_fns,
                bar_start=warmup, bar_end=is_end,
                label="IS",
            )

            print(f"\nWalk-forward out-of-sample: bars {oos_start}–{n_bars}  ({int((1-self.wf_split)*100)}%)...")
            oos_trades, oos_curve, oos_pnl = self._replay_bars(
                entry_df, entry_ts_arr, tf_data, tf_ts_arrays,
                strategy_modules, strategy_fns,
                bar_start=oos_start, bar_end=n_bars,
                label="OOS",
            )

            is_stats  = _compute_stats(self.strategies, self.symbol, is_trades,  is_curve,  is_pnl)
            oos_stats = _compute_stats(self.strategies, self.symbol, oos_trades, oos_curve, oos_pnl)

            results["walk_forward"] = {
                "split_ts":      split_ts,
                "split_pct":     int(self.wf_split * 100),
                "in_sample":     {
                    "trades":       is_stats["total_trades"],
                    "win_rate":     is_stats["win_rate"],
                    "total_pnl":    is_stats["total_pnl"],
                    "max_drawdown": is_stats["max_drawdown"],
                    "profit_factor":is_stats["profit_factor"],
                    "equity_curve": is_stats["equity_curve"],
                },
                "out_of_sample": {
                    "trades":       oos_stats["total_trades"],
                    "win_rate":     oos_stats["win_rate"],
                    "total_pnl":    oos_stats["total_pnl"],
                    "max_drawdown": oos_stats["max_drawdown"],
                    "profit_factor":oos_stats["profit_factor"],
                    "equity_curve": oos_stats["equity_curve"],
                },
            }

            print(f"\n  IS:  {is_stats['total_trades']} trades  WR={is_stats['win_rate']:.1f}%  "
                  f"PnL=${is_stats['total_pnl']:+.2f}")
            print(f"  OOS: {oos_stats['total_trades']} trades  WR={oos_stats['win_rate']:.1f}%  "
                  f"PnL=${oos_stats['total_pnl']:+.2f}")

        # ── Monte Carlo ───────────────────────────────────────────────────
        closed_trades = [t for t in all_trades if t.get("result") != "OPEN_AT_END"]
        if self.monte_carlo and len(closed_trades) >= 5:
            print(f"\nMonte Carlo: {self.mc_iterations} iterations on {len(closed_trades)} closed trades...")
            mc = _compute_monte_carlo(closed_trades, self.mc_iterations)
            results["monte_carlo"] = mc
            print(f"  MC p5=${mc['final_p5']:+.2f}  p50=${mc['final_p50']:+.2f}  "
                  f"p95=${mc['final_p95']:+.2f}  P(positive)={mc['prob_positive']}%")
        elif self.monte_carlo:
            # BUG FIX (B-MC-KEY): Always set results["monte_carlo"] so viewer JS
            # never gets a missing key.  None signals "skipped"; all viewer guards
            # check truthiness so None is handled safely.
            results["monte_carlo"] = None
            print(f"\n  [INFO] Monte Carlo skipped — not enough closed trades ({len(closed_trades)} < 5)")

        return results
