"""
BOS Engine — Break of Structure.
Pinned copy. Flat imports (no relative package prefix).
FIX B2: Replaced per-element pd.Timestamp(t).timestamp() loop with vectorised
         df["ts_unix"].astype("int64") — 30-100× faster per call.
"""
import pandas as pd


def detect_bos(df: pd.DataFrame, swings: list, structure_labels: list, trend: str = "neutral", lookback_hours: int = 48) -> list:
    if len(swings) < 2 or len(df) == 0:
        return []

    bos_events = []
    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values
    # FIX B2: ts_unix is already Unix-seconds int — avoid slow pd.Timestamp per element
    times_arr = df["ts_unix"].astype("int64").tolist()

    broken_levels: set = set()

    for label_item in structure_labels:
        level     = label_item["price"]
        label     = label_item["label"]
        swing_idx = label_item["index"]

        if level in broken_levels:
            continue

        for i in range(swing_idx + 1, len(df)):
            candle_time = times_arr[i]
            close       = closes[i]

            if label in ("HH", "LH", "EQH") and close > level:
                if trend in ("bullish", "neutral"):
                    bos_events.append({
                        "time":          candle_time,
                        "price":         round(level, 5),
                        "direction":     "bullish",
                        "label":         "BOS \u2191",
                        "level_broken":  round(level, 5),
                        "wick_extreme":  round(float(highs[i]), 5),
                    })
                broken_levels.add(level)
                break

            if label in ("LL", "HL", "EQL") and close < level:
                if trend in ("bearish", "neutral"):
                    bos_events.append({
                        "time":          candle_time,
                        "price":         round(level, 5),
                        "direction":     "bearish",
                        "label":         "BOS \u2193",
                        "level_broken":  round(level, 5),
                        "wick_extreme":  round(float(lows[i]), 5),
                    })
                broken_levels.add(level)
                break

    if times_arr:
        cutoff    = times_arr[-1] - (lookback_hours * 3600)
        bos_events = [e for e in bos_events if e["time"] >= cutoff]
    return bos_events
