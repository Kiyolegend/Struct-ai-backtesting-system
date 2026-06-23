"""
Strategy 1 — MTF Pullback Precision Scalping
=============================================
Trade in direction of higher timeframe bias (4H + 1H),
wait for pullback on 15M (HL / LH formation),
then use 5M BOS for precise entry with tight stop loss,
and ride the 15M impulse move.

Session 2 fixes applied:
  - 24h age cap on 15M pullback label
  - 1h freshness check on 5M BOS events
  - Tighter distance scoring: 15/10/5 at ≤5/≤10/≤15 pips
  - Counter CHoCH window: 4h look-back with time check

Session 3 refinements applied:
  - Minimum score raised from 80 → 85 (forces stronger confluence)
  - 5M BOS freshness tightened from 2h → 1h (no stale triggers)
  - Distance hard reject tightened from >15p → >10p (no chase entries)
  - Distance scoring: ≤15p tier (5pts) removed — 10p is now the outer limit
  - Counter-BOS filter added: any counter-direction 5M BOS within 30min = skip

Quality upgrades applied (v2):
  - GATE: Post-BOS structure must hold — after the most recent 5M BOS fires,
    subsequent 5M candles must not make a new low (bullish) or new high (bearish)
    beyond the close of the very first post-BOS candle.
    Catches setups where the BOS was valid when it fired but the continuation has
    already failed — prevents entering into a broken 5M structure.

Pullback logic upgrade (v3):
  - CHANGED: 15M bearish bias is now the BEST setup (+30 pts) — it means the
    pullback IS in progress and we are entering before the impulse, not after.
    Previously this was a hard reject, which caused late entries.
  - 15M neutral = good (+20 pts) — pullback may be finishing.
  - 15M bullish = allowed but lower (+15 pts) — continuation, may be extended.
  - BOS freshness tightened 1h → 30 min — a 50-min-old BOS is not the start
    of the impulse. 30 min keeps entry at the very beginning of the move.
  - Entry distance tightened 13p → 10p outer limit; tiers 3/7/10p.
  - CHoCH soft filter: 1 counter CHoCH = allowed (that IS the pullback starting).
    Reject only when 2+ counter CHoCHs exist with no bullish/bearish structural
    recovery in between — that indicates a genuine reversal, not a pullback.

Institutional confluence upgrade (v4):
  - Zone scoring upgraded 5pts → 10pts max, now checks WHERE THE 15M HL FORMED
    not just where price currently sits.
    * 10pts: 15M HL formed at a demand zone or S/R level AND current price is
      still within that zone — the pullback found institutional support and we
      are entering exactly at the zone.
    * 5pts: 15M HL at zone/S&R only (price moved slightly past it — level was
      respected but entry is a pip or two above the zone).
    * 5pts: current price in zone only (zone present, but HL didn't form there).
    * 0pts: no zone confluence at all.
  - To keep max score = 100, session bonus reduced 10pts → 5pts.
    Rationale: session timing is a circumstantial filter (you can't force a setup
    during London); zone confluence is a structural quality signal about the
    specific setup itself and deserves higher weight.
    - New score breakdown: bias(30) + pullback(20) + bos(20) + loc(15) + sess(5)
    + zone(10) = 100 max.

1H structure alignment bonus (v5):
  - Optional bonus: +5pts when the 15M pullback HL aligns with a confirmed 1H HL.
  - NOT a gate — good setups without 1H alignment still fire.
  - When combined with zone confluence (HL+price@zone = 10pts) this can push
    score to 105 in the best cases. Threshold stays at 85.
  - Data is already in state["1h"]["structure"] — no new endpoints needed.

Asia range alignment bonus (v6):
  - Optional bonus: +5pts when the 15M pullback HL formed at the Asia session
    high (bullish) or Asia session low (bearish).
  - Only relevant during London open — Asia range becomes key S/R after break.
  - Data already in state["asia_range"] — no new endpoints needed.
  - Theoretical max with all bonuses: 110 pts. Threshold stays at 85.

15M HL recency bonus (v7):
  - Optional bonus: +5pts if pullback HL formed < 4h ago, +3pts if < 12h ago.
  - Within the 24h cap, a fresh HL means the pullback just completed — the
    institutional absorption is recent and the impulse is imminent.
  - Uses pb_time already computed in Step 3 — zero extra data needed.
  - Theoretical max with all bonuses: 115 pts. Threshold stays at 85.
"""

