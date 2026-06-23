"""
Support/Resistance Zones Engine. Pinned copy. Flat imports.
"""

CLUSTER_PIPS    = 1.5
ZONE_WIDTH_PIPS = 3.0


def _pip_size(price: float) -> float:
    return 0.01 if price > 50 else 0.0001


def detect_zones(swings: list, timeframe: str = "1h", current_price: float = None) -> list:
    if not swings:
        return []

    if current_price is not None:
        pip = _pip_size(current_price)
    else:
        median_price = sorted(s["price"] for s in swings)[len(swings) // 2]
        pip = _pip_size(median_price)

    cluster_threshold = CLUSTER_PIPS    * pip
    zone_width        = ZONE_WIDTH_PIPS * pip

    tf_strength = {"d1": 4, "4h": 3, "1h": 2, "15m": 1, "5m": 0}
    base_strength = tf_strength.get(timeframe, 1)

    pairs = sorted(zip([s["price"] for s in swings], [s["time"] for s in swings]),
                   key=lambda x: x[0])
    levels = [p[0] for p in pairs]
    times  = [p[1] for p in pairs]

    clusters = []
    used = [False] * len(levels)

    for i in range(len(levels)):
        if used[i]:
            continue

        cluster_prices = [levels[i]]
        cluster_times = [times[i]]

        for j in range(i + 1, len(levels)):
            if not used[j]:
                cluster_mean = sum(cluster_prices) / len(cluster_prices)
                if abs(levels[j] - cluster_mean) <= cluster_threshold:
                    cluster_prices.append(levels[j])
                    cluster_times.append(times[j])
                    used[j] = True

        used[i] = True

        if len(cluster_prices) >= 2:
            center = sum(cluster_prices) / len(cluster_prices)
            touches = len(cluster_prices)
            clusters.append({
                "center": center,
                "touches": touches,
                "first_time": min(cluster_times),
                "last_time": max(cluster_times),
            })

    zones = []
    for cluster in clusters:
        half_w = zone_width + (cluster["touches"] * pip * 0.1)
        strength = min(base_strength + cluster["touches"], 5)
        zones.append({
            "top": round(cluster["center"] + half_w, 5),
            "bottom": round(cluster["center"] - half_w, 5),
            "center": round(cluster["center"], 5),
            "touches": cluster["touches"],
            "strength": strength,
            "timeframe": timeframe,
            "start_time": cluster["first_time"],
            "end_time": cluster["last_time"],
        })

    zones.sort(key=lambda z: z["strength"], reverse=True)
    return zones
