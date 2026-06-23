"""
run_backtest.py — Main entry point for struct-ai-backtest (v2).

Usage:
    python run_backtest.py [OPTIONS]

Examples:
    python run_backtest.py
        Runs all 10 strategies on USD/JPY with walk-forward + Monte Carlo

    python run_backtest.py --portfolio
        Runs all 10 strategies on all 5 pairs (USD/JPY EUR/USD GBP/USD AUD/USD USD/CHF)

    python run_backtest.py --symbol EUR/USD --strategies SC1 SC2 SW1
        Runs SC1, SC2, SW1 on EUR/USD

    python run_backtest.py --collect
        Collects market data first (auto source), then runs backtest

    python run_backtest.py --collect --source mt5
        Collect using MT5 (requires terminal running and logged in)

    python run_backtest.py --collect --source yfinance
        Collect using Yahoo Finance (~60 days of 5M)

    python run_backtest.py --collect --refresh --source mt5
        Fast top-up (new bars only) via MT5

    python run_backtest.py --strategies SW1 SW2 SW3 SW4 --symbol USD/JPY --lot 0.01
        Swing-only backtest with 0.01 lot

    python run_backtest.py --no-wf --no-mc
        Disable walk-forward and Monte Carlo (faster run)

Options:
    --symbol        Symbol to test (default: USD/JPY)
    --portfolio     Run all 5 pairs and aggregate results
    --strategies    Space-separated list of strategy codes SC1-SC6 SW1-SW4
    --lot           Lot size (default: 0.02 for scalping, 0.01 for swing-only)
    --debug         Enable per-bar debug output from strategy check()
    --collect       Re-run data collector before backtest
    --source        Data source for --collect: auto (default), mt5, or yfinance
    --refresh       With --collect: only download bars newer than what is in the DB
    --no-viewer     Skip opening results HTML viewer after run
    --no-wf         Disable walk-forward validation (faster)
    --no-mc         Disable Monte Carlo simulation (faster)
    --mc-iter       Number of Monte Carlo iterations (default: 1000)
    --wf-split      Walk-forward in-sample fraction 0-1 (default: 0.70)
    --help          Show this message
"""

import sys
import os
import argparse
import json
import datetime
import webbrowser
import subprocess

# Fix: force UTF-8 stdout so Unicode chars (→, ─, etc.) don't crash on Windows
# when Python is piped by the dashboard subprocess runner.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "engines"))

from backtest_engine import BacktestEngine, STRATEGY_MAP

PORTFOLIO_SYMBOLS = ["USD/JPY", "EUR/USD", "GBP/USD", "AUD/USD", "USD/CHF"]


def parse_args():
    p = argparse.ArgumentParser(
        description="struct-ai-backtest v2 — bar-by-bar replay for SC1-SC6, SW1-SW4",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--symbol",     default="USD/JPY",
                   help="Forex pair to backtest (default: USD/JPY)")
    p.add_argument("--portfolio",  action="store_true",
                   help=f"Run all 5 pairs: {', '.join(PORTFOLIO_SYMBOLS)}")
    p.add_argument("--strategies", nargs="+", default=list(STRATEGY_MAP.keys()),
                   metavar="CODE",
                   help="Strategy codes to run (default: all 10)")
    p.add_argument("--lot",        type=float, default=None,
                   help="Position size in lots")
    p.add_argument("--debug",      action="store_true",
                   help="Enable verbose debug output from strategies")
    p.add_argument("--collect",    action="store_true",
                   help="Re-run data collector before backtest")
    p.add_argument("--source",     choices=["auto", "mt5", "yfinance"], default="auto",
                   help="Data source for --collect: auto (default), mt5, or yfinance")
    p.add_argument("--refresh",    action="store_true",
                   help="With --collect: only download bars newer than what is in the DB")
    p.add_argument("--no-viewer",  action="store_true",
                   help="Do not open the HTML results viewer after run")
    p.add_argument("--no-wf",      action="store_true",
                   help="Disable walk-forward validation (faster)")
    p.add_argument("--no-mc",      action="store_true",
                   help="Disable Monte Carlo simulation (faster)")
    p.add_argument("--mc-iter",    type=int, default=1000,
                   help="Monte Carlo iterations (default: 1000)")
    p.add_argument("--wf-split",   type=float, default=0.70,
                   help="Walk-forward in-sample fraction (default: 0.70)")
    return p.parse_args()


def run_collector(source: str = "auto", refresh: bool = False):
    print("\n" + "=" * 64)
    print("  Running data collector...")
    print("=" * 64)
    collector = os.path.join(ROOT, "collector", "collect.py")
    cmd = [sys.executable, collector, "--source", source]
    if refresh:
        cmd.append("--refresh")
    # FIX: use subprocess.run() instead of os.system() — os.system() breaks
    # on Windows when the executable path is quoted because cmd.exe reads the
    # first quoted token as a window title, not the program to run.
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print("\n[ERROR] Data collection failed. Check above for details.")
        sys.exit(1)


