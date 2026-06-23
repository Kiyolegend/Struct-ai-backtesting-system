# struct-ai-backtest

A standalone Windows backtesting system for all 10 STRUCT.ai trading strategies:
- **SC1–SC6** — Scalping strategies (5M/15M entry timeframe)
- **SW1–SW4** — Swing strategies (1H/4H entry timeframe)

---

## Quick Start (Windows)

```
1. Double-click  install.bat          — installs Python dependencies into .venv
2. Double-click  start.bat --collect  — downloads market data (~5 min, one-time)
3. Double-click  start.bat            — runs all 10 strategies on USD/JPY
```

After each run, a results HTML file opens automatically in your browser.

---

## Requirements

- **Windows 10 or 11** (64-bit)
- **Python 3.10 or higher** — download from https://python.org  
  Make sure to check **"Add Python to PATH"** during installation.
- Internet connection for first-time data collection

---

## Folder Structure

```
struct-ai-backtest\
│
├── install.bat             ← Run once to set up dependencies
├── start.bat               ← Run the backtest (pass args here)
├── run_backtest.py         ← Main entry point
├── backtest_engine.py      ← Bar-by-bar replay core
├── config.py               ← Unified backtest config (spread, symbols, params)
├── news_filter_live.py     ← Stub (no real news blocking in backtest)
├── requirements.txt        ← Python packages needed
│
├── engines\                ← Pinned copies of all live analysis engines
│   ├── zigzag_engine.py    ← Swing detection (FRACTAL_N=5)
│   ├── structure_engine.py ← HH/HL/LH/LL/EQH/EQL classification
│   ├── bos_engine.py       ← Break of Structure detection
│   ├── choch_engine.py     ← Change of Character detection
│   ├── trend_engine.py     ← Trend direction + confidence
│   └── zones_engine.py     ← S/R zone clustering
│
├── strategies\
│   ├── scalping\
│   │   ├── scalp1.py  (SC1) ← BOS + OB entry
│   │   ├── scalp2.py  (SC2) ← CHoCH reversal
│   │   ├── scalp3.py  (SC3) ← Zone fade
│   │   ├── scalp4.py  (SC4) ← Asia range breakout
│   │   ├── scalp5.py  (SC5) ← Trend continuation
│   │   └── scalp6.py  (SC6) ← Sweep + reversal
│   └── swing\
│       ├── swing1.py  (SW1) ← D1 BOS + 4H entry
│       ├── swing2.py  (SW2) ← Golden zone retracement
│       ├── swing3.py  (SW3) ← CHoCH + trend flip
│       └── swing4.py  (SW4) ← Multi-TF confluence
│
├── collector\
│   └── collect.py          ← Downloads OHLCV via yfinance → SQLite
│
├── data\
│   └── market_data.db      ← SQLite (created after first collect run)
│
├── results\                ← Output JSON + HTML files (auto-created)
│   └── *.json / *.html
│
└── viewer\
    └── index.html          ← Standalone HTML viewer (no server needed)
```

---

## Data Tiers

| Timeframe | Lookback | Approx. bars (1 pair) |
|-----------|----------|----------------------|
| D1        | 5 years  | ~1,300               |
| 4H        | 3 years  | ~6,500               |
| 1H        | 2 years  | ~17,500              |
| 15M       | 2 years  | ~70,000              |
| 5M        | 1 year   | ~105,000             |

Pairs collected: USD/JPY, EUR/USD, GBP/USD, AUD/USD, USD/CHF  
(EUR/JPY, GBP/JPY, USD/CAD disabled — matches live DISABLED_SYMBOLS)

> **Note on 5M data:** Yahoo Finance limits 5M to the last 60 days per request.
> The collector downloads in rolling 60-day chunks to maximise coverage,
> but older 5M data may not be available from yfinance.

---

## Command Line Reference

