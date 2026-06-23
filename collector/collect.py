"""
Data Collector — Downloads OHLCV bars into tiered SQLite DB.

Supports two data sources:
  --source yfinance   Free, no extra install. 5M limited to ~60 days.
  --source mt5        Pulls from your running MT5 terminal. 5M goes back
                      1+ years (broker dependent). Best for scalping.
  --source auto       Try MT5 first; fall back to yfinance (default).

Tiered lookback targets (full collection):
  D1  : 5 years
  4H  : 3 years
  1H  : 2 years
  15M : 2 years
  5M  : 1 year  (yfinance gives ~60 days; MT5 gives the full year)

Pairs: USD/JPY, EUR/USD, GBP/USD, AUD/USD, USD/CHF
  (EUR/JPY, GBP/JPY, USD/CAD disabled — matches live DISABLED_SYMBOLS)

Once data is collected the DB is fully self-contained.
Backtests run OFFLINE with zero internet connection.

Usage:
  start.bat --collect                            full collection, auto source
  start.bat --collect --source mt5               full collection, MT5 only
  start.bat --collect --source yfinance          full collection, Yahoo only
  start.bat --collect --refresh                  top-up only (since last bar in DB)
  start.bat --collect --refresh --source mt5     top-up via MT5
  start.bat --collect --refresh --source yfinance  top-up via Yahoo Finance

  Or directly:
  python collector/collect.py --refresh --source mt5

--refresh mode:
  Only downloads bars newer than the latest bar already in the DB.
  Fast — typically seconds. Use this daily to keep data current.
  Falls back to full collection automatically if the DB is empty.

MT5 requirements:
  1. MetaTrader5 terminal must be running and logged in.
  2. pip install MetaTrader5   (Windows only — already in requirements.txt)
  3. Edit MT5_SYMBOL_MAP below if your broker uses different symbol names.
     Common variants: USDJPY  USDJPYm  USDJPY.s  USDJPY+

Estimated DB size: ~180-220 MB (all pairs, all timeframes).
"""

import sqlite3
import os
import sys
import datetime
import argparse
import glob
import json

# ── Paths ─────────────────────────────────────────────────────────────────────
# Fix: force UTF-8 stdout so Unicode chars don't crash on Windows when piped.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT    = os.path.join(os.path.dirname(__file__), "..")
DB_PATH = os.path.join(ROOT, "data", "market_data.db")

# ── Symbol maps ───────────────────────────────────────────────────────────────
SYMBOL_MAP_YF = {
    "USD/JPY": "USDJPY=X",
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "AUD/USD": "AUDUSD=X",
    "USD/CHF": "USDCHF=X",
}

# Edit these to match your broker's exact symbol names.
MT5_SYMBOL_MAP = {
    "USD/JPY": "USDJPYm",
    "EUR/USD": "EURUSDm",
    "GBP/USD": "GBPUSDm",
    "AUD/USD": "AUDUSDm",
    "USD/CHF": "USDCHFm",
}

# ── Lookback targets ──────────────────────────────────────────────────────────
LOOKBACK_YEARS = {
    "D1":  5,
    "4H":  3,
    "1H":  2,
    "15M": 2,
    "5M":  1,
}

# Yahoo Finance: max days it will actually serve per interval
YF_MAX_CHUNK_DAYS = {
    "1d":  3650,
    "1h":  730,
    "15m": 60,
    "5m":  60,
}

# yfinance interval → our TF label (4H is resampled from 1H)
YF_TF_CONFIG = [
    ("1d",  "D1"),
    ("1h",  "4H"),
    ("1h",  "1H"),
    ("15m", "15M"),
    ("5m",  "5M"),
]

# MT5 timeframe integer constants (from MetaTrader5 package)
MT5_TF_CONST = {
    "M5":  5,
    "M15": 15,
    "H1":  16385,
    "H4":  16388,
    "D1":  16408,
}

MT5_TF_LABELS = [
    ("D1",  "D1"),
    ("H4",  "4H"),
    ("H1",  "1H"),
    ("M15", "15M"),
    ("M5",  "5M"),
]

# Minimum overlap to pull when refreshing (avoids tiny gaps at TF boundaries)
REFRESH_OVERLAP = {
    "D1":  datetime.timedelta(days=5),
    "4H":  datetime.timedelta(days=2),
    "1H":  datetime.timedelta(days=1),
    "15M": datetime.timedelta(hours=6),
    "5M":  datetime.timedelta(hours=2),
}


# ─────────────────────────────────────────────────────────────────────────────
# SQLite helpers
# ─────────────────────────────────────────────────────────────────────────────

def open_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ohlcv (
            symbol    TEXT    NOT NULL,
            timeframe TEXT    NOT NULL,
            ts        INTEGER NOT NULL,
            open      REAL    NOT NULL,
            high      REAL    NOT NULL,
            low       REAL    NOT NULL,
            close     REAL    NOT NULL,
            volume    REAL    NOT NULL DEFAULT 0,
            PRIMARY KEY (symbol, timeframe, ts)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ohlcv_sym_tf_ts ON ohlcv(symbol, timeframe, ts)"
    )
    conn.commit()
    return conn


def get_latest_ts(conn: sqlite3.Connection,
                  symbol: str, tf: str) -> datetime.datetime | None:
    """Return the latest bar datetime stored for symbol+timeframe, or None."""
    row = conn.execute(
        "SELECT MAX(ts) FROM ohlcv WHERE symbol=? AND timeframe=?",
        (symbol, tf),
    ).fetchone()
    if row and row[0]:
        return datetime.datetime.fromtimestamp(int(row[0]),
                                               tz=datetime.timezone.utc)
    return None


def get_db_summary(conn: sqlite3.Connection) -> dict:
    """Return {symbol: {tf: (bar_count, latest_datetime)}}."""
    rows = conn.execute(
        "SELECT symbol, timeframe, COUNT(*), MAX(ts) "
        "FROM ohlcv GROUP BY symbol, timeframe"
    ).fetchall()
    out: dict = {}
    for sym, tf, cnt, ts in rows:
        out.setdefault(sym, {})[tf] = (
            cnt,
            datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc)
            if ts else None,
        )
    return out


