"""
Structure Classification Engine. Pinned copy. Flat imports.
"""

LABEL_HH  = "HH"
LABEL_HL  = "HL"
LABEL_LH  = "LH"
LABEL_LL  = "LL"
LABEL_EQH = "EQH"
LABEL_EQL = "EQL"


def classify_structure(swings: list) -> list:
    labels = []
    prev_high = None
    prev_low  = None

    for swing in swings:
        label = None

        if swing["kind"] == "high":
            if prev_high is not None:
                if swing["price"] > prev_high:
                    label = LABEL_HH
                elif swing["price"] == prev_high:
                    label = LABEL_EQH
                else:
                    label = LABEL_LH
            prev_high = swing["price"]

        else:
            if prev_low is not None:
                if swing["price"] > prev_low:
                    label = LABEL_HL
                elif swing["price"] == prev_low:
                    label = LABEL_EQL
                else:
                    label = LABEL_LL
            prev_low = swing["price"]

        if label:
            labels.append({
                "time": swing["time"],
                "price": swing["price"],
                "label": label,
                "kind": swing["kind"],
                "index": swing["index"],
            })

    return labels
