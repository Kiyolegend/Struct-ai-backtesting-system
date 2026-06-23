"""
CHOCH Engine — Change of Character.
Pinned copy. Flat imports.
FIX B3: Replaced per-element pd.Timestamp(t).timestamp() loop with vectorised
         df["ts_unix"].astype("int64") — 30-100× faster per call.
"""
import pandas as pd


def detect_choch(df: pd.DataFrame, swings: list, structure_labels: list, trend: str, lookback_hours: int = 24) -> list:
    if len(swings) < 3 or len(structure_labels) < 2 or len(df) == 0:
        return []

    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values
    # FIX B3: ts_unix is already Unix-seconds int — avoid slow pd.Timestamp per element
    times_arr = df["ts_unix"].astype("int64").tolist()

    choch_events = []

    last_hl = next((l for l in reversed(structure_labels) if l["label"] == "HL"), None)
    last_lh = next((l for l in reversed(structure_labels) if l["label"] == "LH"), None)

    if last_hl and trend in ("bullish", "neutral"):
        level     = last_hl["price"]
        swing_idx = last_hl["index"]
        for i in range(swing_idx + 1, len(df)):
            if closes[i] < level:
                choch_events.append({
                    "time":         times_arr[i],
                    "price":        round(level, 5),
                    "direction":    "bearish",
                    "label":        "CHOCH",
                    "broken_label": "HL",
                    "wick_extreme": round(float(lows[i]), 5),
                })
                break

    if last_lh and trend in ("bearish", "neutral"):
        level     = last_lh["price"]
        swing_idx = last_lh["index"]
        for i in range(swing_idx + 1, len(df)):
            if closes[i] > level:
                choch_events.append({
                    "time":         times_arr[i],
                    "price":        round(level, 5),
                    "direction":    "bullish",
                    "label":        "CHOCH",
                    "broken_label": "LH",
                    "wick_extreme": round(float(highs[i]), 5),
                })
                break

    if times_arr:
        cutoff       = times_arr[-1] - (lookback_hours * 3600)
        choch_events = [e for e in choch_events if e["time"] >= cutoff]
    return choch_events