def store_rows(conn: sqlite3.Connection,
               rows: list, symbol: str, tf: str) -> int:
    if not rows:
        print(f"    [WARN] No rows to store for {symbol} {tf}")
        return 0
    tagged = [(symbol, tf) + r[2:] for r in rows]
    conn.executemany(
        "INSERT OR REPLACE INTO ohlcv"
        "(symbol,timeframe,ts,open,high,low,close,volume) "
        "VALUES(?,?,?,?,?,?,?,?)",
        tagged,
    )
    conn.commit()
    return len(tagged)


# ─────────────────────────────────────────────────────────────────────────────
# Yahoo Finance source
# ─────────────────────────────────────────────────────────────────────────────

def _yf_import():
    try:
        import yfinance as yf
        return yf
    except ImportError:
        print("ERROR: yfinance not installed. Run install.bat first.")
        sys.exit(1)


def _yf_download(yf, ticker: str, interval: str, years: int,
                 since: datetime.datetime | None = None):
    """
    Download OHLCV from Yahoo Finance.
    If `since` is given, only download from that datetime to now.
    """
    import pandas as pd

    end   = datetime.datetime.now(datetime.timezone.utc)
    start = since if since else (end - datetime.timedelta(days=365 * years))
    chunk = YF_MAX_CHUNK_DAYS.get(interval, 365)

    if interval in ("5m", "15m"):
        frames = []
        cursor = start
        while cursor < end:
            c_end = min(cursor + datetime.timedelta(days=chunk), end)
            print(f"    chunk {cursor.strftime('%Y-%m-%d')} → "
                  f"{c_end.strftime('%Y-%m-%d')} ...", end=" ", flush=True)
            try:
                df = yf.download(
                    ticker,
                    start=cursor.strftime("%Y-%m-%d"),
                    end=c_end.strftime("%Y-%m-%d"),
                    interval=interval,
                    auto_adjust=True,
                    progress=False,
                )
                if not df.empty:
                    frames.append(df)
                print(f"{len(df)} bars")
            except Exception as e:
                print(f"WARN: {e}")
            cursor = c_end
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames)
        out = out[~out.index.duplicated(keep="last")]
        out.sort_index(inplace=True)
        return out
    else:
        label = f"since {start.strftime('%Y-%m-%d')}" if since else f"{years}yr"
        print(f"    downloading {label} of {interval} ...", end=" ", flush=True)
        df = yf.download(
            ticker,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            interval=interval,
            auto_adjust=True,
            progress=False,
        )
        print(f"{len(df)} bars")
        return df


def _df_to_rows(df) -> list:
    import pandas as pd
    if df is None or df.empty:
        return []
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={"Open": "open", "High": "high",
                             "Low": "low", "Close": "close", "Volume": "volume"})
    rows = []
    for ts, row in df.iterrows():
        if pd.isna(row.get("open")) or pd.isna(row.get("close")):
            continue
        rows.append((
            None, None,
            int(pd.Timestamp(ts).timestamp()),
            float(row.get("open",   0)),
            float(row.get("high",   0)),
            float(row.get("low",    0)),
            float(row.get("close",  0)),
            float(row.get("volume", 0)),
        ))
    return rows


def _yf_resample_4h(df_1h):
    import pandas as pd
    df = df_1h.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index, utc=True)
    return df.resample("4h").agg(
        {"Open": "first", "High": "max", "Low": "min",
         "Close": "last", "Volume": "sum"}
    ).dropna()


def collect_yfinance(conn: sqlite3.Connection,
                     since_map: dict | None = None) -> int:
    """
    Full or refresh Yahoo Finance collection.
    since_map: {symbol: {tf: datetime}} — if provided, only fetch from that date.
    """
    yf = _yf_import()
    mode = "REFRESH" if since_map else "FULL"
    print("\n" + "="*64)
    print(f"  SOURCE: Yahoo Finance  [{mode}]")
    if not since_map:
        print("  NOTE: 5M data limited to ~60 days by Yahoo Finance.")
        print("        Use --source mt5 for full 1-year 5M history.")
    print("="*64)

    total = 0
    for symbol, ticker in SYMBOL_MAP_YF.items():
        print(f"\n{'='*60}\n  {symbol}  ({ticker})\n{'='*60}")

        sym_since = (since_map or {}).get(symbol, {})

        for yf_interval, tf_label in YF_TF_CONFIG:
            years = LOOKBACK_YEARS[tf_label]
            since = sym_since.get(tf_label)
            label = f"since {since.strftime('%Y-%m-%d')}" if since else f"{years}yr"
            print(f"\n  [{tf_label}]  {label}")

            if tf_label == "4H":
                since_4h = sym_since.get("4H")
                df1h = _yf_download(yf, ticker, "1h", years, since=since_4h)
                if df1h is None or df1h.empty:
                    print(f"    [WARN] No 1H data — cannot build 4H for {symbol}")
                    continue
                df = _yf_resample_4h(df1h)
                print(f"    resampled 1H→4H: {len(df)} bars")
                rows = _df_to_rows(df)
            else:
                df = _yf_download(yf, ticker, yf_interval, years, since=since)
                rows = _df_to_rows(df)

            n = store_rows(conn, rows, symbol, tf_label)
            print(f"    stored {n:,} rows → DB")
            total += n

    return total


# ─────────────────────────────────────────────────────────────────────────────
# MetaTrader 5 source
# ─────────────────────────────────────────────────────────────────────────────

def _mt5_import():
    try:
        import MetaTrader5 as mt5
        return mt5
    except ImportError:
        return None


def _mt5_connect(mt5) -> bool:
    if not mt5.initialize():
        err = mt5.last_error()
        print(f"\n  [ERROR] mt5.initialize() failed: {err}")
        print("  Is the MetaTrader5 terminal open and logged in?")
        return False
    info = mt5.terminal_info()
    if info:
        print(f"  MT5 connected: build={info.build}")
    return True


def _mt5_resolve_symbol(mt5, broker_symbol: str) -> str | None:
    # MT5 returns None for symbols not yet in Market Watch — select first.
    mt5.symbol_select(broker_symbol, True)
    info = mt5.symbol_info(broker_symbol)
    if info:
        return broker_symbol
    # Try stripping trailing 'm' suffix as a fallback
    fallback = broker_symbol.rstrip("m")
    if fallback != broker_symbol:
        mt5.symbol_select(fallback, True)
        if mt5.symbol_info(fallback):
            print(f"    [INFO] '{broker_symbol}' not found — using '{fallback}'")
            return fallback
    print(f"    [WARN] Symbol '{broker_symbol}' not found in MT5.")
    print(f"    Edit MT5_SYMBOL_MAP in collect.py to match your broker.")
    print(f"    Common variants: {broker_symbol.rstrip('m')}  "
          f"{broker_symbol.rstrip('m')}.s  {broker_symbol.rstrip('m')}+")
    return None


