"""
Trend Engine. Pinned copy. Flat imports.
"""

def detect_trend(structure_labels: list) -> dict:
    if len(structure_labels) < 2:
        return {
            "trend": "neutral",
            "confidence": 0,
            "last_high_label": None,
            "last_low_label": None,
            "last_labels": [],
        }

    last_high_label = None
    last_high_pos = None
    last_low_label = None
    last_low_pos = None

    total = len(structure_labels)

    for i in range(total - 1, -1, -1):
        item = structure_labels[i]
        lbl = item["label"]

        if lbl in ("HH", "LH", "EQH") and last_high_label is None:
            last_high_label = lbl
            last_high_pos = i

        if lbl in ("HL", "LL", "EQL") and last_low_label is None:
            last_low_label = lbl
            last_low_pos = i

        if last_high_label is not None and last_low_label is not None:
            break

    last_labels = [x["label"] for x in structure_labels[-6:]]

    if last_high_label == "HH" and last_low_label == "HL":
        trend = "bullish"
        recency_threshold = total - 4
        if (last_high_pos is not None and last_high_pos >= recency_threshold and
                last_low_pos is not None and last_low_pos >= recency_threshold):
            confidence = 100
        else:
            confidence = 75

    elif last_high_label == "LH" and last_low_label == "LL":
        trend = "bearish"
        recency_threshold = total - 4
        if (last_high_pos is not None and last_high_pos >= recency_threshold and
                last_low_pos is not None and last_low_pos >= recency_threshold):
            confidence = 100
        else:
            confidence = 75

    else:
        trend = "neutral"
        confidence = 50

    return {
        "trend": trend,
        "confidence": confidence,
        "last_high_label": last_high_label,
        "last_low_label": last_low_label,
        "last_labels": last_labels,
    }