import sys, os, math, time as _time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import config


def _last_label(structure: list, label: str | tuple) -> dict | None:
    """Return the most recent structure point dict with a given label."""
    labels = (label,) if isinstance(label, str) else label
    for s in reversed(structure[-20:]):
        if not isinstance(s, dict):
            continue
        if s.get("label") in labels:
            return s
    return None


# ── Post-BOS structure holds ───────────────────────────────────────────────────
def _structure_holds(candles_5m: list, confirm_time: int, direction: str) -> bool:
    """
    After the 5M BOS timestamp, subsequent 5M candles must not violate the
    continuation by making a new low (bullish) or new high (bearish) beyond
    the close of the very first post-BOS candle.

    This catches the case where the BOS was genuine at the time it fired but
    price has since reversed — the pullback continuation has already failed
    and the setup should be skipped entirely.

    Returns True (structure intact) when:
      - Fewer than 2 candles exist after the event (too early to judge).
      - No subsequent candle has violated the anchor close.
    Returns False (structure broken) when a subsequent candle's low (bullish)
    or high (bearish) breaches the first post-BOS candle's close.
    """
    post = [c for c in candles_5m if c.get("time", 0) > confirm_time]
    if len(post) < 2:
        return True

    anchor_close = post[0].get("close", 0)
    subsequent   = post[1:]

    if direction == "bullish":
        return not any(c.get("low", 0) < anchor_close for c in subsequent)
    else:
        return not any(c.get("high", float("inf")) > anchor_close for c in subsequent)