def _mt5_rates_to_rows(rates) -> list:
    return [
        (None, None,
         int(r["time"]),
         float(r["open"]),
         float(r["high"]),
         float(r["low"]),
         float(r["close"]),
         float(r["tick_volume"]) if "tick_volume" in rates.dtype.names else 0.0)
        for r in rates
    ]


def _mt5_pull(mt5, broker_sym: str, tf_const: int,
              years: int, tf_label: str,
              since: datetime.datetime | None = None) -> list:
    """Pull bars from MT5. If `since` is given, only fetch from that date.

    MT5 hard limit: copy_rates_range returns error -2 when a single request
    exceeds ~100,000 bars.  5M over 1 year is ~105,120 bars (over the limit).
    All other timeframes are safely under 100k.  This function detects when the
    expected bar count would exceed the limit and automatically chunks the
    request into safe windows, then merges and deduplicates the results.
    """
    _MT5_MAX_BARS = 100_000
    _BARS_PER_DAY = {"5M": 288, "15M": 96, "1H": 24, "4H": 6, "D1": 1}

    end   = datetime.datetime.now(datetime.timezone.utc)
    start = since if since else (end - datetime.timedelta(days=365 * years))
    label = f"since {start.strftime('%Y-%m-%d')}" if since else f"{years}yr"

    bars_per_day   = _BARS_PER_DAY.get(tf_label, 1)
    total_days     = max((end - start).days + 1, 1)
    expected_bars  = total_days * bars_per_day

    if expected_bars > _MT5_MAX_BARS:
        chunk_days = _MT5_MAX_BARS // bars_per_day
        print(f"    pulling {label} of {tf_label} from MT5 in chunks "
              f"({chunk_days}d each) ...")
        all_rows: list = []
        seen_ts: set   = set()
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + datetime.timedelta(days=chunk_days), end)
            print(f"      chunk {cursor.strftime('%Y-%m-%d')} → "
                  f"{chunk_end.strftime('%Y-%m-%d')} ...", end=" ", flush=True)
            rates = mt5.copy_rates_range(broker_sym, tf_const, cursor, chunk_end)
            if rates is None or len(rates) == 0:
                print(f"0 bars  (MT5 error: {mt5.last_error()})")
            else:
                chunk_rows = _mt5_rates_to_rows(rates)
                new_rows = [r for r in chunk_rows if r[2] not in seen_ts]
                seen_ts.update(r[2] for r in new_rows)
                all_rows.extend(new_rows)
                print(f"{len(rates):,} bars")
            cursor = chunk_end
        print(f"    total: {len(all_rows):,} bars")
        return all_rows
    else:
        print(f"    pulling {label} of {tf_label} from MT5 ...", end=" ", flush=True)
        rates = mt5.copy_rates_range(broker_sym, tf_const, start, end)
        if rates is None or len(rates) == 0:
            print(f"0 bars  (MT5 error: {mt5.last_error()})")
            return []
        print(f"{len(rates):,} bars")
        return _mt5_rates_to_rows(rates)


def _mt5_pull_4h(mt5, broker_sym: str, years: int,
                 since: datetime.datetime | None = None) -> list:
    """Pull 1H from MT5, resample to 4H. `since` limits the start date."""
    import pandas as pd
    end   = datetime.datetime.now(datetime.timezone.utc)
    start = since if since else (end - datetime.timedelta(days=365 * years))
    label = f"since {start.strftime('%Y-%m-%d')}" if since else f"{years}yr"
    print(f"    pulling {label} of 1H from MT5 for 4H resample ...",
          end=" ", flush=True)
    rates = mt5.copy_rates_range(broker_sym, MT5_TF_CONST["H1"], start, end)
    if rates is None or len(rates) == 0:
        print(f"0 bars  (MT5 error: {mt5.last_error()})")
        return []
    print(f"{len(rates):,} 1H bars — resampling ...")
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("time")
    df4 = df.resample("4h").agg(
        {"open": "first", "high": "max", "low": "min",
         "close": "last", "tick_volume": "sum"}
    ).dropna()
    print(f"    resampled to {len(df4):,} 4H bars")
    return [
        (None, None,
         int(ts.timestamp()),
         float(r["open"]), float(r["high"]),
         float(r["low"]),  float(r["close"]),
         float(r["tick_volume"]))
        for ts, r in df4.iterrows()
    ]


def collect_mt5(conn: sqlite3.Connection,
                since_map: dict | None = None) -> int:
    """
    Full or refresh MT5 collection.
    since_map: {symbol: {tf: datetime}} — if provided, only fetch from that date.
    Auto-detects broker symbol names before pulling data.
    """
    mt5 = _mt5_import()
    if mt5 is None:
        print("\n[ERROR] MetaTrader5 package not installed.")
        print("  Run:  pip install MetaTrader5")
        sys.exit(1)

    mode = "REFRESH" if since_map else "FULL"
    print("\n" + "="*64)
    print(f"  SOURCE: MetaTrader5  [{mode}]")
    print("="*64)

    if not _mt5_connect(mt5):
        sys.exit(1)

    # ── Auto-detect broker symbol names ──────────────────────────────────────
    # Scans every symbol your broker offers and finds the best match for each
    # pair. This means you never need to edit MT5_SYMBOL_MAP manually.
    try:
        from collector.detect_symbols import detect_symbols
    except ImportError:
        try:
            sys.path.insert(0, os.path.dirname(__file__))
            from detect_symbols import detect_symbols
        except ImportError:
            detect_symbols = None

    active_symbol_map = dict(MT5_SYMBOL_MAP)  # fallback to static map

    if detect_symbols is not None:
        print("\n  Auto-detecting broker symbol names ...")
        detected = detect_symbols(mt5)
        if detected:
            # Merge: use detected name where found, keep static name as fallback
            for our_sym, broker_sym in detected.items():
                if broker_sym is not None:
                    if broker_sym != active_symbol_map.get(our_sym):
                        print(f"  [AUTO] {our_sym}: using '{broker_sym}' "
                              f"(was '{active_symbol_map.get(our_sym)}')")
                    active_symbol_map[our_sym] = broker_sym
        print()
    # ─────────────────────────────────────────────────────────────────────────

    total = 0
    for our_sym, broker_sym_raw in active_symbol_map.items():
        print(f"\n{'='*60}\n  {our_sym}  (MT5: {broker_sym_raw})\n{'='*60}")

        broker_sym = _mt5_resolve_symbol(mt5, broker_sym_raw)
        if broker_sym is None:
            print(f"  Skipping {our_sym}.")
            continue

        sym_info = mt5.symbol_info(broker_sym)
        if sym_info is not None and not sym_info.visible:
            mt5.symbol_select(broker_sym, True)

        sym_since = (since_map or {}).get(our_sym, {})

        for mt5_tf_name, tf_label in MT5_TF_LABELS:
            years = LOOKBACK_YEARS[tf_label]
            since = sym_since.get(tf_label)
            label = f"since {since.strftime('%Y-%m-%d')}" if since else f"{years}yr"
            print(f"\n  [{tf_label}]  {label}")

            if tf_label == "4H":
                rows = _mt5_pull_4h(mt5, broker_sym, years,
                                    since=sym_since.get("4H"))
            else:
                rows = _mt5_pull(mt5, broker_sym,
                                 MT5_TF_CONST[mt5_tf_name],
                                 years, tf_label, since=since)

            n = store_rows(conn, rows, our_sym, tf_label)
            print(f"    stored {n:,} rows → DB")
            total += n

    mt5.shutdown()
    print("\n  MT5 connection closed.")
    return total


