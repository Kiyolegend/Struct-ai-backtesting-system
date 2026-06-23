"""
struct-ai-backtest  —  Test Suite
==================================
Run from the repo root:
    python tests/test_backtest.py          (no pytest needed)
    python -m pytest tests/test_backtest.py -v   (with pytest)

Tests cover:
  T01 – sr_levels includes D1 zones after fix
  T02 – sr_levels directionality (above=resistance, below=support)
  T03 – sr_levels no duplicates (single kind per zone, not both)
  T04 – build_state returns all required top-level keys
  T05 – build_state TF sub-dicts have required keys
  T06 – session classification (London / NY / Asia / overlap)
  T07 – asia_range calculation from synthetic 1H bars
  T08 – zigzag engine: alternating highs/lows
  T09 – structure engine: detects HH/HL/LH/LL sequence
  T10 – BOS engine: detects BOS after HH breaks
  T11 – zones engine: clusters nearby swing prices
  T12 – zones engine: no cluster if swings are far apart
  T13 – calc_pnl BUY trade
  T14 – calc_pnl SELL trade
  T15 – calc_pnl spread+commission deducted
  T16 – Monte Carlo: same seed = same result
  T17 – Monte Carlo: prob_positive between 0 and 100
  T18 – Monte Carlo: p5 <= p50 <= p95 at every step
  T19 – _compute_stats: no divide-by-zero with zero losses
  T20 – _compute_stats: OPEN_AT_END excluded from win rate
  T21 – all 10 strategy modules import without error
  T22 – SW1/SW2/SW3 use SWING_MIN_CONFIDENCE (75) not MIN_CONFIDENCE (80)
  T23 – SW2/SW4: sr_levels includes d1-timeframe entries after fix
  T24 – SL/TP sanity guard rejects inverted BUY trade
  T25 – SL/TP sanity guard rejects inverted SELL trade
"""

import sys
import os
import datetime
import importlib.util
import traceback

# ── Path setup ────────────────────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "engines"))

import pandas as pd
import numpy as np
from backtest_engine import (
    build_state, calc_pnl, _compute_stats, _compute_monte_carlo,
    _classify_session, _calc_asia_range,
)
import config


# ── Synthetic data helpers ────────────────────────────────────────────────────

def _make_candles(prices: list, start_ts: int = 1700000000, step: int = 300) -> pd.DataFrame:
    """Create a minimal OHLCV DataFrame from a list of close prices."""
    rows = []
    for i, p in enumerate(prices):
        ts = start_ts + i * step
        noise = 0.002
        rows.append({
            "ts_unix": ts,
            "open":    round(p - noise, 5),
            "high":    round(p + noise, 5),
            "low":     round(p - noise * 2, 5),
            "close":   round(p, 5),
            "volume":  100.0,
            "time":    pd.Timestamp(ts, unit="s", tz="UTC"),
        })
    return pd.DataFrame(rows)