def save_results(results: dict, label: str) -> str:
    os.makedirs(os.path.join(ROOT, "results"), exist_ok=True)
    ts_str = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    fname  = f"{label}_{ts_str}.json"
    fpath  = os.path.join(ROOT, "results", fname)

    def _clean(obj):
        if isinstance(obj, float):
            return round(obj, 6)
        if isinstance(obj, dict):
            return {k: _clean(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_clean(v) for v in obj]
        return obj

    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(_clean(results), f, indent=2)

    print(f"\n  Results saved: {fpath}")
    return fpath


def aggregate_portfolio(results_by_pair: dict, strategies: list) -> dict:
    """
    Merge per-pair backtest results into a single portfolio summary.
    Equity curves are offset-combined (each pair's cumPnL added step-wise
    would require re-sorting by timestamp; instead we report per-pair curves
    and compute aggregate totals from trade lists).
    """
    all_trades   = []
    total_pnl    = 0.0
    per_pair     = {}

    for sym, res in results_by_pair.items():
        all_trades.extend(res.get("trades", []))
        total_pnl += res.get("total_pnl", 0)
        per_pair[sym] = {
            "total_trades":   res.get("total_trades", 0),
            "wins":           res.get("wins", 0),
            "losses":         res.get("losses", 0),
            "win_rate":       res.get("win_rate", 0),
            "total_pnl":      res.get("total_pnl", 0),
            "max_drawdown":   res.get("max_drawdown", 0),
            "profit_factor":  res.get("profit_factor", 0),
            "equity_curve":   res.get("equity_curve", []),
            "by_strategy":    res.get("by_strategy", {}),   # Fix: was strategy_stats
            "walk_forward":   res.get("walk_forward"),
            "monte_carlo":    res.get("monte_carlo"),
        }

    # Combined strategy stats
    combined_strat = {}
    for code in strategies:
        all_st  = [t for t in all_trades if t["strategy"] == code]
        all_stc = [t for t in all_st if t.get("result") != "OPEN_AT_END"]
        all_sw  = [t for t in all_stc if t.get("result") == "TP"]  # Fix: was pnl>0
        sn      = len(all_st)
        snc     = len(all_stc)
        combined_strat[code] = {
            "trades":    sn,
            "wins":      len(all_sw),
            "losses":    snc - len(all_sw),
            "win_rate":  round(len(all_sw) / snc * 100, 1) if snc else 0,
            "total_pnl": round(sum(t.get("pnl", 0) or 0 for t in all_st), 4),
            # BUG FIX (B-AVG-RR-PORT-STRAT): use closed trades only (all_stc/snc)
            "avg_rr":    round(sum(t.get("rr",  0) or 0 for t in all_stc) / snc, 2) if snc else 0,
        }

    n      = len(all_trades)
    closed = [t for t in all_trades if t.get("result") != "OPEN_AT_END"]
    wins   = [t for t in closed if t.get("result") == "TP"]   # Fix: was pnl>0
    losses = [t for t in closed if t.get("result") == "SL"]   # Fix: was pnl<=0
    gross_win  = sum(t["pnl"] for t in wins  if t.get("pnl"))
    gross_loss = abs(sum(t["pnl"] for t in losses if t.get("pnl")))
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)

    return {
        "mode":          "portfolio",
        "symbols":       list(results_by_pair.keys()),
        "strategies":    strategies,
        "total_trades":  n,
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(len(wins) / len(closed) * 100, 2) if closed else 0,
        "total_pnl":     round(total_pnl, 4),
        # BUG FIX (B-AVG-RR-PORT): exclude OPEN_AT_END from portfolio avg_rr
        "avg_rr":        round(sum(t.get("rr", 0) or 0 for t in closed) / len(closed), 2) if closed else 0,
        "profit_factor": pf,
        "trades":        all_trades,
        "per_pair":      per_pair,
        "by_strategy":   combined_strat,   # Fix: was strategy_stats
    }


