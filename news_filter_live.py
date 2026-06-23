"""
News Filter Stub for backtesting.
In live trading this calls a news API. In backtest mode we always return
not-blocked so strategies run on all bars without news interference.
Set BACKTEST_BLOCK_NEWS = True to simulate news blocks (requires event list).
"""

BACKTEST_BLOCK_NEWS = False


def is_symbol_blocked(symbol: str, reference_ts: float = None) -> tuple[bool, str]:
    """
    Backtest stub — always returns (False, '') so no bar is news-blocked.
    In a real backtest you could inject a news event list here.
    """
    if BACKTEST_BLOCK_NEWS:
        # Placeholder: load your news event CSV and check reference_ts
        return False, ""
    return False, ""