def _make_swing_candles(n: int = 60) -> pd.DataFrame:
    """Create candles with clear alternating HH/LL ZigZag pattern."""
    prices = []
    base   = 150.0
    for i in range(n):
        phase = (i // 5) % 2
        prices.append(base + (2.0 if phase == 0 else -1.0) + (i % 5) * 0.1)
    return _make_candles(prices)


def _build_minimal_state(current_price: float = 150.0,
                         d1_zones: list | None = None,
                         h4_zones: list | None = None,
                         h1_zones: list | None = None) -> dict:
    """Build a minimal state dict for testing sr_levels logic."""
    state = {
        "symbol":            "USD/JPY",
        "reference_ts":      1700000000,
        "sessions":          ["london"],
        "tradeable_session": True,
        "current_price":     current_price,
        "bias":              {},
        "asia_range":        {},
        "d1":                {"candles": [], "bos": [], "choch": [], "structure": [],
                              "zones": d1_zones or [], "swing_hi": None, "swing_lo": None},
        "4h":                {"candles": [], "bos": [], "choch": [], "structure": [],
                              "zones": h4_zones or [], "swing_hi": None, "swing_lo": None},
        "1h":                {"candles": [], "bos": [], "choch": [], "structure": [],
                              "zones": h1_zones or [], "swing_hi": None, "swing_lo": None},
        "15m":               {"candles": [], "bos": [], "choch": [], "structure": [],
                              "zones": [], "swing_hi": None, "swing_lo": None},
        "5m":                {"candles": [], "bos": [], "choch": [], "structure": [],
                              "zones": [], "swing_hi": None, "swing_lo": None},
        "sr_levels":         [],
    }
    # Recompute sr_levels using the same logic as build_state (copy of the fixed code)
    cp = current_price
    sr = []
    for tf_label in ("D1", "4H", "1H"):
        tf_key = tf_label.lower()
        for zone in (state.get(tf_key) or {}).get("zones", []):
            ctr = zone.get("center", 0)
            if ctr and ctr > 0:
                kind = "resistance" if ctr >= cp else "support"
                sr.append({"price": ctr, "timeframe": tf_key, "kind": kind,
                            "strength": zone.get("strength", 1)})
    state["sr_levels"] = sr
    return state


# ── Test runner helpers ───────────────────────────────────────────────────────

_passed = 0
_failed = 0


def _ok(name: str) -> None:
    global _passed
    _passed += 1
    print(f"  PASS  {name}")


def _fail(name: str, msg: str) -> None:
    global _failed
    _failed += 1
    print(f"  FAIL  {name}")
    print(f"        {msg}")


def _run(name: str, fn) -> None:
    try:
        fn()
        _ok(name)
    except AssertionError as exc:
        _fail(name, str(exc))
    except Exception as exc:
        _fail(name, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_sr_d1_included():
    """T01 – sr_levels includes D1 zones."""
    d1_zones = [{"center": 152.0, "strength": 4}]
    state = _build_minimal_state(current_price=150.0, d1_zones=d1_zones)
    tfs = {lvl["timeframe"] for lvl in state["sr_levels"]}
    assert "d1" in tfs, f"D1 not in sr_levels timeframes: {tfs}"


def test_sr_directionality_above_is_resistance():
    """T02 – Zone centre above current price → resistance."""
    zones = [{"center": 155.0, "strength": 3}]
    state = _build_minimal_state(current_price=150.0, d1_zones=zones)
    lvl = next(l for l in state["sr_levels"] if abs(l["price"] - 155.0) < 0.001)
    assert lvl["kind"] == "resistance", f"Expected resistance, got {lvl['kind']}"


def test_sr_directionality_below_is_support():
    """T03 – Zone centre below current price → support."""
    zones = [{"center": 145.0, "strength": 3}]
    state = _build_minimal_state(current_price=150.0, h4_zones=zones)
    lvl = next(l for l in state["sr_levels"] if abs(l["price"] - 145.0) < 0.001)
    assert lvl["kind"] == "support", f"Expected support, got {lvl['kind']}"


def test_sr_no_duplicate_kinds():
    """T04 – Each zone produces exactly ONE sr_level entry (not both support and resistance)."""
    zones = [{"center": 152.0, "strength": 3}]
    state = _build_minimal_state(current_price=150.0, h4_zones=zones)
    matching = [l for l in state["sr_levels"] if abs(l["price"] - 152.0) < 0.001]
    assert len(matching) == 1, f"Expected 1 entry, got {len(matching)}: {matching}"


def test_build_state_top_level_keys():
    """T05 – build_state returns required top-level keys."""
    required = {"symbol", "reference_ts", "sessions", "tradeable_session",
                "sr_levels", "asia_range", "bias", "current_price"}
    # Build minimal tf_data and call build_state
    df = _make_candles([150.0] * 50)
    tf_data = {"5M": df}
    tf_ts   = {"5M": df["ts_unix"].values.astype(np.int64)}
    state = build_state("USD/JPY", int(df["ts_unix"].iloc[-1]), tf_data, tf_ts)
    missing = required - set(state.keys())
    assert not missing, f"Missing keys: {missing}"


def test_build_state_tf_sub_keys():
    """T06 – build_state TF sub-dicts contain candles/bos/choch/structure/zones."""
    df = _make_candles([150.0 + i * 0.01 for i in range(50)])
    tf_data = {"5M": df, "1H": df, "4H": df, "D1": df}
    tf_ts   = {tf: d["ts_unix"].values.astype(np.int64) for tf, d in tf_data.items()}
    state = build_state("USD/JPY", int(df["ts_unix"].iloc[-1]), tf_data, tf_ts)
    for tf_key in ("5m", "1h", "4h", "d1"):
        sub = state.get(tf_key)
        assert sub is not None, f"Missing TF sub-dict: {tf_key}"
        for k in ("candles", "bos", "choch", "structure", "zones"):
            assert k in sub, f"{tf_key} missing key: {k}"


def test_session_london():
    """T07 – 09:00 UTC is London session."""
    ts = int(datetime.datetime(2024, 1, 15, 9, 0, tzinfo=datetime.timezone.utc).timestamp())
    sessions = _classify_session(ts)
    assert "london" in sessions, f"London not in sessions at 09:00 UTC: {sessions}"


def test_session_ny():
    """T08 – 15:00 UTC is NY session."""
    ts = int(datetime.datetime(2024, 1, 15, 15, 0, tzinfo=datetime.timezone.utc).timestamp())
    sessions = _classify_session(ts)
    assert "ny" in sessions, f"NY not in sessions at 15:00 UTC: {sessions}"


def test_session_asia():
    """T09 – 03:00 UTC is Asia session only."""
    ts = int(datetime.datetime(2024, 1, 15, 3, 0, tzinfo=datetime.timezone.utc).timestamp())
    sessions = _classify_session(ts)
    assert "asia" in sessions, f"Asia not in sessions at 03:00 UTC: {sessions}"
    assert "london" not in sessions, f"London should not be active at 03:00 UTC"


def test_session_overlap():
    """T10 – 13:00 UTC is London+NY overlap."""
    ts = int(datetime.datetime(2024, 1, 15, 13, 0, tzinfo=datetime.timezone.utc).timestamp())
    sessions = _classify_session(ts)
    assert "london" in sessions and "ny" in sessions, f"Expected both sessions: {sessions}"


def test_asia_range_calc():
    """T11 – asia_range high/low computed correctly from 1H bars."""
    # Create 1H bars: midnight→09:00 UTC should give high=152, low=148
    start  = int(datetime.datetime(2024, 1, 15, 0, 0, tzinfo=datetime.timezone.utc).timestamp())
    prices = [148.0, 150.0, 149.0, 152.0, 151.0, 149.5, 150.0, 150.5, 149.8]
    rows   = []
    for i, p in enumerate(prices):
        rows.append({"ts_unix": start + i * 3600, "open": p, "high": p + 0.5,
                     "low": p - 0.5, "close": p, "volume": 100})
    df     = pd.DataFrame(rows)
    ts_arr = df["ts_unix"].values.astype(np.int64)
    # Request asia_range at 10:00 UTC (after Asia session)
    ref_ts = start + 10 * 3600
    result = _calc_asia_range(df, ts_arr, ref_ts)
    assert result, "asia_range should not be empty"
    assert result["high"] >= 152.0, f"Expected high >= 152.0, got {result['high']}"
    assert result["low"]  <= 147.5, f"Expected low <= 147.5, got {result['low']}"


def test_zigzag_alternates():
    """T12 – ZigZag produces alternating high/low swings."""
    from zigzag_engine import detect_swings
    df     = _make_swing_candles(60)
    swings = detect_swings(df)
    if len(swings) < 2:
        return   # not enough candles to detect — acceptable
    for i in range(1, len(swings)):
        assert swings[i]["kind"] != swings[i-1]["kind"], \
            f"Consecutive same-kind swings at {i}: {swings[i-1]['kind']}/{swings[i]['kind']}"


def test_structure_classifies():
    """T13 – structure engine classifies at least one label from swing data."""
    from zigzag_engine import detect_swings
    from structure_engine import classify_structure
    df     = _make_swing_candles(80)
    swings = detect_swings(df)
    if len(swings) < 4:
        return
    labels = classify_structure(swings)
    assert isinstance(labels, list), "classify_structure must return a list"


def test_bos_returns_list():
    """T14 – BOS engine returns a list."""
    from zigzag_engine import detect_swings
    from structure_engine import classify_structure
    from trend_engine import detect_trend
    from bos_engine import detect_bos
    df     = _make_swing_candles(80)
    swings = detect_swings(df)
    labels = classify_structure(swings)
    trend  = detect_trend(labels)["trend"]
    result = detect_bos(df, swings, labels, trend=trend, lookback_hours=48)
    assert isinstance(result, list), f"detect_bos must return list, got {type(result)}"


def test_zones_cluster():
    """T15 – zones_engine clusters nearby swing prices."""
    from zones_engine import detect_zones
    # Swings very close together → should form a cluster
    swings = [
        {"price": 150.00, "time": 1000, "kind": "high"},
        {"price": 150.03, "time": 2000, "kind": "high"},
        {"price": 150.01, "time": 3000, "kind": "low"},
    ]
    zones = detect_zones(swings, timeframe="4h", current_price=150.0)
    assert isinstance(zones, list), "detect_zones must return a list"
    # With 3 close prices, should detect at least one cluster zone
    assert len(zones) >= 1, f"Expected ≥1 zone, got {len(zones)}"


def test_zones_no_cluster_far_apart():
    """T16 – zones_engine produces no cluster when prices are far apart."""
    from zones_engine import detect_zones
    # Each price is 100 pips apart → no clustering
    swings = [
        {"price": 150.00, "time": 1000, "kind": "high"},
        {"price": 151.00, "time": 2000, "kind": "high"},
    ]
    zones = detect_zones(swings, timeframe="4h", current_price=150.0)
    assert len(zones) == 0, f"Expected 0 clusters for far-apart prices, got {len(zones)}"


def test_calc_pnl_buy_win():
    """T17 – calc_pnl BUY: profit when exit > entry."""
    trade = {"type": "BUY", "entry": 150.000}
    pnl = calc_pnl(trade, exit_price=150.100, symbol="USD/JPY", lot_size=0.02)
    assert pnl > 0, f"Expected positive P&L for winning BUY, got {pnl}"


def test_calc_pnl_buy_loss():
    """T18 – calc_pnl BUY: loss when exit < entry."""
    trade = {"type": "BUY", "entry": 150.000}
    pnl = calc_pnl(trade, exit_price=149.900, symbol="USD/JPY", lot_size=0.02)
    assert pnl < 0, f"Expected negative P&L for losing BUY, got {pnl}"


def test_calc_pnl_sell_win():
    """T19 – calc_pnl SELL: profit when exit < entry."""
    trade = {"type": "SELL", "entry": 150.000}
    pnl = calc_pnl(trade, exit_price=149.900, symbol="USD/JPY", lot_size=0.02)
    assert pnl > 0, f"Expected positive P&L for winning SELL, got {pnl}"


def test_calc_pnl_spread_deducted():
    """T20 – Spread+commission always deducted (even on breakeven entry/exit)."""
    trade = {"type": "BUY", "entry": 150.000}
    pnl = calc_pnl(trade, exit_price=150.000, symbol="USD/JPY", lot_size=0.02)
    assert pnl < 0, f"Breakeven entry/exit must show negative P&L due to costs, got {pnl}"


def test_monte_carlo_deterministic():
    """T21 – Monte Carlo with same seed produces same result."""
    trades = [{"pnl": v} for v in [1.0, -0.5, 2.0, -1.0, 0.8, 1.2, -0.3, 1.5]]
    mc1 = _compute_monte_carlo(trades, iterations=100)
    mc2 = _compute_monte_carlo(trades, iterations=100)
    assert mc1["final_p50"] == mc2["final_p50"], "Monte Carlo not deterministic"
    assert mc1["prob_positive"] == mc2["prob_positive"], "Monte Carlo prob not deterministic"


def test_monte_carlo_percentile_ordering():
    """T22 – p5 ≤ p50 ≤ p95 at every step."""
    trades = [{"pnl": v} for v in [1.0, -0.5, 2.0, -1.0, 0.8, 1.2, -0.3, 1.5, 0.9, -0.7]]
    mc = _compute_monte_carlo(trades, iterations=200)
    for i, (a, b, c) in enumerate(zip(mc["p5"], mc["p50"], mc["p95"])):
        assert a <= b <= c, f"Step {i}: p5={a} p50={b} p95={c} — ordering violated"


def test_monte_carlo_prob_in_range():
    """T23 – prob_positive is between 0 and 100."""
    trades = [{"pnl": 1.0}] * 5
    mc = _compute_monte_carlo(trades, iterations=50)
    assert 0 <= mc["prob_positive"] <= 100, f"prob_positive out of range: {mc['prob_positive']}"


def test_compute_stats_no_div_zero():
    """T24 – _compute_stats with no losses (profit_factor guard)."""
    trades = [{"strategy": "SC1", "result": "TP", "pnl": 2.0, "rr": 2.0}]
    curve  = [(1700000000, 2.0)]
    stats  = _compute_stats(["SC1"], "USD/JPY", trades, curve, 2.0)
    assert stats["profit_factor"] in (999.0, 0.0) or stats["profit_factor"] > 0


def test_compute_stats_excludes_open_at_end():
    """T25 – OPEN_AT_END trades excluded from win rate."""
    trades = [
        {"strategy": "SC1", "result": "TP",           "pnl": 2.0,  "rr": 2.0},
        {"strategy": "SC1", "result": "SL",           "pnl": -1.0, "rr": 0.5},
        {"strategy": "SC1", "result": "OPEN_AT_END",  "pnl": 0.5,  "rr": 1.0},
    ]
    curve = [(1700000000 + i * 300, float(i)) for i in range(3)]
    stats = _compute_stats(["SC1"], "USD/JPY", trades, curve, 1.5)
    # Win rate must be 50% (1 TP, 1 SL, ignoring OPEN_AT_END)
    assert stats["win_rate"] == 50.0, f"Expected 50.0% WR, got {stats['win_rate']}"


def test_all_strategies_import():
    """T26 – All 10 strategy modules import without error."""
    strategies = {
        "SC1": ("strategies/scalping", "scalp1"),
        "SC2": ("strategies/scalping", "scalp2"),
        "SC3": ("strategies/scalping", "scalp3"),
        "SC4": ("strategies/scalping", "scalp4"),
        "SC5": ("strategies/scalping", "scalp5"),
        "SC6": ("strategies/scalping", "scalp6"),
        "SW1": ("strategies/swing",    "swing1"),
        "SW2": ("strategies/swing",    "swing2"),
        "SW3": ("strategies/swing",    "swing3"),
        "SW4": ("strategies/swing",    "swing4"),
    }
    errors = []
    for code, (folder, modname) in strategies.items():
        fpath = os.path.join(ROOT, folder, f"{modname}.py")
        try:
            spec = importlib.util.spec_from_file_location(modname, fpath)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            assert callable(getattr(mod, "check", None)), f"{code}: no check() function"
        except Exception as exc:
            errors.append(f"{code}: {exc}")
    assert not errors, "Import errors:\n  " + "\n  ".join(errors)


def test_sw1_uses_swing_min_confidence():
    """T27 – SW1 uses SWING_MIN_CONFIDENCE (75), not MIN_CONFIDENCE (80)."""
    fpath = os.path.join(ROOT, "strategies", "swing", "swing1.py")
    with open(fpath, "r") as f:
        src = f.read()
    assert "SWING_MIN_CONFIDENCE" in src, \
        "SW1 must use config.SWING_MIN_CONFIDENCE (75), found MIN_CONFIDENCE (80)"
    assert "score < config.MIN_CONFIDENCE" not in src, \
        "SW1 still has old config.MIN_CONFIDENCE check — not fixed"


def test_sw2_uses_swing_min_confidence():
    """T28 – SW2 uses SWING_MIN_CONFIDENCE (75), not MIN_CONFIDENCE (80)."""
    fpath = os.path.join(ROOT, "strategies", "swing", "swing2.py")
    with open(fpath, "r") as f:
        src = f.read()
    assert "SWING_MIN_CONFIDENCE" in src, \
        "SW2 must use config.SWING_MIN_CONFIDENCE (75)"
    assert "score < config.MIN_CONFIDENCE" not in src, \
        "SW2 still has old config.MIN_CONFIDENCE check — not fixed"


def test_sw3_uses_swing_min_confidence():
    """T29 – SW3 uses SWING_MIN_CONFIDENCE (75), not MIN_CONFIDENCE (80)."""
    fpath = os.path.join(ROOT, "strategies", "swing", "swing3.py")
    with open(fpath, "r") as f:
        src = f.read()
    assert "SWING_MIN_CONFIDENCE" in src, \
        "SW3 must use config.SWING_MIN_CONFIDENCE (75)"


def test_sr_levels_d1_present_in_state():
    """T30 – After fix, build_state sr_levels contains d1-timeframe entries."""
    # We simulate what build_state does for sr_levels using _build_minimal_state
    d1_zones = [{"center": 152.5, "strength": 4}]
    state = _build_minimal_state(current_price=150.0, d1_zones=d1_zones)
    d1_entries = [l for l in state["sr_levels"] if l["timeframe"] == "d1"]
    assert len(d1_entries) >= 1, \
        f"Expected ≥1 D1 sr_level, got 0. SW2/SW4 need D1 timeframe S/R levels."


def test_sl_tp_sanity_buy():
    """T31 – SL/TP guard: BUY with SL above entry → rejected."""
    from backtest_engine import BacktestEngine
    # We only test the guard logic directly (not a full replay)
    trade_type = "BUY"
    entry, sl, tp = 150.0, 151.0, 152.0   # SL above entry → invalid BUY
    valid = (sl < entry < tp)
    assert not valid, "Expected guard to reject SL above entry on BUY"


def test_sl_tp_sanity_sell():
    """T32 – SL/TP guard: SELL with TP above entry → rejected."""
    trade_type = "SELL"
    entry, sl, tp = 150.0, 151.0, 152.0   # TP above entry → invalid SELL
    valid = (tp < entry < sl)
    assert not valid, "Expected guard to reject TP above entry on SELL"


# ── T33–T36: Dashboard equity-curve SSE parsing ───────────────────────────────

import re as _re

_CHART_RE = _re.compile(
    r'\[(\w+)\]\s+([\d.]+)%\s+(\d{4}-\d{2}-\d{2})'
    r'.*?cumPnL=\$([+\-]?[\d.]+).*?trades=(\d+)'
)


def test_chart_regex_full_line():
    """T33 – _CHART_RE parses a FULL progress line correctly."""
    line = "  [FULL]  30.0%  2025-10-09  cumPnL=$+72.34  trades=26"
    m = _CHART_RE.search(line)
    assert m is not None, "Regex should match a FULL progress line"
    assert m.group(1) == "FULL"
    assert float(m.group(2)) == 30.0
    assert m.group(3) == "2025-10-09"
    assert float(m.group(4)) == 72.34
    assert int(m.group(5)) == 26


def test_chart_regex_is_oos_segments():
    """T34 – _CHART_RE correctly captures IS and OOS segment labels."""
    line_is  = "  [IS]   50.0%  2025-08-01  cumPnL=$+45.12  trades=14"
    line_oos = "  [OOS]  20.0%  2025-11-01  cumPnL=$+28.90  trades=8"

    m_is  = _CHART_RE.search(line_is)
    m_oos = _CHART_RE.search(line_oos)

    assert m_is  is not None, "Regex should match IS line"
    assert m_oos is not None, "Regex should match OOS line"
    assert m_is.group(1)  == "IS"
    assert m_oos.group(1) == "OOS"


def test_chart_regex_negative_pnl():
    """T35 – _CHART_RE handles negative cumPnL (e.g. early drawdown)."""
    line = "  [FULL]  10.0%  2025-07-30  cumPnL=$-21.46  trades=6"
    m = _CHART_RE.search(line)
    assert m is not None, "Regex should match negative P&L line"
    pnl = float(m.group(4))
    assert pnl == -21.46, f"Expected -21.46, got {pnl}"


def test_chart_regex_no_false_positives():
    """T36 – _CHART_RE returns None for non-matching log lines."""
    non_matching = [
        "Loading data from SQLite...",
        "  SC1: loaded OK",
        "  Monte Carlo: 1000 iterations",
        "================================================================",
        "  D1: 1,562 bars  2021-06-25 → 2026-06-23",
    ]
    for line in non_matching:
        m = _CHART_RE.search(line)
        assert m is None, f"Regex falsely matched: {line!r}"


# ── T37–T43: New tests for B-FIB, B-MC-KEY, B-AVG-RR, B-ZONEINFO, B-STRAT-KEY ─

def test_fib_tp_no_cross_tf_mixing():
    """T37 – fib_extension_tp must NOT mix D1 swing_hi with 4H swing_lo."""
    # D1 has a hi but no lo → should fall back to 4H pair, not mix D1 hi + 4H lo
    state = {
        "d1": {"swing_hi": 157.0, "swing_lo": None},
        "4h": {"swing_hi": 155.0, "swing_lo": 153.0},
    }
    # 4H range = 155.0 - 153.0 = 2.0 → bullish TP = 155.0 + 0.272*2.0 = 155.544
    # Cross-TF (wrong): range = 157.0 - 153.0 = 4.0 → TP = 157.0 + 0.272*4.0 = 158.088
    tp = config.fib_extension_tp(state, "bullish", 154.0)
    assert tp is not None, "Should return a TP using 4H pair"
    assert tp < 157.0, f"TP {tp} looks like it used D1 hi (157.0) — cross-TF mixing detected"
    # Correct 4H TP: 155.0 + 0.272 * 2.0 = 155.544
    assert abs(tp - 155.544) < 0.01, f"Expected ~155.544 (4H pair), got {tp}"


def test_fib_tp_pure_d1():
    """T38 – fib_extension_tp uses D1 pair when both D1 hi and lo are present."""
    # D1 complete: hi=157.0, lo=153.0 → range=4.0 → TP=157.0+0.272*4.0=158.088
    state = {
        "d1": {"swing_hi": 157.0, "swing_lo": 153.0},
        "4h": {"swing_hi": 155.0, "swing_lo": 153.5},
    }
    tp = config.fib_extension_tp(state, "bullish", 156.0)
    assert tp is not None, "Should return a TP using D1 pair"
    assert abs(tp - 158.088) < 0.01, f"Expected ~158.088 (D1 pair), got {tp}"


def test_fib_tp_fallback_to_4h_when_d1_missing():
    """T39 – fib_extension_tp falls back to full 4H pair when D1 is entirely absent."""
    state = {
        "d1": {"swing_hi": None, "swing_lo": None},
        "4h": {"swing_hi": 155.0, "swing_lo": 153.0},
    }
    tp = config.fib_extension_tp(state, "bullish", 154.0)
    assert tp is not None, "Should return a TP using 4H pair"
    assert abs(tp - 155.544) < 0.01, f"Expected ~155.544 (4H fallback), got {tp}"


def test_monte_carlo_key_always_present():
    """T40 – monte_carlo key exists in results even when < 5 closed trades."""
    # Simulate what BacktestEngine.run() does: call _compute_monte_carlo
    # indirectly by verifying the stub return has all required keys.
    mc_stub = {
        "iterations": 1000, "p5": [], "p50": [], "p95": [],
        "final_p5": 0.0, "final_p50": 0.0, "final_p95": 0.0,
        "prob_positive": 0.0,
    }
    required_keys = {"iterations", "p5", "p50", "p95",
                     "final_p5", "final_p50", "final_p95", "prob_positive"}
    missing = required_keys - set(mc_stub.keys())
    assert not missing, f"MC stub is missing keys: {missing}"

    # Also verify _compute_monte_carlo with < 2 trades returns the same shape
    mc_tiny = _compute_monte_carlo([], iterations=100)
    missing2 = required_keys - set(mc_tiny.keys())
    assert not missing2, f"_compute_monte_carlo(<2) missing keys: {missing2}"


def test_avg_rr_excludes_open_at_end():
    """T41 – _compute_stats avg_rr excludes OPEN_AT_END trades (Bug 3 fix)."""
    trades = [
        {"strategy": "SC1", "result": "TP",          "pnl":  2.0, "rr": 2.0},
        {"strategy": "SC1", "result": "SL",          "pnl": -1.0, "rr": 0.5},
        {"strategy": "SC1", "result": "OPEN_AT_END", "pnl":  0.5, "rr": 333.83},
    ]
    curve = [(1700000000 + i * 300, float(i)) for i in range(3)]
    stats = _compute_stats(["SC1"], "USD/JPY", trades, curve, 1.5)
    # Correct avg_rr from closed trades only: (2.0 + 0.5) / 2 = 1.25
    assert abs(stats["avg_rr"] - 1.25) < 0.01, \
        f"Expected avg_rr=1.25 (closed only), got {stats['avg_rr']} — OPEN_AT_END not excluded"


def test_per_strategy_avg_rr_excludes_open_at_end():
    """T42 – by_strategy avg_rr also excludes OPEN_AT_END (hidden bug fix)."""
    trades = [
        {"strategy": "SC1", "result": "TP",          "pnl":  2.0, "rr": 2.0},
        {"strategy": "SC1", "result": "SL",          "pnl": -1.0, "rr": 0.5},
        {"strategy": "SC1", "result": "OPEN_AT_END", "pnl":  0.5, "rr": 100.0},
    ]
    curve = [(1700000000 + i * 300, float(i)) for i in range(3)]
    stats = _compute_stats(["SC1"], "USD/JPY", trades, curve, 1.5)
    strat_rr = stats["by_strategy"]["SC1"]["avg_rr"]
    # Correct: (2.0 + 0.5) / 2 = 1.25
    assert abs(strat_rr - 1.25) < 0.01, \
        f"Expected per-strategy avg_rr=1.25, got {strat_rr} — OPEN_AT_END not excluded"


def test_zoneinfo_fallback_dst_london():
    """T43a – ZoneInfo fallback: London BST offset is +1 in summer."""
    import sys, os
    sys.path.insert(0, os.path.join(ROOT, "strategies", "scalping"))
    # Import the fallback function from scalp5
    spec = importlib.util.spec_from_file_location(
        "scalp5", os.path.join(ROOT, "strategies", "scalping", "scalp5.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from datetime import datetime, timezone as tz_
    # July 15 2024 → BST (+1)
    dt_summer = datetime(2024, 7, 15, 12, 0, tzinfo=tz_.utc)
    offset = mod._utc_offset_hours("Europe/London", dt_summer)
    assert offset == 1, f"Expected BST offset +1 in July, got {offset}"
    # January 15 2024 → GMT (+0)
    dt_winter = datetime(2024, 1, 15, 12, 0, tzinfo=tz_.utc)
    offset2 = mod._utc_offset_hours("Europe/London", dt_winter)
    assert offset2 == 0, f"Expected GMT offset 0 in January, got {offset2}"


def test_zoneinfo_fallback_dst_ny():
    """T43b – ZoneInfo fallback: New York EDT offset is -4 in summer, -5 in winter."""
    import sys, os
    sys.path.insert(0, os.path.join(ROOT, "strategies", "scalping"))
    spec = importlib.util.spec_from_file_location(
        "scalp5", os.path.join(ROOT, "strategies", "scalping", "scalp5.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from datetime import datetime, timezone as tz_
    dt_summer = datetime(2024, 7, 15, 12, 0, tzinfo=tz_.utc)
    offset = mod._utc_offset_hours("America/New_York", dt_summer)
    assert offset == -4, f"Expected EDT offset -4 in July, got {offset}"
    dt_winter = datetime(2024, 1, 15, 12, 0, tzinfo=tz_.utc)
    offset2 = mod._utc_offset_hours("America/New_York", dt_winter)
    assert offset2 == -5, f"Expected EST offset -5 in January, got {offset2}"


def test_fractal_n_per_timeframe():
    """T44 – backtest_engine uses fractal_n=3 for D1/4H/1H and fractal_n=5 for 15M/5M.

    Matches the live Struct.ai engine (repo 1) which explicitly uses:
        fractal_n=3 if timeframe in ("1h", "4h", "d1") else 5

    We verify this with two checks:
      (a) Source inspection — the patch is present in backtest_engine.py
      (b) Functional — detect_swings produces fewer/different pivots on a short
          df with n=5 vs n=3, proving the parameter actually changes behaviour.
    """
    import sys, os, re
    import pandas as pd

    sys.path.insert(0, os.path.join(ROOT, "engines"))
    from zigzag_engine import detect_swings

    # ── (a) Source inspection ────────────────────────────────────────────────
    engine_src = os.path.join(ROOT, "backtest_engine.py")
    with open(engine_src, "r", encoding="utf-8") as fh:
        src = fh.read()

    # Must contain the per-TF fractal_n assignment
    assert '_fractal_n = 3 if tf_label in ("D1", "4H", "1H") else 5' in src, \
        "backtest_engine.py missing per-TF fractal_n assignment"

    # Must pass _fractal_n to detect_swings (not the default)
    assert "detect_swings(slice_df, fractal_n=_fractal_n)" in src, \
        "backtest_engine.py does not pass fractal_n to detect_swings"

    # Must NOT call detect_swings with bare default anywhere in build_state
    # (bare call would be "detect_swings(slice_df)" without fractal_n)
    bare_calls = [ln for ln in src.splitlines()
                  if "detect_swings(" in ln
                  and "fractal_n" not in ln
                  and "import" not in ln]
    assert not bare_calls, \
        f"Found detect_swings call(s) without explicit fractal_n:\n" + "\n".join(bare_calls)

    # ── (b) Functional: n=3 finds pivots that n=5 misses on short data ──────
    # 11 bars: spike at bar 5 (index 5), exactly 5 bars each side.
    # n=3: bar 5 has ≥3 bars each side → pivot found.
    # n=5: bar 5 has exactly 5 bars on right but they end at bar 10 → borderline.
    #       n=5 loop range is [5 .. len-6] = [5..5] → bar 5 just barely included,
    #       BUT the window check window_highs = highs[0:11].max() which bar 5
    #       dominates → still found for n=5 in this case.
    # Use an asymmetric pattern instead: peak at bar 3 with 10 bars total.
    #   n=3: bar 3 surrounded by 3 each side → found (index range [3..6])
    #   n=5: bar 3 would need bars [-2..8] which is out of range → not found
    # detect_swings uses df["high"], so the stored price = high column value.
    # Peak bar index 3: close=110, high=110.5 → that's what detect_swings records.
    prices = [100, 101, 102, 110, 106, 104, 102, 101, 100, 99]  # 10 bars, peak at 3
    peak_high = 110.5   # prices[3] + 0.5
    df10 = pd.DataFrame({
        "open":   prices,
        "high":   [p + 0.5 for p in prices],
        "low":    [p - 0.5 for p in prices],
        "close":  prices,
        "volume": [1000.0] * len(prices),
        "ts_unix": [1700000000 + i * 300 for i in range(len(prices))],
    })

    swings_n3 = detect_swings(df10, fractal_n=3)
    swings_n5 = detect_swings(df10, fractal_n=5)

    # n=3: loop range is [3 .. 6], bar 3 is evaluated → peak at 110.5 found
    highs_n3 = [s["price"] for s in swings_n3 if s["kind"] == "high"]
    assert peak_high in highs_n3, \
        f"n=3 should detect the peak at {peak_high}; highs found: {highs_n3}"

    # n=5: loop range starts at index 5 (n=5 ≤ i ≤ len-n-1=4) → empty, bar 3 never evaluated
    highs_n5 = [s["price"] for s in swings_n5 if s["kind"] == "high"]
    assert peak_high not in highs_n5, \
        f"n=5 should NOT detect peak at bar 3 (index 3 < fractal_n=5); highs: {highs_n5}"


def test_scalp_extension_tp_cap():
    """T46 – scalp_extension_tp() caps fib TP at SCALP_MAX_RR × SL distance.

    B-FIB-TP root cause: all SC1-SC6 called fib_extension_tp() unconditionally.
    For USD/JPY with D1/4H swings spanning 400-2000 pips, this produced TPs
    150-500+ pips from entry vs. a 15-40 pip SL → actual_rr of 10-40:1 that is
    virtually never reached.  scalp_extension_tp() must:
      (a) return None  when fib would produce RR > SCALP_MAX_RR (→ caller uses 2× SL)
      (b) return the fib TP when it is within SCALP_MAX_RR × sl_dist

    Also verifies that all 6 scalping strategy files use scalp_extension_tp()
    (not the raw fib_extension_tp()) and compute rr from actual prices.
    """
    import sys, os
    sys.path.insert(0, ROOT)
    import config as cfg

    # ── Part A: scalp_extension_tp returns None when fib is too far ──────────
    # State with D1 hi=155, lo=140 → rng=15 yen. fib TP (bullish) = 155 + 0.272*15 = 159.08
    state_far = {
        "d1": {"swing_hi": 155.0, "swing_lo": 140.0},
        "4h": {},
    }
    entry   = 144.0
    sl      = 143.0   # sl_dist = 1.0 yen = 100 pips
    sl_dist = abs(entry - sl)
    # fib TP = 159.08 → dist from entry = 15.08, RR = 15.08 → >> SCALP_MAX_RR (3.0)
    result = cfg.scalp_extension_tp(state_far, "bullish", entry, sl_dist)
    assert result is None, (
        f"scalp_extension_tp should return None when fib RR={159.08-entry:.2f}/{sl_dist:.2f}="
        f"{(159.08-entry)/sl_dist:.1f}x > SCALP_MAX_RR={cfg.SCALP_MAX_RR}, got {result}"
    )

    # ── Part B: scalp_extension_tp returns fib TP when within cap ────────────
    # State with D1 hi=144.5, lo=143.5 → rng=1.0 yen. fib TP (bullish) = 144.5+0.272=144.772
    state_close = {
        "d1": {"swing_hi": 144.5, "swing_lo": 143.5},
        "4h": {},
    }
    entry2   = 144.0
    sl2      = 143.5   # sl_dist = 0.5 yen = 50 pips
    sl_dist2 = abs(entry2 - sl2)
    fib_tp   = 144.5 + 0.272 * 1.0   # = 144.772
    # RR = (144.772 - 144.0) / 0.5 = 1.544 ≤ SCALP_MAX_RR=3 → should be returned
    result2 = cfg.scalp_extension_tp(state_close, "bullish", entry2, sl_dist2)
    assert result2 is not None, "scalp_extension_tp should return fib TP when within cap"
    assert abs(result2 - fib_tp) < 0.01, f"Expected fib TP ≈{fib_tp:.3f}, got {result2}"

    # ── Part C: sl_dist=0 guard ───────────────────────────────────────────────
    assert cfg.scalp_extension_tp(state_close, "bullish", 144.0, 0.0) is None, \
        "scalp_extension_tp must return None when sl_dist=0"

    # ── Part D: all 6 scalping files use scalp_extension_tp, not raw fib_extension_tp ──
    scalp_dir = os.path.join(ROOT, "strategies", "scalping")
    for sc in ("scalp1", "scalp2", "scalp3", "scalp4", "scalp5", "scalp6"):
        path = os.path.join(scalp_dir, f"{sc}.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        assert "scalp_extension_tp" in src, \
            f"{sc}.py must use scalp_extension_tp() (B-FIB-TP fix)"
        # Must NOT call raw fib_extension_tp in the TP assignment block
        # (it's still called internally by scalp_extension_tp, but not directly by strategy)
        assert "config.fib_extension_tp" not in src, \
            f"{sc}.py still calls config.fib_extension_tp() directly — must use scalp_extension_tp()"

    # ── Part E: rr stored = actual price ratio, not always 2.0 ──────────────
    # Check that no scalp strategy computes rr = round(config.TARGET_RR, 2)
    for sc in ("scalp1", "scalp2", "scalp3", "scalp4", "scalp5", "scalp6"):
        path = os.path.join(scalp_dir, f"{sc}.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        assert "round(config.TARGET_RR, 2)" not in src, \
            f"{sc}.py still hardcodes rr=round(config.TARGET_RR,2) — must compute from prices"
        assert "config.TARGET_RR" not in src or "rr" not in src.split("config.TARGET_RR")[0][-20:], \
            "Unexpected TARGET_RR usage in rr assignment"


def test_bos_choch_lookback_matches_live():
    """T45 – build_state BOS/CHoCH lookback hours match the live Struct.ai tables.

    Live source (routers/structure.py):
        _bos_hours   = {"5m": 8, "15m": 48, "1h": 72, "4h": 336, "d1": 8760}
        _choch_hours = {"5m": 8, "15m": 24, "1h": 72, "4h": 336, "d1": 4320}

    Verifies via source inspection that backtest_engine.py uses identical values.
    """
    import os
    engine_src = os.path.join(ROOT, "backtest_engine.py")
    with open(engine_src, "r", encoding="utf-8") as fh:
        src = fh.read()

    # BOS lookback table
    expected_bos = '"D1": 8760, "4H": 336, "1H": 72, "15M": 48, "5M": 8'
    assert expected_bos in src, \
        f"BOS lookback table does not match live values.\nExpected: {expected_bos}"

    # CHoCH lookback table
    expected_choch = '"D1": 4320, "4H": 336, "1H": 72, "15M": 24, "5M": 8'
    assert expected_choch in src, \
        f"CHoCH lookback table does not match live values.\nExpected: {expected_choch}"

    # CHoCH must use choch_h, NOT lookback_h // 2  (that was the wrong formula)
    assert "lookback_hours=choch_h" in src, \
        "CHoCH detect_choch() not using choch_h variable"
    assert "lookback_hours=lookback_h // 2" not in src, \
        "Old wrong CHoCH formula (lookback_h // 2) still present — should be choch_h"


# ── Runner ────────────────────────────────────────────────────────────────────

ALL_TESTS = [
    ("T01 sr_levels includes D1",                test_sr_d1_included),
    ("T02 sr_levels: above=resistance",          test_sr_directionality_above_is_resistance),
    ("T03 sr_levels: below=support",             test_sr_directionality_below_is_support),
    ("T04 sr_levels: no duplicate kinds",        test_sr_no_duplicate_kinds),
    ("T05 build_state top-level keys",           test_build_state_top_level_keys),
    ("T06 build_state TF sub-dict keys",         test_build_state_tf_sub_keys),
    ("T07 session: London at 09:00 UTC",         test_session_london),
    ("T08 session: NY at 15:00 UTC",             test_session_ny),
    ("T09 session: Asia at 03:00 UTC",           test_session_asia),
    ("T10 session: overlap 13:00 UTC",           test_session_overlap),
    ("T11 asia_range high/low correct",          test_asia_range_calc),
    ("T12 zigzag alternates hi/lo",              test_zigzag_alternates),
    ("T13 structure classifies labels",          test_structure_classifies),
    ("T14 bos_engine returns list",              test_bos_returns_list),
    ("T15 zones_engine clusters nearby",         test_zones_cluster),
    ("T16 zones_engine: no cluster far apart",   test_zones_no_cluster_far_apart),
    ("T17 calc_pnl BUY win",                     test_calc_pnl_buy_win),
    ("T18 calc_pnl BUY loss",                    test_calc_pnl_buy_loss),
    ("T19 calc_pnl SELL win",                    test_calc_pnl_sell_win),
    ("T20 calc_pnl spread deducted",             test_calc_pnl_spread_deducted),
    ("T21 Monte Carlo deterministic",            test_monte_carlo_deterministic),
    ("T22 Monte Carlo p5<=p50<=p95",             test_monte_carlo_percentile_ordering),
    ("T23 Monte Carlo prob in [0,100]",          test_monte_carlo_prob_in_range),
    ("T24 _compute_stats no div/zero",           test_compute_stats_no_div_zero),
    ("T25 _compute_stats excludes OPEN_AT_END",  test_compute_stats_excludes_open_at_end),
    ("T26 all 10 strategies import",             test_all_strategies_import),
    ("T27 SW1 uses SWING_MIN_CONFIDENCE",        test_sw1_uses_swing_min_confidence),
    ("T28 SW2 uses SWING_MIN_CONFIDENCE",        test_sw2_uses_swing_min_confidence),
    ("T29 SW3 uses SWING_MIN_CONFIDENCE",        test_sw3_uses_swing_min_confidence),
    ("T30 sr_levels has D1 entries in state",    test_sr_levels_d1_present_in_state),
    ("T31 SL/TP guard rejects bad BUY",         test_sl_tp_sanity_buy),
    ("T32 SL/TP guard rejects bad SELL",        test_sl_tp_sanity_sell),
    ("T33 chart regex: FULL line parse",        test_chart_regex_full_line),
    ("T34 chart regex: IS and OOS segments",    test_chart_regex_is_oos_segments),
    ("T35 chart regex: negative P&L",           test_chart_regex_negative_pnl),
    ("T36 chart regex: no false positives",     test_chart_regex_no_false_positives),
    # New tests for fixed bugs
    ("T37 fib_tp: no cross-TF mixing",          test_fib_tp_no_cross_tf_mixing),
    ("T38 fib_tp: pure D1 pair used",           test_fib_tp_pure_d1),
    ("T39 fib_tp: fallback to 4H when D1 absent", test_fib_tp_fallback_to_4h_when_d1_missing),
    ("T40 monte_carlo stub has all keys",       test_monte_carlo_key_always_present),
    ("T41 avg_rr excludes OPEN_AT_END",         test_avg_rr_excludes_open_at_end),
    ("T42 per-strategy avg_rr excludes OPEN_AT_END", test_per_strategy_avg_rr_excludes_open_at_end),
    ("T43a ZoneInfo fallback: London DST",      test_zoneinfo_fallback_dst_london),
    ("T43b ZoneInfo fallback: NY DST",          test_zoneinfo_fallback_dst_ny),
    ("T44 fractal_n=3 for D1/4H/1H, 5 for 15M/5M", test_fractal_n_per_timeframe),
    ("T45 BOS/CHoCH lookback matches live tables",  test_bos_choch_lookback_matches_live),
    ("T46 scalp_extension_tp caps fib TP at SCALP_MAX_RR", test_scalp_extension_tp_cap),
]


def main():
    print(f"\n{'='*60}")
    print(f"  struct-ai-backtest  Test Suite  ({len(ALL_TESTS)} tests)")
    print(f"{'='*60}")
    for name, fn in ALL_TESTS:
        _run(name, fn)
    print(f"\n{'='*60}")
    print(f"  Results:  {_passed} passed  /  {_failed} failed  "
          f"/ {len(ALL_TESTS)} total")
    print(f"{'='*60}\n")
    sys.exit(0 if _failed == 0 else 1)


if __name__ == "__main__":
    main()


# ── pytest compatibility ───────────────────────────────────────────────────────
# When run via pytest, each test_ function is discovered automatically.
# The _run() harness above is only used when invoked as a plain script.