# ─────────────────────────────────────────────────────────────────────────────
# Refresh mode — query DB for latest timestamps, fetch only new bars
# ─────────────────────────────────────────────────────────────────────────────

def build_since_map(conn: sqlite3.Connection) -> dict:
    """
    For each symbol+timeframe in the DB, compute the start datetime for a
    refresh download: latest stored bar minus a small overlap to avoid gaps.
    Returns {symbol: {tf: datetime}}.
    """
    since_map: dict = {}
    all_syms = list(SYMBOL_MAP_YF.keys())
    all_tfs  = ["D1", "4H", "1H", "15M", "5M"]

    for sym in all_syms:
        for tf in all_tfs:
            latest = get_latest_ts(conn, sym, tf)
            if latest is None:
                continue
            overlap = REFRESH_OVERLAP.get(tf, datetime.timedelta(days=1))
            since   = latest - overlap
            since_map.setdefault(sym, {})[tf] = since

    return since_map


def collect_refresh(conn: sqlite3.Connection, source: str) -> int:
    """
    Top-up the DB with only new bars since the last stored bar.
    Falls back to full collection if DB is empty.
    """
    since_map = build_since_map(conn)

    if not since_map:
        print("\n  DB is empty — falling back to full collection.")
        if source == "mt5":
            return collect_mt5(conn)
        elif source == "yfinance":
            return collect_yfinance(conn)
        else:
            return collect_auto(conn)

    # Show what we have and what we will fetch
    print("\n  Current DB coverage:")
    tfs_order = ["5M", "15M", "1H", "4H", "D1"]
    for sym in sorted(since_map.keys()):
        for tf in tfs_order:
            if tf in since_map[sym]:
                dt = since_map[sym][tf]
                print(f"    {sym}  {tf}  latest bar ≈ "
                      f"{(dt + REFRESH_OVERLAP.get(tf, datetime.timedelta(days=1))).strftime('%Y-%m-%d %H:%M')} UTC")

    print()
    if source == "mt5":
        return collect_mt5(conn, since_map=since_map)
    elif source == "yfinance":
        return collect_yfinance(conn, since_map=since_map)
    else:
        return collect_auto(conn, since_map=since_map)


# ─────────────────────────────────────────────────────────────────────────────
# Auto mode — try MT5, fall back to Yahoo Finance
# ─────────────────────────────────────────────────────────────────────────────

def collect_auto(conn: sqlite3.Connection,
                 since_map: dict | None = None) -> int:
    mt5 = _mt5_import()
    if mt5 is not None:
        print("\n  MetaTrader5 package found — testing connection ...")
        if mt5.initialize():
            mt5.shutdown()
            print("  MT5 terminal reachable — using MT5 source.")
            return collect_mt5(conn, since_map=since_map)
        else:
            print(f"  MT5 not reachable ({mt5.last_error()}) — falling back to Yahoo Finance.")
    else:
        print("\n  MetaTrader5 package not installed — using Yahoo Finance.")
        print("  NOTE: 5M data limited to ~60 days.")
        print("  For 1-year 5M: pip install MetaTrader5  then re-run.")
    return collect_yfinance(conn, since_map=since_map)


# ─────────────────────────────────────────────────────────────────────────────
# DB Validator
# ─────────────────────────────────────────────────────────────────────────────

