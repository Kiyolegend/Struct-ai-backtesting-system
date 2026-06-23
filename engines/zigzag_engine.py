"""
ZigZag Engine — The backbone of all market structure analysis.
Pinned copy from STRUCT.ai repo. Flat imports (no package prefix).
FIX B4: Replaced per-pivot pd.Timestamp(times[i]).timestamp() with vectorised
         df["ts_unix"].values array — removes slow datetime conversion from the
         inner loop entirely.
"""

import pandas as pd
import numpy as np
from typing import TypedDict

FRACTAL_N = 5


class SwingPoint(TypedDict):
    index: int
    time: int
    price: float
    kind: str  # "high" or "low"


def detect_swings(df: pd.DataFrame, fractal_n: int = FRACTAL_N) -> list:
    n      = fractal_n
    highs  = df["high"].values
    lows   = df["low"].values
    # FIX B4: ts_unix already contains Unix-seconds ints — no pd.Timestamp needed
    times_unix = df["ts_unix"].values.astype("int64")

    raw_pivots = []

    for i in range(n, len(df) - n):
        window_highs = highs[i - n: i + n + 1]
        window_lows  = lows[i - n: i + n + 1]

        if highs[i] == window_highs.max():
            raw_pivots.append({
                "index": i,
                "time":  int(times_unix[i]),
                "price": float(round(highs[i], 5)),
                "kind":  "high",
            })

        if lows[i] == window_lows.min():
            raw_pivots.append({
                "index": i,
                "time":  int(times_unix[i]),
                "price": float(round(lows[i], 5)),
                "kind":  "low",
            })

    if not raw_pivots:
        return []

    alternating = [raw_pivots[0]]

    for pivot in raw_pivots[1:]:
        last = alternating[-1]
        if pivot["kind"] == last["kind"]:
            if pivot["kind"] == "high" and pivot["price"] > last["price"]:
                alternating[-1] = pivot
            elif pivot["kind"] == "low" and pivot["price"] < last["price"]:
                alternating[-1] = pivot
        else:
            alternating.append(pivot)

    MIN_SWING_PIPS = 5
    if len(alternating) >= 2:
        pip     = 0.01 if alternating[0]["price"] > 50 else 0.0001
        min_move = MIN_SWING_PIPS * pip
        sized   = [alternating[0]]
        for pt in alternating[1:]:
            if abs(pt["price"] - sized[-1]["price"]) >= min_move:
                sized.append(pt)
        alternating = sized

    return alternating


def swings_to_zigzag_lines(swings: list) -> list:
    lines = []
    for i in range(len(swings) - 1):
        lines.append({
            "from_time":  swings[i]["time"],
            "from_price": swings[i]["price"],
            "to_time":    swings[i + 1]["time"],
            "to_price":   swings[i + 1]["price"],
        })
    return lines