```
start.bat [OPTIONS]

Options:
  --symbol     SYMBOL          Pair to test (default: USD/JPY)
  --strategies CODE [CODE ...]  Strategy codes (default: all 10)
  --lot        FLOAT           Lot size (default: 0.02 scalp, 0.01 swing-only)
  --debug                      Enable verbose per-bar strategy output
  --collect                    Re-download market data before running
  --no-viewer                  Skip auto-opening HTML results
```

Examples:
```batch
REM Collect data then run all strategies on USD/JPY
start.bat --collect

REM Run only scalping strategies on EUR/USD
start.bat --symbol EUR/USD --strategies SC1 SC2 SC3 SC4 SC5 SC6

REM Run only swing strategies
start.bat --strategies SW1 SW2 SW3 SW4

REM Run one strategy with debug output
start.bat --strategies SC1 --debug --no-viewer

REM Small lot size test
start.bat --lot 0.01 --symbol GBP/USD
```

---

## Results Viewer

After each backtest run, a standalone HTML file is saved to `results\` and
opened in your default browser automatically.

The viewer shows:
- **KPI cards** — Total P&L, win rate, trades, R:R, profit factor, max drawdown
- **Equity curve** — Cumulative P&L over the backtest period
- **Per-strategy breakdown** — Individual cards with mini win-rate bar
- **Full trade log** — Filterable by strategy / result (TP/SL) / trade type

You can also open `viewer\index.html` directly and use the "Load Results JSON"
button to load any `.json` file from the `results\` folder.

---

## No-Lookahead Guarantee

The replay engine enforces strict no-lookahead:

1. For each bar `i` in the entry timeframe, only bars `[0 .. i]` are passed
   to the engine computation functions.
2. Fractal swing detection internally excludes the last `FRACTAL_N=5` bars of
   each visible slice (this is where the `detect_swings()` inner loop naturally
   stops), so a pivot at bar `i` is only confirmed when bars `i+5` are visible.
3. Trade fills use the **open price of the next bar** (simulated via bar
   close of the signal bar plus spread) — worst-case realistic fill.
4. TP/SL checks use bar **high** and **low** within the same bar,
   not tick data. Both TP and SL can be hit in the same bar — TP is checked
   first (conservative / gives benefit of the doubt to the strategy).

---

## Spread & Commission Model

Taken from `config.py → SYMBOL_CONFIG`:

| Pair    | Spread (pips) | Commission (pips) | Total cost |
|---------|--------------|-------------------|------------|
| USD/JPY | 1.0          | 1.0               | 2.0        |
| EUR/USD | 1.0          | 0.8               | 1.8        |
| GBP/USD | 1.2          | 1.0               | 2.2        |
| AUD/USD | 1.2          | 0.9               | 2.1        |
| USD/CHF | 1.5          | 0.7               | 2.2        |

All costs are deducted from every trade's P&L automatically.

---

## Adjusting Parameters

Edit `config.py` to change:
- **`ACCOUNT_BALANCE`** — used for risk % calculations in strategy logic
- **`MIN_RR`** / **`SWING_MIN_RR`** — minimum risk:reward threshold
- **`MIN_CONFIDENCE`** — minimum strategy confidence to take a trade
- **`DEFAULT_LOT`** / **`MAX_LOT`** — default and maximum position sizes
- **`SCAN_SYMBOLS`** / **`DISABLED_SYMBOLS`** — which pairs to enable

> Changes to `config.py` take effect immediately on next `start.bat` run.
> No rebuild required.

---

## Troubleshooting

**"Database not found"**  
→ Run `start.bat --collect` to download market data first.

**"Python not found"**  
→ Install Python 3.10+ from https://python.org and check "Add Python to PATH".

**"No data for 5M timeframe"**  
→ Yahoo Finance limits 5M to ~60 days. This is expected. SC strategies will
   still run — the engine falls back to 15M for the entry bar.

**"Strategy load error"**  
→ The strategy file imports failed. Run `start.bat --debug --strategies SC1`
   to see the detailed import traceback.

**Viewer shows blank chart**  
→ If opened directly from `results\`, use the "Load Results JSON" button to
   load the matching `.json` file from the same folder.