def generate_viewer(results: dict, json_path: str) -> str:
    viewer_template = os.path.join(ROOT, "viewer", "index.html")
    if not os.path.exists(viewer_template):
        print("  [WARN] Viewer template not found — skipping HTML generation")
        return ""

    with open(viewer_template, "r", encoding="utf-8") as f:
        template = f.read()

    results_json = json.dumps(results, indent=2)
    html = template.replace(
        "/* RESULTS_DATA_PLACEHOLDER */",
        f"const RESULTS_DATA = {results_json};"
    )

    out_path = json_path.replace(".json", ".html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    return out_path


def main():
    args = parse_args()

    invalid = [s for s in args.strategies if s not in STRATEGY_MAP]
    if invalid:
        print(f"[ERROR] Unknown strategies: {invalid}")
        print(f"Valid options: {list(STRATEGY_MAP.keys())}")
        sys.exit(1)

    import config as bt_config

    if args.lot is None:
        all_swing = all(s.startswith("SW") for s in args.strategies)
        args.lot = 0.01 if all_swing else 0.02

    walk_forward = not args.no_wf
    monte_carlo  = not args.no_mc

    if args.collect:
        symbols_to_collect = PORTFOLIO_SYMBOLS if args.portfolio else [args.symbol]
        for sym in symbols_to_collect:
            if sym not in bt_config.SYMBOL_CONFIG:
                print(f"[ERROR] Unknown symbol: {sym}")
                sys.exit(1)
        run_collector(source=args.source, refresh=args.refresh)

    # ── Portfolio mode ────────────────────────────────────────────────────────
    if args.portfolio:
        print(f"\n{'='*64}")
        print(f"  PORTFOLIO MODE — {len(PORTFOLIO_SYMBOLS)} pairs")
        print(f"  Pairs: {', '.join(PORTFOLIO_SYMBOLS)}")
        print(f"{'='*64}")

        results_by_pair = {}
        for sym in PORTFOLIO_SYMBOLS:
            if sym not in bt_config.SYMBOL_CONFIG:
                print(f"\n  [SKIP] {sym} not in SYMBOL_CONFIG")
                continue
            print(f"\n{'─'*64}")
            print(f"  Running: {sym}")
            print(f"{'─'*64}")
            try:
                engine = BacktestEngine(
                    symbol=sym,
                    strategies=args.strategies,
                    lot_size=args.lot,
                    debug=args.debug,
                    walk_forward=walk_forward,
                    monte_carlo=monte_carlo,
                    mc_iterations=args.mc_iter,
                    wf_split=args.wf_split,
                )
                results_by_pair[sym] = engine.run()
            except Exception as e:
                print(f"  [ERROR] {sym} failed: {e}")

        if not results_by_pair:
            print("[ERROR] All symbols failed. Check data availability.")
            sys.exit(1)

        combined = aggregate_portfolio(results_by_pair, args.strategies)
        # Embed full per-pair results for the viewer
        combined["_per_pair_full"] = results_by_pair

        print(f"\n{'='*64}")
        print(f"  PORTFOLIO SUMMARY")
        print(f"  Pairs run:  {len(results_by_pair)}")
        print(f"  Total trades: {combined['total_trades']}")
        print(f"  Win rate:     {combined['win_rate']:.1f}%")
        print(f"  Total PnL:    ${combined['total_pnl']:+.2f}")
        print(f"  Profit Factor:{combined['profit_factor']}")
        for sym, r in combined["per_pair"].items():
            print(f"    {sym}: {r['total_trades']} trades  "
                  f"WR={r['win_rate']:.1f}%  PnL=${r['total_pnl']:+.2f}")
        print(f"{'='*64}\n")

        label    = "PORTFOLIO_" + "_".join(s.replace("/","") for s in PORTFOLIO_SYMBOLS)
        json_path = save_results(combined, label)

    # ── Single symbol mode ────────────────────────────────────────────────────
    else:
        if args.symbol not in bt_config.SYMBOL_CONFIG:
            print(f"[ERROR] Unknown symbol: {args.symbol}")
            print(f"Valid options: {list(bt_config.SYMBOL_CONFIG.keys())}")
            sys.exit(1)

        engine = BacktestEngine(
            symbol=args.symbol,
            strategies=args.strategies,
            lot_size=args.lot,
            debug=args.debug,
            walk_forward=walk_forward,
            monte_carlo=monte_carlo,
            mc_iterations=args.mc_iter,
            wf_split=args.wf_split,
        )
        results   = engine.run()
        sym       = args.symbol.replace("/", "")
        codes     = "_".join(args.strategies)
        json_path = save_results(results, f"{sym}_{codes}")

    # ── Viewer ────────────────────────────────────────────────────────────────
    if not args.no_viewer:
        data_to_render = combined if args.portfolio else results
        viewer_html = generate_viewer(data_to_render, json_path)
        if viewer_html:
            print(f"  Viewer: {viewer_html}")
            try:
                webbrowser.open(f"file:///{viewer_html.replace(os.sep, '/')}")
            except Exception:
                print("  (Could not auto-open browser — open the HTML file manually)")

    return combined if args.portfolio else results


if __name__ == "__main__":
    main()
