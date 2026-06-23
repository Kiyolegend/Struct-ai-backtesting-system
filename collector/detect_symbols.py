"""
MT5 Broker Symbol Auto-Detector.

Connects to your running MT5 terminal, fetches every symbol the broker
offers, and finds the best match for each of the 5 pairs we trade.

Matching priority (for each base name e.g. "EURUSD"):
  1. Exact match            EURUSD
  2. Common suffix variants EURUSDm  EURUSD+  EURUSD.s  EURUSD.r
                            EURUSD#  EURUSDpro  EURUSDstp  EURUSDmicro
  3. Shortest name that STARTS WITH the base
  4. Shortest name that CONTAINS the base (last resort)

Usage:
  python collector\\detect_symbols.py          -- print detected map, exit
  python collector\\detect_symbols.py --apply  -- print map + write to collect.py
  start.bat --detect-symbols                  -- same as above via batch
"""

import sys
import os

TARGET_PAIRS = {
    "USD/JPY": "USDJPY",
    "EUR/USD": "EURUSD",
    "GBP/USD": "GBPUSD",
    "AUD/USD": "AUDUSD",
    "USD/CHF": "USDCHF",
}

COMMON_SUFFIXES = [
    "",          # exact first
    "m",
    "+",
    ".s",
    ".r",
    ".i",
    ".pro",
    ".ecn",
    ".stp",
    "#",
    "pro",
    "ecn",
    "stp",
    "micro",
    "_",
    ".",
]


def _find_best_symbol(all_names: list, base: str) -> tuple:
    """
    Return (broker_symbol, match_method) for the best match, or (None, None).
    all_names: list of symbol name strings from mt5.symbols_get()
    base:      uppercase base name e.g. "USDJPY"
    """
    upper_map = {s.upper(): s for s in all_names}

    # 1 + 2 — exact and suffix variants
    for suffix in COMMON_SUFFIXES:
        candidate = (base + suffix).upper()
        if candidate in upper_map:
            method = "exact" if suffix == "" else f"suffix '{suffix}'"
            return upper_map[candidate], method

    # 3 — starts with base (pick shortest)
    starters = [s for s in all_names if s.upper().startswith(base.upper())]
    if starters:
        best = min(starters, key=len)
        return best, "starts-with match"

    # 4 — contains base (pick shortest)
    contains = [s for s in all_names if base.upper() in s.upper()]
    if contains:
        best = min(contains, key=len)
        return best, "contains match"

    return None, None


def detect_symbols(mt5) -> dict:
    """
    Connect to MT5, scan all broker symbols, return best match map.
    Returns {our_label: broker_symbol_name} — value is None if not found.
    Prints a clear report as it goes.
    """
    print()
    print("=" * 64)
    print("  MT5 Broker Symbol Auto-Detector")
    print("=" * 64)

    if not mt5.initialize():
        err = mt5.last_error()
        print(f"\n  [ERROR] Cannot connect to MT5: {err}")
        print("  Make sure MetaTrader 5 is open and logged in.")
        print("=" * 64)
        return {}

    info = mt5.terminal_info()
    if info:
        print(f"  Connected  build={info.build}  company={getattr(info, 'company', 'n/a')}")

    all_syms = mt5.symbols_get()
    # NOTE: do NOT call mt5.shutdown() here — the caller (collect.py) owns the
    # MT5 connection lifecycle. Shutting down here disconnects MT5 before
    # collect_mt5() pulls any bar data, resulting in 0 rows stored.  (Fix B1)

    if not all_syms:
        print("  [ERROR] No symbols returned from MT5.")
        print("=" * 64)
        return {}

    all_names = [s.name for s in all_syms]
    print(f"  Broker symbol count: {len(all_names)}")
    print()

    result = {}
    for our_label, base in TARGET_PAIRS.items():
        broker_sym, method = _find_best_symbol(all_names, base)
        result[our_label] = broker_sym

        if broker_sym:
            print(f"  {our_label:<10}  {base:<8}  ->  {broker_sym:<16}  [{method}]")
        else:
            print(f"  {our_label:<10}  {base:<8}  ->  *** NOT FOUND ***")
            print(f"             Search your broker's symbols for anything containing '{base}'")
            print(f"             Then edit MT5_SYMBOL_MAP in collector/collect.py manually.")

    not_found = [k for k, v in result.items() if v is None]
    found     = [k for k, v in result.items() if v is not None]

    print()
    print(f"  Found: {len(found)}/5   Not found: {len(not_found)}/5")
    print("=" * 64)

    return result


def print_symbol_list(mt5, filter_base: str = None):
    """
    Utility: print all symbols available from broker.
    Optionally filter to only those containing filter_base.
    """
    if not mt5.initialize():
        print(f"  [ERROR] Cannot connect to MT5: {mt5.last_error()}")
        return

    all_syms = mt5.symbols_get()
    mt5.shutdown()

    if not all_syms:
        print("  [ERROR] No symbols returned.")
        return

    names = sorted(s.name for s in all_syms)
    if filter_base:
        names = [n for n in names if filter_base.upper() in n.upper()]
        print(f"\n  Broker symbols containing '{filter_base}' ({len(names)} found):")
    else:
        print(f"\n  All broker symbols ({len(names)} total):")

    for n in names:
        print(f"    {n}")


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    list_all = "--list" in sys.argv
    filter_arg = None
    for i, a in enumerate(sys.argv):
        if a == "--filter" and i + 1 < len(sys.argv):
            filter_arg = sys.argv[i + 1]

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("\n[ERROR] MetaTrader5 package not installed.")
        print("  Run install.bat to install all packages.")
        sys.exit(1)

    if list_all or filter_arg:
        print_symbol_list(mt5, filter_base=filter_arg)
        sys.exit(0)

    detected = detect_symbols(mt5)

    if not detected:
        sys.exit(1)

    not_found = [k for k, v in detected.items() if v is None]
    if not_found:
        print(f"\n  WARNING: {len(not_found)} pair(s) not matched.")
        print("  You can search your broker's full symbol list with:")
        print("    python collector\\detect_symbols.py --list")
        print("    python collector\\detect_symbols.py --filter USD")

    print()
    print("  These symbols are used automatically every time you run:")
    print("    start.bat --collect --source mt5")
    print()
    print("  No manual editing of collect.py needed.")
    if not_found:
        print()
        print("  For any NOT FOUND pair, search your broker's full list:")
        print("    start.bat --collect --list-symbols --filter USDJPY")