def check(state: dict, debug: bool = False) -> dict | None:
    if not isinstance(state, dict):
        return None

    bias  = state.get("bias", {})
    price = state.get("current_price")
    s5m   = state.get("5m", {})
    s15m  = state.get("15m", {})
    candles_5m = s5m.get("candles", [])

    if not price or not isinstance(price, (int, float)) or not math.isfinite(price):
        return None

    # ── Step 1: Higher timeframe bias alignment ──────────────────────────
    b4h  = bias.get("4h",  "neutral")
    b1h  = bias.get("1h",  "neutral")
    b15m = bias.get("15m", "neutral")

    bull_4h  = b4h  == "bullish"
    bear_4h  = b4h  == "bearish"
    bull_1h  = b1h  == "bullish"
    bear_1h  = b1h  == "bearish"
    bull_15m = b15m == "bullish"
    bear_15m = b15m == "bearish"

    both_bull = bull_4h and bull_1h
    both_bear = bear_4h and bear_1h

    if both_bull:
        direction  = "bullish"
        trade_type = "BUY"
        # v3: 15M bearish = pullback in progress = BEST entry point
        # v3: 15M bullish = continuation, may already be extended = lower score
        if bear_15m:
            bias_score = 30   # pullback in progress — ideal
        elif bull_15m:
            bias_score = 15   # continuation — valid but may be extended
        else:
            bias_score = 20   # neutral — pullback likely finishing
    elif both_bear:
        direction  = "bearish"
        trade_type = "SELL"
        if bull_15m:
            bias_score = 30   # pullback in progress — ideal
        elif bear_15m:
            bias_score = 15   # continuation — valid but may be extended
        else:
            bias_score = 20   # neutral — pullback likely finishing
    else:
        if debug: print("    [S1] skip: 4H and 1H not both aligned")
        return None

    # ── Step 2: 15M CHoCH soft filter — 4h look-back ─────────────────────
    # v3 CHANGE: Was a hard reject on any single counter CHoCH.
    # Now: 1 counter CHoCH is ALLOWED — that IS the pullback starting.
    # Reject only when 2+ counter CHoCHs exist with NO bullish/bearish
    # structural recovery (HL/HH or LH/LL) between them. That pattern
    # indicates a genuine multi-wave reversal, not a simple pullback.
    now_sec = config.get_broker_ts(state)
    choch_15m    = s15m.get("choch", [])
    recent_choch = [c for c in choch_15m
                    if isinstance(c, dict) and (now_sec - c.get("time", 0)) <= 4 * 3600][-3:]

    if direction == "bullish":
        counter_chochs = [c for c in recent_choch if c.get("direction") == "bearish"]
        if len(counter_chochs) >= 2:
            struct_15m_now   = s15m.get("structure", [])
            first_choch_time = min(c.get("time", 0) for c in counter_chochs)
            recovery = [s for s in struct_15m_now
                        if isinstance(s, dict)
                        and s.get("label") in ("HL", "HH")
                        and s.get("time", 0) > first_choch_time]
            if not recovery:
                if debug: print("    [S1] skip: 2+ bearish CHoCHs on 15M, no HL/HH recovery — reversal, not pullback")
                return None
    else:
        counter_chochs = [c for c in recent_choch if c.get("direction") == "bullish"]
        if len(counter_chochs) >= 2:
            struct_15m_now   = s15m.get("structure", [])
            first_choch_time = min(c.get("time", 0) for c in counter_chochs)
            recovery = [s for s in struct_15m_now
                        if isinstance(s, dict)
                        and s.get("label") in ("LH", "LL")
                        and s.get("time", 0) > first_choch_time]
            if not recovery:
                if debug: print("    [S1] skip: 2+ bullish CHoCHs on 15M, no LH/LL recovery — reversal, not pullback")
                return None

    # ── Step 3: 15M pullback — 24h age cap ───────────────────────────────
    struct_15m = s15m.get("structure", [])
    recent_15m = struct_15m[-12:]

    pullback_label = "HL" if direction == "bullish" else "LH"
    continue_label = "HH" if direction == "bullish" else "LL"

    pullback_item = _last_label(struct_15m, pullback_label)

    if pullback_item is None:
        if debug: print(f"    [S1] skip: no 15M {pullback_label} found")
        return None

    pb_time = pullback_item.get("time", 0)
    if pb_time and (now_sec - pb_time) > 24 * 3600:
        if debug: print(f"    [S1] skip: 15M {pullback_label} is stale ({(now_sec - pb_time)//3600}h ago)")
        return None

    pullback_price_15m = pullback_item.get("price")
    if pullback_price_15m is None:
        return None

    last_15m_item     = recent_15m[-1] if recent_15m else None
    most_recent_label = last_15m_item.get("label") if isinstance(last_15m_item, dict) else None
    has_continuation  = any(isinstance(s, dict) and s.get("label") == continue_label for s in recent_15m)

    if most_recent_label == pullback_label and has_continuation:
        pullback_score = 20
    elif most_recent_label == pullback_label or has_continuation:
        pullback_score = 10
    else:
        pullback_score = 0

    if pullback_score == 0:
        if debug: print(f"    [S1] skip: no current 15M pullback structure")
        return None

    # ── Step 4: Entry location — tightened distance scoring ──────────────
    # v3: outer limit 13p → 10p; tighter tier boundaries 3/7/10p
    pip                = config.get_symbol_cfg(state.get("symbol"))["pip_size"]
    near_pips          = config.NEAR_LEVEL_PIPS
    dist_from_pullback = abs(price - pullback_price_15m)
    dist_pips          = dist_from_pullback / pip

    if dist_pips > 10:
        if debug: print(f"    [S1] skip: price {dist_pips:.1f}p from pullback (>10p)")
        return None

    if dist_pips <= 3:
        location_score = 15
    elif dist_pips <= 7:
        location_score = 10
    elif dist_pips <= 10:
        location_score = 5
    else:
        location_score = 0

    # ── Step 5: 5M BOS — tightened to 30 min freshness ───────────────────
    # v3: 1 hour → 30 minutes. A BOS older than 30 min is not the start of
    # the impulse — the move has already happened. 30 min keeps entry at
    # the very beginning of the impulse leg.
    bos_5m       = s5m.get("bos", [])
    matching_bos = [
        b for b in bos_5m[-6:]
        if isinstance(b, dict)
        and b.get("direction") == direction
        and (now_sec - b.get("time", now_sec)) <= 30 * 60
    ]

    if not matching_bos:
        if debug: print(f"    [S1] skip: no {direction} BOS on 5M (or all stale >30min)")
        return None

    bos_score = 20 if len(matching_bos) >= 2 else 10

    # ── Step 5b: Counter-BOS rejection ───────────────────────────────────
    # If a counter-direction BOS fired on 5M in the last 30 minutes, the
    # structure is two-sided — directional conviction is weak. Skip.
    counter_dir = "bearish" if direction == "bullish" else "bullish"
    counter_bos = [
        b for b in bos_5m[-6:]
        if isinstance(b, dict)
        and b.get("direction") == counter_dir
        and (now_sec - b.get("time", now_sec)) <= 30 * 60
    ]
    if counter_bos:
        if debug: print(f"    [S1] skip: counter {counter_dir} BOS on 5M within 30min")
        return None

    # ── GATE: Post-BOS structure must hold ───────────────────────────────
    # After the most recent valid BOS fired, the pullback continuation must
    # still be intact. If subsequent 5M candles have already broken back
    # through the first post-BOS close, the setup has failed — skip and wait
    # for a fresh BOS rather than entering a broken continuation.
    recent_bos_sorted = sorted(matching_bos, key=lambda x: x.get("time", 0), reverse=True)
    confirm_time_bos  = recent_bos_sorted[0].get("time", 0) if recent_bos_sorted else 0
    if confirm_time_bos > 0 and candles_5m:
        if not _structure_holds(candles_5m, confirm_time_bos, direction):
            if debug:
                print(f"    [S1] skip: post-BOS structure violated "
                      f"— {direction} continuation already invalidated, waiting for fresh BOS")
            return None

    # ── Step 6: Session timing (5pts) ────────────────────────────────────
    # v4: reduced from 10pts → 5pts to make room for the upgraded zone check.
    # Session timing is a circumstantial filter; zone confluence is a structural
    # quality signal that deserves higher weight.
    sessions       = state.get("sessions", [])
    sessions_lower = [s.lower() for s in sessions]

    if any(s in sessions_lower for s in ["london", "ny", "new york"]):
        session_score = 5
    else:
        session_score = 0

    # ── Step 7: Institutional zone confluence (0 / 5 / 10 pts) ──────────
    # v4 UPGRADE: checks WHERE THE 15M HL ITSELF FORMED, not just where price
    # happens to be right now. When the 15M pullback HL formed at a demand zone
    # or S/R level, that means institutional orders absorbed the selling at that
    # exact structural level — that is one of the highest-quality SMC signals.
    #
    # Scoring:
    #   10pts: 15M HL formed at zone/S&R  AND  current price still in that zone
    #           → pullback found institutional support and we're entering at it
    #   5pts:  15M HL at zone/S&R only (price has since moved slightly above zone)
    #           → level was respected; entry is a pip or two beyond the zone edge
    #   5pts:  current price in zone only (zone present, but HL didn't form there)
    #           → partial confluence; weaker than above
    #   0pts:  no zone confluence at all

    zones_5m  = s5m.get("zones") or []
    zones_15m = s15m.get("zones") or []
    if not isinstance(zones_5m,  list): zones_5m  = []
    if not isinstance(zones_15m, list): zones_15m = []
    all_zones  = zones_5m + zones_15m
    sr_levels  = state.get("sr_levels") or []
    if not isinstance(sr_levels, list): sr_levels = []
    z_thresh   = near_pips * pip          # zone proximity tolerance
    sr_thresh  = near_pips * pip * 2     # S/R levels are approximate — wider tolerance

    def _price_in_zone(p: float) -> bool:
        """True if price p sits inside (or within tolerance of) any zone,
        on the correct side of center for the trade direction."""
        for z in all_zones:
            if not isinstance(z, dict): continue
            top = z.get("top") or 0; bot = z.get("bottom") or 0
            if top == 0 and bot == 0: continue
            center = z.get("center", (top + bot) / 2)
            if not ((bot - z_thresh) <= p <= (top + z_thresh)): continue
            if direction == "bullish" and p <= center + z_thresh: return True
            if direction == "bearish" and p >= center - z_thresh: return True
        return False

    def _price_at_sr(p: float) -> bool:
        """True if price p is within sr_thresh of a matching S/R level."""
        sr_kind = "support" if direction == "bullish" else "resistance"
        for lvl in sr_levels:
            if not isinstance(lvl, dict): continue
            if lvl.get("kind") != sr_kind: continue
            if abs(lvl.get("price", 0) - p) <= sr_thresh: return True
        return False

    hl_at_institutional = _price_in_zone(pullback_price_15m) or _price_at_sr(pullback_price_15m)
    price_in_zone_now   = _price_in_zone(price)

    if hl_at_institutional and price_in_zone_now:
        zone_score = 10   # best: HL formed at zone, still inside it
        zone_tag   = "HL+price@zone"
    elif hl_at_institutional:
        zone_score = 5    # HL at institutional level, price moved slightly out
        zone_tag   = "HL@zone"
    elif price_in_zone_now:
        zone_score = 5    # price in zone but HL didn't form at institutional level
        zone_tag   = "price@zone"
    else:
        zone_score = 0
        zone_tag   = "✗"

    # ── Step 8: 1H structure alignment bonus (0 / 5 pts) ─────────────────
    # v5 BONUS: checks if the 15M pullback HL formed at the same price as a
    # confirmed 1H HL (bullish) or 1H LH (bearish). When two timeframes agree
    # on the same structural level, institutional participation is confirmed at
    # both scales — one of the strongest SMC confluence signals possible.
    #
    # This is a bonus, NOT a gate. Setups without 1H alignment are still valid.
    # Data already exists in state["1h"]["structure"] — no new API calls needed.
    #
    #   5pts: 15M pullback HL is within 10 pips of a confirmed 1H structural point
    #   0pts: no 1H structure alignment found
    h1_struct_label  = "HL" if direction == "bullish" else "LH"
    h1_struct_pts    = state.get("1h", {}).get("structure", [])
    h1_struct_score  = 0
    h1_struct_tag    = "✗"
    h1_align_thresh  = near_pips * pip          # 10 pips — same as zone proximity

    for pt in h1_struct_pts:
        if not isinstance(pt, dict): continue
        if pt.get("label") != h1_struct_label: continue
        pt_price = pt.get("price")
        if pt_price is None: continue
        if abs(pullback_price_15m - pt_price) <= h1_align_thresh:
            h1_struct_score = 5
            h1_struct_tag   = "HL@1H"
            break

    # ── Step 9: Asia range alignment bonus (0 / 5 pts) ───────────────────
    # v6 BONUS: checks if the 15M pullback HL formed at the Asian session
    # high (bullish) or Asian session low (bearish). When London price
    # breaks the Asia range then pulls back to it, the Asia boundary flips
    # to institutional S/R — the textbook SMC range-breakout setup.
    #
    #   5pts: 15M pullback HL within 10 pips of Asia high (bull) or low (bear)
    #   0pts: no Asia range alignment
    asia_range   = state.get("asia_range") or {}
    asia_high    = asia_range.get("high")
    asia_low     = asia_range.get("low")
    asia_score   = 0
    asia_tag     = "✗"
    asia_thresh  = near_pips * pip

    if direction == "bullish" and isinstance(asia_high, (int, float)):
        if abs(pullback_price_15m - asia_high) <= asia_thresh:
            asia_score = 5
            asia_tag   = "HL@AsiaHi"
    elif direction == "bearish" and isinstance(asia_low, (int, float)):
        if abs(pullback_price_15m - asia_low) <= asia_thresh:
            asia_score = 5
            asia_tag   = "LH@AsiaLo"

    # ── Step 10: 15M HL recency bonus (0 / 3 / 5 pts) ────────────────────
    # v7 BONUS: rewards how fresh the 15M pullback HL is. Within the 24h cap,
    # a HL that formed recently means the pullback is still "active" —
    # institutional absorption is recent and the impulse is imminent.
    # A HL from 20 hours ago is structurally valid but less predictive.
    #
    # Note: the 5M BOS 30-min freshness filter already ensures the MARKET is
    # acting on this level right now. Recency of the 15M HL adds a second
    # quality dimension — how long ago did the pullback itself complete?
    #
    # Uses pb_time already computed in Step 3 — zero extra data needed.
    #
    #   5pts: HL formed < 4 hours ago   — pullback just completed, impulse imminent
    #   3pts: HL formed 4–12 hours ago  — recent, still within today's structure
    #   0pts: HL formed 12–24 hours ago — stale (valid within cap, diminished quality)
    pb_age_hours = (now_sec - pb_time) / 3600 if pb_time else 9999

    if pb_age_hours < 4:
        recency_score = 5
        recency_tag   = "fresh(<4h)"
    elif pb_age_hours < 12:
        recency_score = 3
        recency_tag   = "recent(<12h)"
    else:
        recency_score = 0
        recency_tag   = "stale(>12h)"

    # ── Total score ───────────────────────────────────────────────────────
    # Normal max: bias(30) + pullback(20) + bos(20) + loc(15) + sess(5) + zone(10) = 100
    # Best-case:  +5 (1H struct) +5 (Asia range) +5 (recency) = 115 theoretical max
    total_score = bias_score + pullback_score + bos_score + location_score + session_score + zone_score + h1_struct_score + asia_score + recency_score

    # ── Displacement upgrade (must run BEFORE threshold gate) ─────────────
    # If only one BOS, check for a strong displacement candle (body ≥ 70%).
    # A single BOS with clear displacement is upgraded to equal a double BOS.
    # This must happen before the MIN_CONFIDENCE check so the upgraded score
    # is what gets evaluated — not the lower pre-upgrade score.
    if len(matching_bos) < 2:
        is_displacement = False
        for c in reversed([c for c in candles_5m[-6:] if c.get("time", 0) >= confirm_time_bos]):
            o_ = c.get("open", 0); h_ = c.get("high", 0)
            l_ = c.get("low",  0); cl_= c.get("close",0)
            if (cl_ > o_) if direction == "bullish" else (cl_ < o_):
                rng = h_ - l_; body = abs(cl_ - o_)
                if rng > 0 and (body / rng) >= 0.70:
                    is_displacement = True; break
        if not is_displacement:
            if debug: print("    [S1] REJECTED: weak BOS — 1 BOS, no displacement candle")  # Fix B5
            return None
        bos_score = 20  # single strong BOS upgraded to match TradeTeller scoring
        total_score = bias_score + pullback_score + bos_score + location_score + session_score + zone_score + h1_struct_score + asia_score + recency_score

    if debug:
        print(f"    [S1] {direction} | bias={bias_score} pb={pullback_score} bos={bos_score} "
              f"loc={location_score} sess={session_score} zone={zone_score}({zone_tag}) "
              f"1H={h1_struct_score}({h1_struct_tag}) asia={asia_score}({asia_tag}) "
              f"rec={recency_score}({recency_tag}) → {total_score}")

    if total_score < max(85, config.MIN_CONFIDENCE):
        if debug: print(f"    [S1] skip: score {total_score} < {max(85, config.MIN_CONFIDENCE)}")
        return None

    # ── SL / TP ───────────────────────────────────────────────────────────
    buf            = config.SL_BUFFER_PIPS * pip
    struct_5m      = s5m.get("structure", [])
    struct_15m_pts = s15m.get("structure", [])
    _z5  = s5m.get("zones")  or []
    _z15 = s15m.get("zones") or []
    zones_all      = (list(_z5) if isinstance(_z5, list) else []) + (list(_z15) if isinstance(_z15, list) else [])
    sr_all         = state.get("sr_levels") or []
    if not isinstance(sr_all, list): sr_all = []
    align_thresh   = near_pips * pip * 2
    zone_thresh    = near_pips * pip

    def _is_strong_5m_level(lvl: float, kind: str) -> bool:
        lvl_15m = (_last_label(struct_15m_pts, ("HL", "EQL") if kind == "support" else ("LH", "EQH")) or {}).get("price")
        if lvl_15m is not None and abs(lvl - lvl_15m) <= align_thresh:
            return True
        for zone in zones_all:
            t = zone.get("top", 0); b = zone.get("bottom", 0)
            if t == 0 and b == 0: continue
            if (b - zone_thresh) <= lvl <= (t + zone_thresh): return True
        sr_kind = "support" if kind == "support" else "resistance"
        for lvl_sr in sr_all:
            if lvl_sr.get("kind") == sr_kind and abs(lvl_sr.get("price", 0) - lvl) <= zone_thresh:
                return True
        return False

    sl_source = "5M"

    if direction == "bullish":
        sl_5m_lvl  = (_last_label(struct_5m,      ("HL", "EQL")) or {}).get("price")
        sl_15m_lvl = (_last_label(struct_15m_pts, ("HL", "EQL")) or {}).get("price")
        if sl_5m_lvl is not None and _is_strong_5m_level(sl_5m_lvl, "support"):
            sl_anchor = sl_5m_lvl
        elif sl_15m_lvl is not None:
            sl_anchor = sl_15m_lvl; sl_source = "15M"
        else:
            if debug: print("    [S1] skip: no structural SL anchor")
            return None
        sl = sl_anchor - buf
        if sl >= price:
            if debug: print("    [S1] skip: SL not below entry for BUY")
            return None
    else:
        sl_5m_lvl  = (_last_label(struct_5m,      ("LH", "EQH")) or {}).get("price")
        sl_15m_lvl = (_last_label(struct_15m_pts, ("LH", "EQH")) or {}).get("price")
        if sl_5m_lvl is not None and _is_strong_5m_level(sl_5m_lvl, "resistance"):
            sl_anchor = sl_5m_lvl
        elif sl_15m_lvl is not None:
            sl_anchor = sl_15m_lvl; sl_source = "15M"
        else:
            if debug: print("    [S1] skip: no structural SL anchor")
            return None
        sl = sl_anchor + buf
        if sl <= price:
            if debug: print("    [S1] skip: SL not above entry for SELL")
            return None

    sl_dist = abs(price - sl)
    # FIX B-FIB-TP: use scalp_extension_tp() which caps the fib TP at SCALP_MAX_RR.
    # Raw fib_extension_tp() uses D1/4H swing ranges (400-2000 pips for USDJPY),
    # making TP unreachable for a scalp with a 15-40 pip SL (actual RR 10-40:1).
    # Cap at 3× SL; fall back to TARGET_RR × SL when fib is too far.
    _fib = config.scalp_extension_tp(state, direction, price, sl_dist)
    tp   = _fib if _fib is not None else (
               (price + sl_dist * config.TARGET_RR) if direction == "bullish" else (price - sl_dist * config.TARGET_RR))
    rr      = round(abs(tp - price) / sl_dist, 2) if sl_dist > 0 else config.TARGET_RR
    sl      = round(sl, 5)
    tp      = round(tp, 5)

    total_cost_pips = config.get_total_cost_pips(state.get("symbol"))
    spread_pips     = config.get_spread_pips(state.get("symbol"))   # kept for logging
    cost_amount     = total_cost_pips * pip
    net_tp_dist     = max(abs(tp - price) - cost_amount, 0.0)
    net_sl_dist     = sl_dist + cost_amount
    net_rr          = round(net_tp_dist / net_sl_dist, 2) if net_sl_dist > 0 else 0

    # ── Post filters ──────────────────────────────────────────────────────

    if sl_dist < config.MIN_SL_PIPS * pip:
        if debug: print(f"    [S1] REJECTED: SL too tight ({sl_dist/pip:.1f}p < {config.MIN_SL_PIPS})")  # Fix B5
        return None

    if abs(tp - price) / sl_dist < 2.0:
        if debug: print(f"    [S1] REJECTED: raw RR < 2.0")  # Fix B5
        return None

    if net_rr < config.NET_MIN_RR:
        if debug: print(f"    [S1] REJECTED: net RR {net_rr} < {config.NET_MIN_RR}")  # Fix B5
        return None

    pb_age_h = round(pb_age_hours, 1)
    pb_qual  = "clean" if pullback_score == 20 else "weak"
    b15m_tag = (
        "pullback" if (direction == "bullish" and bear_15m) or (direction == "bearish" and bull_15m)
        else "cont"    if (direction == "bullish" and bull_15m) or (direction == "bearish" and bear_15m)
        else "neutral"
    )
    reason   = (
        f"4H={b4h} 1H={b1h} 15M={b15m}({b15m_tag}) | "
        f"15M {pullback_label}={pb_qual} age={pb_age_h}h dist={dist_pips:.1f}p | "
        f"5M BOS {len(matching_bos)}× ✓held | loc={location_score}pts | "
        f"sess={sessions} zone={zone_score}pts({zone_tag}) 1H={h1_struct_score}pts({h1_struct_tag}) "
        f"asia={asia_score}pts({asia_tag}) rec={recency_score}pts({recency_tag}) SL={sl_source} | "
        f"spread={spread_pips}pip netRR={net_rr} score={total_score}"
    )

    return {
        "trade":       True,
        "type":        trade_type,
        "confidence":  total_score,
        "strategy":    "MTF Pullback Precision Scalping",
        "reason":      reason,
        "entry":       price,
        "sl":          sl,
        "tp":          tp,
        "rr":          rr,
        "net_rr":      net_rr,
        "spread_pips": spread_pips,
        "total_cost_pips":  total_cost_pips,
    }