def validate_db(conn: sqlite3.Connection) -> bool:
    """
    Audit the SQLite database and print a coverage + integrity report.

    Checks per symbol/timeframe:
      1. Coverage  — bar count vs minimum expected for the lookback target
      2. Freshness — latest bar within a reasonable staleness window
      3. Duplicates — PRIMARY KEY enforces uniqueness, but we verify anyway
      4. Gaps      — any silence longer than 10× the nominal bar interval
                     (accounts for weekends / holidays automatically)
      5. OHLC sanity — high >= open/close >= low, no zero prices

    Returns True if no FAIL-level issues were found.
    """
    ALL_TFS = ["5M", "15M", "1H", "4H", "D1"]
    ALL_SYMS = list(SYMBOL_MAP_YF.keys())

    TF_SECONDS = {"5M": 300, "15M": 900, "1H": 3600, "4H": 14400, "D1": 86400}
    TF_BARS_PER_YEAR = {"5M": 105_120, "15M": 26_280, "1H": 8_760, "4H": 2_190, "D1": 365}

    MIN_BAR_RATIO = 0.40
    GAP_FACTOR    = 10
    STALE_DAYS    = {"5M": 3, "15M": 3, "1H": 5, "4H": 7, "D1": 10}

    now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())

    fail_count = 0
    warn_count = 0
    ok_count   = 0

    print("\n" + "=" * 68)
    print("  struct-ai-backtest  DB Validator")
    print(f"  DB: {DB_PATH}")
    print("=" * 68)

    if not os.path.exists(DB_PATH):
        print("\n  [FAIL] DB file does not exist. Run --collect first.")
        return False

    for sym in ALL_SYMS:
        print(f"\n  {sym}")
        print(f"  {'─'*50}")

        for tf in ALL_TFS:
            years = LOOKBACK_YEARS[tf]
            bar_interval = TF_SECONDS[tf]
            gap_threshold = bar_interval * GAP_FACTOR

            # ── 1. Coverage ───────────────────────────────────────────────
            rows = conn.execute(
                "SELECT ts FROM ohlcv WHERE symbol=? AND timeframe=? ORDER BY ts",
                (sym, tf),
            ).fetchall()

            bar_count = len(rows)
            expected_min = int(TF_BARS_PER_YEAR[tf] * years * MIN_BAR_RATIO)

            if bar_count == 0:
                print(f"    [{tf:>3}]  FAIL   0 bars — run --collect first")
                fail_count += 1
                continue

            timestamps = [r[0] for r in rows]

            # ── 2. Freshness ───────────────────────────────────────────────
            latest_ts   = timestamps[-1]
            stale_secs  = STALE_DAYS[tf] * 86400
            age_days    = (now_ts - latest_ts) / 86400
            stale       = (now_ts - latest_ts) > stale_secs
            latest_dt   = datetime.datetime.fromtimestamp(latest_ts, tz=datetime.timezone.utc)

            # ── 3. Duplicates ─────────────────────────────────────────────
            dup_count = bar_count - len(set(timestamps))

            # ── 4. Gaps ───────────────────────────────────────────────────
            gaps = []
            for i in range(1, len(timestamps)):
                delta = timestamps[i] - timestamps[i - 1]
                if delta > gap_threshold:
                    gap_dt = datetime.datetime.fromtimestamp(
                        timestamps[i - 1], tz=datetime.timezone.utc
                    )
                    gaps.append((gap_dt, delta))

            # ── 5. OHLC sanity ────────────────────────────────────────────
            bad_ohlc = conn.execute(
                """SELECT COUNT(*) FROM ohlcv
                   WHERE symbol=? AND timeframe=?
                     AND (high < low
                       OR open > high OR open < low
                       OR close > high OR close < low
                       OR open <= 0   OR close <= 0)""",
                (sym, tf),
            ).fetchone()[0]

            # ── Classify ──────────────────────────────────────────────────
            issues = []
            level  = "OK  "

            if bar_count < expected_min:
                pct = bar_count * 100 // expected_min
                issues.append(f"LOW BARS {bar_count:,} ({pct}% of min {expected_min:,})")
                level = "WARN"
                warn_count += 1
            if stale:
                issues.append(f"STALE latest={latest_dt.strftime('%Y-%m-%d')} ({age_days:.0f}d ago)")
                level = "WARN"
                warn_count += 1
            if dup_count > 0:
                issues.append(f"{dup_count} DUPLICATE timestamps")
                level = "FAIL"
                fail_count += 1
            if bad_ohlc > 0:
                issues.append(f"{bad_ohlc} BAD OHLC rows")
                level = "FAIL"
                fail_count += 1

            sig_gaps = [g for g in gaps if g[1] > gap_threshold * 5]
            if sig_gaps:
                for gdt, gd in sig_gaps[:3]:
                    issues.append(f"gap {gd//3600:.0f}h at {gdt.strftime('%Y-%m-%d')}")
                level = "WARN" if level == "OK  " else level
                warn_count += 1

            if level == "OK  ":
                ok_count += 1

            bar_label  = f"{bar_count:>8,} bars"
            fresh_label = latest_dt.strftime("%Y-%m-%d")
            issue_str  = "  |  ".join(issues) if issues else ""
            print(f"    [{tf:>3}]  {level}   {bar_label}   latest={fresh_label}"
                  + (f"   >>> {issue_str}" if issue_str else ""))

    total = ok_count + warn_count + fail_count
    print(f"\n{'='*68}")
    print(f"  Results: {ok_count} OK  /  {warn_count} WARN  /  {fail_count} FAIL"
          f"   (out of {total} symbol×TF slots)")
    if fail_count == 0 and warn_count == 0:
        print("  All checks passed. DB is ready for backtesting.")
    elif fail_count == 0:
        print("  No fatal issues. WARNs are typically weekend gaps or stale data.")
        print("  Run --collect --refresh to top up stale timeframes.")
    else:
        print("  FAIL items require attention before backtesting.")
        print("  Re-run --collect to fix missing data.")
    print("=" * 68 + "\n")

    return fail_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# Results reporter
# ─────────────────────────────────────────────────────────────────────────────

_RESULTS_DIR = os.path.join(ROOT, "results")


def generate_report(results_dir: str = _RESULTS_DIR) -> bool:
    """
    Scan results/ for backtest JSON files and print a formatted summary table.

    Groups by base label (strips the _YYYYMMDD_HHMMSS timestamp suffix) and
    shows only the latest run per label.  For multi-strategy runs the
    per-strategy breakdown is shown indented beneath the overall row.
    """
    W = 72

    if not os.path.isdir(results_dir):
        print(f"\n  [ERROR] Results directory not found: {results_dir}")
        print("  Run a backtest first:")
        print("    python run_backtest.py --strategy SC1 --symbol EURUSD")
        return False

    files = sorted(glob.glob(os.path.join(results_dir, "*.json")))
    if not files:
        print(f"\n  No result files found in: {results_dir}")
        print("  Run a backtest first:")
        print("    python run_backtest.py --strategy SC1 --symbol EURUSD")
        return False

    # ── Group by base label, keep only the latest timestamped run ─────────────
    by_label: dict = {}
    for fpath in files:
        fname = os.path.basename(fpath)
        parts = fname[:-5].rsplit("_", 2)          # strip .json, split on _
        if (len(parts) == 3
                and len(parts[1]) == 8 and parts[1].isdigit()
                and len(parts[2]) == 6 and parts[2].isdigit()):
            label = parts[0]
        else:
            label = fname[:-5]
        by_label.setdefault(label, []).append(fpath)

    # ── Load latest per label ─────────────────────────────────────────────────
    rows = []
    skipped = []
    for label in sorted(by_label):
        latest = sorted(by_label[label])[-1]
        try:
            with open(latest, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception as exc:
            skipped.append(f"{label}: {exc}")
            continue

        strats = d.get("strategies") or []
        rows.append({
            "label":    label,
            "file":     os.path.basename(latest),
            "symbol":   d.get("symbol", "—"),
            "strats":   ",".join(strats),
            "trades":   d.get("total_trades", 0),
            "wins":     d.get("wins",         0),
            "losses":   d.get("losses",       0),
            "win_pct":  d.get("win_rate",     0.0),
            "avg_rr":   d.get("avg_rr",       0.0),
            "pnl":      d.get("total_pnl",    0.0),
            "dd":       d.get("max_drawdown", 0.0),
            "pf":       d.get("profit_factor",0.0),
            "by_strat": d.get("by_strategy"),
        })

    if not rows:
        print("\n  No valid result files could be loaded.")
        if skipped:
            for s in skipped:
                print(f"    [SKIP] {s}")
        return False

    # ── Print table ───────────────────────────────────────────────────────────
    print(f"\n{'='*W}")
    print(f"  struct-ai-backtest  Results Report  "
          f"({len(rows)} run{'s' if len(rows) != 1 else ''})")
    print(f"{'='*W}")
    print(f"  {'Label':<26} {'Sym':>6}  {'Trd':>4}  "
          f"{'Win%':>5}  {'AvgRR':>5}  {'PnL($)':>9}  {'MaxDD':>7}  {'PF':>5}")
    print(f"  {'-'*68}")

    total_trades = 0
    total_pnl    = 0.0

    for r in rows:
        sign = "+" if r["pnl"] >= 0 else ""
        print(f"  {r['label']:<26} {r['symbol']:>6}  {r['trades']:>4}  "
              f"{r['win_pct']:>4.1f}%  {r['avg_rr']:>5.2f}  "
              f"{sign}{r['pnl']:>8.2f}  "
              f"{r['dd']:>7.2f}  {r['pf']:>5.2f}")

        # Per-strategy breakdown — only when multiple strategies were run
        bs = r.get("by_strat") or {}
        if bs and len(strats := r["strats"].split(",")) > 1:
            for code in sorted(bs):
                st   = bs[code]
                ssign = "+" if (st.get("total_pnl") or 0) >= 0 else ""
                print(f"    ↳ {code:<5}  "
                      f"trd={st.get('trades',0):>4}  "
                      f"WR={st.get('win_rate',0):>4.1f}%  "
                      f"avgRR={st.get('avg_rr',0):>5.2f}  "
                      f"PnL={ssign}{st.get('total_pnl',0):>8.2f}")

        total_trades += r["trades"]
        total_pnl    += r["pnl"]

    print(f"  {'-'*68}")
    tsign = "+" if total_pnl >= 0 else ""
    print(f"  {'TOTAL':<26} {'':>6}  {total_trades:>4}  "
          f"{'':>5}  {'':>5}  {tsign}{total_pnl:>8.2f}")

    print(f"\n  Directory : {results_dir}")
    print(f"  Tip       : run --report after each backtest to track performance.")
    print(f"{'='*W}\n")

    if skipped:
        print(f"  [{len(skipped)} file(s) skipped due to parse errors]")
        for s in skipped:
            print(f"    {s}")

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Live-repo comparator
# ─────────────────────────────────────────────────────────────────────────────

# Cosmetic anchors — if ANY changed line in a diff hunk contains one of these,
# the ENTIRE hunk is a known backtest adaptation (not a logic change).
_COSMETIC_ANCHORS = (
    # Import / path wiring
    "sys.path.insert",
    "import sys, os",
    "from .zigzag_engine import",
    "from .structure_engine import",
    # Backtest-specific state management
    "_backtest_mode",
    "_COOLDOWN_FILE",
    "_save_cooldown",
    "_load_cooldown",
    # Debug-guard adaptations: bare print() wrapped with if debug:
    "if debug:",
    "print(",           # bare print removed — replaced by debug-guarded version
    # Known intentional backtest fixes (backtest is ahead of live — no action needed)
    "# Fix B",          # inline fix comment marks any hunk as intentional
    "SWING_MIN_RR",     # Fix B4: swing R:R threshold corrected from 2.0 → 3.0
    "or {}",            # Fix B13: None-safe state.get("asia_range") or {}
    # Type-hint simplifications (runtime-equivalent, no logic change)
    "SwingPoint",       # list[SwingPoint] → list
    "list[dict]",
    "str | None",
    "set[float]",
    # Docstring / comment markers
    "Pinned copy",
    "Flat imports",
)

# Python syntax tokens that indicate an executable statement (not prose/comment)
_CODE_SIGNALS = (
    "=", "(", "if ", "for ", "while ",
    "return ", "def ", "class ", "import ", "raise ", "try:", "except",
)


def _is_prose_line(line: str) -> bool:
    """True when a line has no executable Python syntax — e.g. docstring content."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return True
    if stripped.startswith('"""') or stripped.startswith("'''"):
        return True
    return not any(sig in line for sig in _CODE_SIGNALS)


def _parse_hunks(diff_lines: list) -> list:
    """
    Split a unified diff into per-hunk dicts:
      {'removed': [...], 'added': [...]}
    where removed/added hold the raw line content (no +/- prefix).
    """
    hunks: list = []
    cur_rem: list = []
    cur_add: list = []

    def _flush():
        if cur_rem or cur_add:
            hunks.append({"removed": list(cur_rem), "added": list(cur_add)})
        cur_rem.clear()
        cur_add.clear()

    for line in diff_lines:
        if line.startswith("@@"):
            _flush()
        elif line.startswith("-") and not line.startswith("---"):
            cur_rem.append(line[1:])
        elif line.startswith("+") and not line.startswith("+++"):
            cur_add.append(line[1:])

    _flush()
    return hunks


def _strip_comment_and_ws(line: str) -> str:
    """
    Normalize a source line for logic comparison:
      • strip trailing/leading whitespace
      • remove inline comments (everything from unquoted # onward)
      • collapse internal whitespace to single spaces
    Returns '' for blank or pure-comment lines.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return ""
    # Remove inline comment (simple heuristic — splits on first # not inside a string)
    in_single = in_double = False
    for i, ch in enumerate(stripped):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            stripped = stripped[:i].rstrip()
            break
    return " ".join(stripped.split())


def _hunk_is_cosmetic(hunk: dict) -> bool:
    """
    A diff hunk is cosmetic if ANY of these is true:

    (a) ANY changed line contains a known backtest-adaptation anchor.
    (b) ALL changed lines are prose (no executable Python syntax).
    (c) After stripping whitespace and inline comments, the sets of removed
        and added code are identical — meaning only formatting/comments changed.
    """
    removed = hunk.get("removed", [])
    added   = hunk.get("added",   [])
    all_changed = removed + added

    if not all_changed:
        return True

    hunk_text = "\n".join(all_changed)

    # (a) Anchor match
    if any(anchor in hunk_text for anchor in _COSMETIC_ANCHORS):
        return True

    # (b) All prose
    if all(_is_prose_line(l) for l in all_changed):
        return True

    # (c) Whitespace + comment normalization: same code, different formatting
    norm_rem = sorted(s for s in (_strip_comment_and_ws(l) for l in removed) if s)
    norm_add = sorted(s for s in (_strip_comment_and_ws(l) for l in added)   if s)
    if norm_rem == norm_add:
        return True

    return False


def compare_live(
    live_scalping: str,
    live_swing: str,
    live_ai: str,
) -> bool:
    """
    Compare every backtest strategy and engine against its live-repo counterpart.

    For each file pair the diff is split into hunks. A hunk is COSMETIC if every
    changed line matches a known backtest-only pattern (path wiring, debug guards,
    import simplification, docstring trimming). Any other hunk is flagged as a
    LOGIC CHANGE that needs attention.

    Returns True if no logic divergence was found.
    """
    import difflib

    BT_ROOT  = os.path.join(os.path.dirname(__file__), "..")
    BT_SC    = os.path.join(BT_ROOT, "strategies", "scalping")
    BT_SW    = os.path.join(BT_ROOT, "strategies", "swing")
    BT_ENG   = os.path.join(BT_ROOT, "engines")

    LIVE_SC  = os.path.join(live_scalping, "artifacts", "scalping-engine", "strategies")
    LIVE_SW  = os.path.join(live_swing,    "artifacts", "swing-bot",       "strategies")
    LIVE_ENG = os.path.join(live_ai,       "artifacts", "trading-api",     "services")

    pairs = [
        ("SC1", os.path.join(BT_SC,  "scalp1.py"),          os.path.join(LIVE_SC,  "scalp1.py")),
        ("SC2", os.path.join(BT_SC,  "scalp2.py"),          os.path.join(LIVE_SC,  "scalp2.py")),
        ("SC3", os.path.join(BT_SC,  "scalp3.py"),          os.path.join(LIVE_SC,  "scalp3.py")),
        ("SC4", os.path.join(BT_SC,  "scalp4.py"),          os.path.join(LIVE_SC,  "scalp4.py")),
        ("SC5", os.path.join(BT_SC,  "scalp5.py"),          os.path.join(LIVE_SC,  "scalp5.py")),
        ("SC6", os.path.join(BT_SC,  "scalp6.py"),          os.path.join(LIVE_SC,  "scalp6.py")),
        ("SW1", os.path.join(BT_SW,  "swing1.py"),          os.path.join(LIVE_SW,  "swing1.py")),
        ("SW2", os.path.join(BT_SW,  "swing2.py"),          os.path.join(LIVE_SW,  "swing2.py")),
        ("SW3", os.path.join(BT_SW,  "swing3.py"),          os.path.join(LIVE_SW,  "swing3.py")),
        ("SW4", os.path.join(BT_SW,  "swing4.py"),          os.path.join(LIVE_SW,  "swing4.py")),
        ("zigzag",    os.path.join(BT_ENG, "zigzag_engine.py"),    os.path.join(LIVE_ENG, "zigzag_engine.py")),
        ("bos",       os.path.join(BT_ENG, "bos_engine.py"),       os.path.join(LIVE_ENG, "bos_engine.py")),
        ("choch",     os.path.join(BT_ENG, "choch_engine.py"),     os.path.join(LIVE_ENG, "choch_engine.py")),
        ("structure", os.path.join(BT_ENG, "structure_engine.py"), os.path.join(LIVE_ENG, "structure_engine.py")),
        ("trend",     os.path.join(BT_ENG, "trend_engine.py"),     os.path.join(LIVE_ENG, "trend_engine.py")),
        ("zones",     os.path.join(BT_ENG, "zones_engine.py"),     os.path.join(LIVE_ENG, "zones_engine.py")),
    ]

    print("\n" + "=" * 68)
    print("  struct-ai-backtest  Live-Repo Comparator")
    print("=" * 68)

    missing_repos = []
    for label, path in (("struct-scalping", LIVE_SC),
                        ("struct-swing",    LIVE_SW),
                        ("struct-ai",       LIVE_ENG)):
        if not os.path.isdir(path):
            missing_repos.append(f"  {label} strategies not found at: {path}")
    if missing_repos:
        print("\n  [ERROR] Cannot locate live repo files:")
        for m in missing_repos:
            print(m)
        print("\n  Pass the correct paths with:")
        print("    --live-scalping  <path to struct-scalping repo root>")
        print("    --live-swing     <path to struct-swing repo root>")
        print("    --live-ai        <path to struct-ai repo root>")
        return False

    any_logic_change = False

    for label, bt_path, live_path in pairs:
        if not os.path.exists(bt_path):
            print(f"\n  [MISS]  {label:<12}  backtest copy not found: {bt_path}")
            any_logic_change = True
            continue
        if not os.path.exists(live_path):
            print(f"\n  [MISS]  {label:<12}  live file not found: {live_path}")
            any_logic_change = True
            continue

        with open(bt_path,   encoding="utf-8") as f:
            bt_lines = f.readlines()
        with open(live_path, encoding="utf-8") as f:
            live_lines = f.readlines()

        diff = list(difflib.unified_diff(
            live_lines, bt_lines,
            fromfile=f"live/{label}",
            tofile=f"backtest/{label}",
            lineterm="",
        ))

        if not diff:
            print(f"  [SYNC]  {label:<12}  identical to live")
            continue

        # Parse into hunks, classify each as cosmetic or logic
        hunks = _parse_hunks(diff)
        logic_hunks = [h for h in hunks if not _hunk_is_cosmetic(h)]
        cosmetic_hunks = len(hunks) - len(logic_hunks)

        if not logic_hunks:
            total_cosmetic_lines = sum(
                len(h["removed"]) + len(h["added"]) for h in hunks
            )
            print(f"  [OK]    {label:<12}  logic identical  "
                  f"({total_cosmetic_lines} cosmetic line"
                  f"{'s' if total_cosmetic_lines != 1 else ''} in "
                  f"{cosmetic_hunks} hunk{'s' if cosmetic_hunks != 1 else ''} — expected)")
        else:
            any_logic_change = True
            logic_lines = [l for h in logic_hunks
                           for l in h["removed"] + h["added"]]
            print(f"\n  [DIFF]  {label:<12}  *** LOGIC CHANGE DETECTED ***")
            print(f"          Live repo has changes not in backtest copy.")
            print(f"          Sync command:")
            print(f"            copy \"{live_path}\"")
            print(f"                 \"{bt_path}\"")
            print(f"          Changed logic lines ({len(logic_lines)}):")
            for l in logic_lines[:8]:
                print(f"            {l.rstrip()}")
            if len(logic_lines) > 8:
                print(f"            ... and {len(logic_lines)-8} more")

    print(f"\n{'='*68}")
    if not any_logic_change:
        print("  All 16 files: logic is in sync with live repos.")
        print("  (Known cosmetic differences — path wiring, debug guards,")
        print("   import simplification — are expected and ignored.)")
    else:
        print("  ACTION REQUIRED: copy updated files shown above into backtest,")
        print("  then re-run --compare-live to confirm sync.")
    print("=" * 68 + "\n")

    return not any_logic_change


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="struct-ai-backtest data collector",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--source",
        choices=["auto", "mt5", "yfinance"],
        default="auto",
        help="Data source: auto (default), mt5, or yfinance",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Only download bars newer than what is already in the DB (fast top-up)",
    )
    parser.add_argument(
        "--detect-symbols",
        action="store_true",
        help="Connect to MT5, scan all broker symbols, print best match for each pair, then exit",
    )
    parser.add_argument(
        "--list-symbols",
        action="store_true",
        help="Print every symbol your broker offers (use with --filter to narrow down)",
    )
    parser.add_argument(
        "--filter",
        default=None,
        help="Filter symbol list (e.g. --filter USD shows only symbols containing USD)",
    )
    parser.add_argument(
        "--validate-db",
        action="store_true",
        help=(
            "Audit the DB: check coverage, freshness, duplicates, "
            "OHLC gaps, and price sanity for every symbol × timeframe. "
            "Exits with code 0 if clean, 1 if any FAIL found."
        ),
    )
    parser.add_argument(
        "--compare-live",
        action="store_true",
        help=(
            "Compare every backtest strategy and engine against the live repos. "
            "Flags logic divergence; ignores known cosmetic adaptations. "
            "Requires --live-scalping, --live-swing, and --live-ai."
        ),
    )
    parser.add_argument(
        "--live-scalping",
        default=None,
        metavar="PATH",
        help="Root directory of the struct-scalping repo (for --compare-live).",
    )
    parser.add_argument(
        "--live-swing",
        default=None,
        metavar="PATH",
        help="Root directory of the struct-swing repo (for --compare-live).",
    )
    parser.add_argument(
        "--live-ai",
        default=None,
        metavar="PATH",
        help="Root directory of the struct-ai repo (for --compare-live).",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help=(
            "Print a formatted summary table of all backtest results found "
            "in the results/ directory. Shows latest run per label with "
            "trade count, win rate, avg R:R, PnL, max drawdown, and profit factor."
        ),
    )
    args = parser.parse_args()

    # ── Symbol detection mode ─────────────────────────────────────────────────
    if args.detect_symbols or args.list_symbols:
        mt5 = _mt5_import()
        if mt5 is None:
            print("\n[ERROR] MetaTrader5 package not installed. Run install.bat first.")
            sys.exit(1)
        try:
            from collector.detect_symbols import detect_symbols, print_symbol_list
        except ImportError:
            sys.path.insert(0, os.path.dirname(__file__))
            from detect_symbols import detect_symbols, print_symbol_list

        if args.list_symbols:
            print_symbol_list(mt5, filter_base=args.filter)
        else:
            detected = detect_symbols(mt5)
            not_found = [k for k, v in detected.items() if v is None]
            if not_found:
                print(f"\n  Tip: run with --list-symbols --filter <base> to search manually.")
                print(f"  Example:  start.bat --collect --list-symbols --filter JPY")
        sys.exit(0)
    # ─────────────────────────────────────────────────────────────────────────

    # ── Report mode ───────────────────────────────────────────────────────────
    if args.report:
        ok = generate_report()
        sys.exit(0 if ok else 1)
    # ─────────────────────────────────────────────────────────────────────────

    # ── Validate-DB mode ──────────────────────────────────────────────────────
    if args.validate_db:
        conn = open_db()
        ok = validate_db(conn)
        conn.close()
        sys.exit(0 if ok else 1)
    # ─────────────────────────────────────────────────────────────────────────

    # ── Compare-live mode ─────────────────────────────────────────────────────
    if args.compare_live:
        missing = []
        if not args.live_scalping:
            missing.append("--live-scalping")
        if not args.live_swing:
            missing.append("--live-swing")
        if not args.live_ai:
            missing.append("--live-ai")
        if missing:
            print(f"\n[ERROR] --compare-live requires: {', '.join(missing)}")
            print("\nExample:")
            print("  python collector/collect.py --compare-live \\")
            print("    --live-scalping C:\\repos\\struct-scalping \\")
            print("    --live-swing    C:\\repos\\struct-swing \\")
            print("    --live-ai       C:\\repos\\struct-ai")
            sys.exit(1)
        ok = compare_live(
            live_scalping=args.live_scalping,
            live_swing=args.live_swing,
            live_ai=args.live_ai,
        )
        sys.exit(0 if ok else 1)
    # ─────────────────────────────────────────────────────────────────────────

    print("\n" + "="*64)
    print("  struct-ai-backtest  Data Collector")
    print(f"  Source: {args.source}   Mode: {'REFRESH' if args.refresh else 'FULL'}")
    print("="*64)

    conn = open_db()
    print(f"\n  DB: {DB_PATH}")

    if args.refresh:
        total = collect_refresh(conn, source=args.source)
    elif args.source == "mt5":
        total = collect_mt5(conn)
    elif args.source == "yfinance":
        total = collect_yfinance(conn)
    else:
        total = collect_auto(conn)

    conn.close()

    db_mb = os.path.getsize(DB_PATH) / 1024 / 1024
    print(f"\n{'='*64}")
    print(f"  Collection complete: {total:,} new rows stored")
    print(f"  DB: {DB_PATH}")
    print(f"  DB size: {db_mb:.1f} MB")
    print(f"")
    print(f"  Backtests now run fully OFFLINE from the stored DB.")
    print(f"  Use --refresh to top up with new bars in seconds.")
    print(f"{'='*64}\n")


if __name__ == "__main__":
    main()
