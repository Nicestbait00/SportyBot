"""
Ticket Engine — pure scoring, pool building, and ticket generation logic.

No Telegram imports. No context objects. All functions take data in and return data out.
This module can be called from Telegram bot handlers, OpenClaw agent tools, or a web API.
"""

from __future__ import annotations

import random

from core.config import DEFAULT_ENABLED_MARKETS


# ── Pick helpers ──────────────────────────────────────────────────────────────

def confidence_verdict(conf: int) -> str:
    """Map confidence score to verdict label."""
    if conf >= 75:
        return "strong"
    elif conf >= 60:
        return "moderate"
    return "weak"


def pick_key(p: dict) -> str:
    """Stable content-based key for a pick (immune to list reordering)."""
    return f"{p.get('home','')}|{p.get('away','')}|{p.get('market','')}|{p.get('pick','')}"


def pick_threshold(p: dict) -> float | None:
    """Extract the Over/Under threshold from a pick string like 'Over (total=1.5)'."""
    pick_str = p.get("pick", "")
    if "total=" in pick_str:
        try:
            return float(pick_str.split("total=")[1].rstrip(")"))
        except (ValueError, IndexError):
            pass
    return None


def match_key(p: dict) -> str:
    """Stable match identifier for scored picks."""
    return f"{p.get('home', '')}_{p.get('away', '')}"


def ticket_totals(picks: list[dict]) -> tuple[float, int]:
    """Return total odds and average confidence for a ticket."""
    total_odds = 1.0
    total_conf = 0
    for pick in picks:
        total_odds *= float(pick.get("odds", 1.0) or 1.0)
        total_conf += int(pick.get("data_confidence", pick.get("confidence", 50)) or 50)
    avg_conf = int(total_conf / max(len(picks), 1))
    return round(total_odds, 2), avg_conf


# ── Pick qualification ────────────────────────────────────────────────────────

def is_safe_team_total_pick(p: dict) -> bool:
    """Return True for low-line team total markets we want to surface more often."""
    market = p.get("market", "")
    threshold = pick_threshold(p)
    return market in {"Home Over/Under", "Away Over/Under"} and threshold in (0.5, 1.5)


def is_thin_data_safe_pick(p: dict) -> bool:
    """Identifies limited-data picks allowed only as fallback."""
    market = p.get("market", "")
    threshold = pick_threshold(p)
    if market in {"Home Over/Under", "Away Over/Under"} and threshold == 0.5:
        return True
    if market == "Over/Under" and threshold in (0.5, 1.5):
        return True
    if market == "Double Chance":
        pick_str = p.get("pick", "")
        if pick_str in {"1X", "X2"}:
            return True
    if market.endswith("Or Over") or market.endswith("Or Under") or market.endswith("Or GG"):
        return True
    return False


def pick_selection_score(p: dict) -> float:
    """Score a pick for combo ordering, with a bias toward safer goal markets."""
    conf = float(p.get("data_confidence", p.get("confidence", 50)) or 50)
    odds = float(p.get("odds", 1.0) or 1.0)
    market = p.get("market", "")
    threshold = pick_threshold(p)
    data_quality = p.get("data_quality", "unknown")

    bonus = 0.0
    if market in {"Home Over/Under", "Away Over/Under"}:
        if threshold == 0.5:
            bonus += 14.0
        elif threshold == 1.5:
            bonus += 8.0
    elif market == "Over/Under":
        if threshold == 0.5:
            bonus += 7.0
        elif threshold == 1.5:
            bonus += 4.0
    elif market == "Double Chance":
        bonus += 4.5
    elif market == "Draw No Bet":
        bonus += 2.0
    elif market.endswith("Or Over") or market.endswith("Or Under") or market.endswith("Or GG"):
        bonus += 10.0

    if p.get("rating") == "safe":
        bonus += 1.5
    if data_quality == "limited":
        bonus -= 8.0
        if is_thin_data_safe_pick(p):
            bonus += 4.0
    elif data_quality == "fair":
        bonus -= 3.0

    odds_penalty = max(0.0, odds - 1.8) * 6.0
    return conf + bonus - odds_penalty


def pick_qualifies_for_combo(p: dict, min_confidence: int, min_pick_odds: float) -> bool:
    """Apply combo thresholds, with a small allowance for very safe team totals."""
    odds = float(p.get("odds", 0) or 0)
    data_quality = p.get("data_quality")
    if odds < min_pick_odds:
        return False

    conf = int(p.get("data_confidence", p.get("confidence", 50)) or 50)
    if data_quality == "limited":
        if not is_thin_data_safe_pick(p):
            return False
        market = p.get("market", "")
        threshold = pick_threshold(p)
        if market in {"Home Over/Under", "Away Over/Under"} and threshold == 0.5 and odds <= 1.45:
            return conf >= max(48, min_confidence - 18)
        if market == "Over/Under" and threshold == 0.5 and odds <= 1.30:
            return conf >= max(50, min_confidence - 16)
        if market == "Over/Under" and threshold == 1.5 and odds <= 1.55:
            return conf >= max(52, min_confidence - 14)
        if market == "Double Chance" and odds <= 1.38:
            return conf >= max(50, min_confidence - 15)
        if (market.endswith("Or Over") or market.endswith("Or Under") or market.endswith("Or GG")) and odds <= 1.50:
            return conf >= max(50, min_confidence - 18)
        return False

    if conf >= min_confidence:
        return True

    if not is_safe_team_total_pick(p):
        return False

    threshold = pick_threshold(p)
    if threshold == 0.5 and odds <= 1.55:
        return conf >= max(55, min_confidence - 7)
    if threshold == 1.5 and odds <= 1.80:
        return conf >= max(60, min_confidence - 5)
    return False


# ── Ticket construction ───────────────────────────────────────────────────────

def market_cap(pick: dict) -> int:
    """Maximum number of picks allowed from this market type in a single ticket."""
    market = pick.get("market", "")
    threshold = pick_threshold(pick)
    if market in {"Home Over/Under", "Away Over/Under"} and threshold == 0.5:
        return 3
    if market in {"Home Over/Under", "Away Over/Under", "Over/Under"}:
        return 2
    if market in {"Double Chance", "Draw No Bet"}:
        return 2
    if market.endswith("Or Over") or market.endswith("Or Under") or market.endswith("Or GG"):
        return 2
    return 1


def make_ticket_entry(
    ticket_id: int,
    mode: str,
    target_odds: float,
    picks: list[dict],
    fallback_notes: list[str] | None = None,
    reused_fixtures: bool = False,
    excluded_pick_keys: set[str] | None = None,
) -> dict:
    """Build the runtime ticket object stored in pick_bundle."""
    total_odds, avg_conf = ticket_totals(picks)
    notes = list(fallback_notes or [])
    return {
        "id": ticket_id,
        "mode": mode,
        "target_odds": target_odds,
        "picks": [dict(p) for p in picks],
        "total_odds": total_odds,
        "avg_confidence": avg_conf,
        "fallback_applied": bool(notes),
        "fallback_notes": notes,
        "reused_fixtures": reused_fixtures,
        "_excluded_pick_keys": set(excluded_pick_keys or set()),
    }


# ── Fallback profiles ────────────────────────────────────────────────────────

def build_fallback_profiles(pick_cfg: dict) -> list[dict]:
    """Build ordered soft-fallback profiles for bundle generation."""
    base_conf = int(pick_cfg.get("min_confidence", 75))
    base_odds = float(pick_cfg.get("min_odds", 1.05))
    base_markets = list(dict.fromkeys(pick_cfg.get("preferred_markets", DEFAULT_ENABLED_MARKETS)))
    conf_floor = max(55, base_conf - 15)
    safe_market_order = ["Home Over/Under", "Away Over/Under", "Double Chance", "Draw No Bet", "Over/Under"]

    profiles: list[dict] = []
    seen: set[tuple] = set()

    def add_profile(min_conf: int, min_odds: float, markets: list[str], notes: list[str]):
        allow_limited_safe = any("thin-data" in note.lower() for note in notes)
        key = (min_conf, round(min_odds, 2), tuple(markets), allow_limited_safe)
        if key in seen:
            return
        seen.add(key)
        profiles.append({
            "min_confidence": min_conf,
            "min_odds": round(min_odds, 2),
            "preferred_markets": list(markets),
            "notes": list(notes),
            "allow_limited_safe": allow_limited_safe,
        })

    add_profile(base_conf, base_odds, base_markets, [])

    current_conf = base_conf
    while current_conf > conf_floor:
        current_conf = max(conf_floor, current_conf - 5)
        add_profile(
            current_conf,
            base_odds,
            base_markets,
            [f"Min confidence lowered to {current_conf}%"],
        )

    current_odds = base_odds
    current_conf = max(conf_floor, current_conf)
    while current_odds > 1.05 + 1e-9:
        current_odds = max(1.05, round(current_odds - 0.05, 2))
        add_profile(
            current_conf,
            current_odds,
            base_markets,
            [
                f"Min confidence lowered to {current_conf}%",
                f"Min odds lowered to {current_odds:.2f}",
            ],
        )

    expanded = list(base_markets)
    expansion_notes = [
        f"Min confidence lowered to {current_conf}%",
        f"Min odds lowered to {current_odds:.2f}",
    ]
    for mkt in safe_market_order:
        if mkt in expanded:
            continue
        expanded.append(mkt)
        add_profile(
            current_conf,
            current_odds,
            expanded,
            expansion_notes + [f"Added market: {mkt}"],
        )

    add_profile(
        current_conf,
        current_odds,
        expanded,
        expansion_notes + ["Enabled thin-data fallback for safe low-line markets"],
    )

    return profiles


# ── Pool building ─────────────────────────────────────────────────────────────

def build_qualified_pool(
    all_scored: list[dict],
    excluded: set[str],
    pick_cfg: dict,
    market_slots: list[dict] | None = None,
    shuffle_seed: int = 0,
) -> list[dict]:
    """Filter and sort the scored pool using an effective config."""
    allow_limited_safe = bool(pick_cfg.get("allow_limited_safe"))
    available = [
        p for p in all_scored
        if pick_key(p) not in excluded
        and (
            p.get("data_quality") != "limited"
            or (allow_limited_safe and is_thin_data_safe_pick(p))
        )
    ]

    preferred_markets = pick_cfg.get("preferred_markets", DEFAULT_ENABLED_MARKETS)
    min_confidence = pick_cfg.get("min_confidence", 75)
    min_pick_odds = pick_cfg.get("min_odds", 1.05)

    if not market_slots:
        available = [p for p in available if p.get("market", "") in preferred_markets]

    if shuffle_seed > 0:
        rng = random.Random(shuffle_seed)
        available.sort(
            key=lambda p: pick_selection_score(p) + rng.uniform(-4.0, 4.0),
            reverse=True,
        )
    else:
        available.sort(key=pick_selection_score, reverse=True)

    if market_slots:
        return [
            p for p in available
            if p.get("odds", 0) > 1.0
            and (
                p.get("data_quality") != "limited"
                or (allow_limited_safe and is_thin_data_safe_pick(p))
            )
        ]

    return [
        p for p in available
        if pick_qualifies_for_combo(p, min_confidence, min_pick_odds)
    ]


# ── Ticket selectors ─────────────────────────────────────────────────────────

def select_ticket_from_pool(
    qualified: list[dict],
    target: float,
    market_slots: list[dict] | None = None,
    disallowed_match_keys: set[str] | None = None,
    disallowed_pick_keys: set[str] | None = None,
) -> list[dict]:
    """Build one ticket from a qualified pool using the greedy selector.

    disallowed_match_keys: match keys (fixtures) to skip entirely.
    disallowed_pick_keys: specific pick keys to skip (allows same fixture, different market).
    """
    disallowed_match_keys = disallowed_match_keys or set()
    disallowed_pick_keys = disallowed_pick_keys or set()

    selected: list[dict] = []
    used_matches: set[str] = set()
    current_odds = 1.0

    # Overshoot target slightly so the final ticket actually meets/exceeds
    # the user's requested odds. Without this, the greedy selector often
    # stops one pick short because the last pick doesn't quite reach target.
    # Cap the overshoot so high targets (50+) don't require too many extra picks.
    overshoot = min(target * 0.10, 5.0)
    effective_target = target + overshoot

    if market_slots:
        for slot in market_slots:
            if slot.get("fill"):
                continue
            slot_market = slot["market"]
            slot_count = slot.get("count", 1)
            slot_threshold = slot.get("threshold")

            slot_picks = [
                p for p in qualified
                if p["market"] == slot_market
                and match_key(p) not in used_matches
                and match_key(p) not in disallowed_match_keys
                and pick_key(p) not in disallowed_pick_keys
            ]
            if slot_market == "Over/Under" and slot_threshold is not None:
                slot_picks = [p for p in slot_picks if pick_threshold(p) == slot_threshold]

            slot_picks.sort(key=pick_selection_score, reverse=True)
            for pick in slot_picks[:slot_count]:
                mk = match_key(pick)
                selected.append(pick)
                used_matches.add(mk)
                current_odds *= pick["odds"]

        fill_slots = [slot for slot in market_slots if slot.get("fill")]
        if fill_slots and current_odds < target:
            fill_slot = fill_slots[0]
            fill_picks = [
                p for p in qualified
                if p["market"] == fill_slot["market"]
                and match_key(p) not in used_matches
                and match_key(p) not in disallowed_match_keys
                and pick_key(p) not in disallowed_pick_keys
            ]
            if fill_slot["market"] == "Over/Under" and fill_slot.get("threshold") is not None:
                fill_picks = [p for p in fill_picks if pick_threshold(p) == fill_slot["threshold"]]
            fill_picks.sort(key=pick_selection_score, reverse=True)
            for pick in fill_picks:
                if current_odds >= effective_target:
                    break
                mk = match_key(pick)
                selected.append(pick)
                used_matches.add(mk)
                current_odds *= pick["odds"]

    league_counts: dict[str, int] = {}
    max_per_league = max(3, len(qualified) // 5) if qualified else 3
    market_counts: dict[str, int] = {}
    deferred_for_diversity: list[dict] = []

    for pick in qualified:
        if current_odds >= effective_target:
            break
        mk = match_key(pick)
        if mk in used_matches or mk in disallowed_match_keys:
            continue
        if pick_key(pick) in disallowed_pick_keys:
            continue
        league = pick.get("league", "")
        if league_counts.get(league, 0) >= max_per_league:
            continue
        mkt = pick.get("market", "")
        if market_counts.get(mkt, 0) >= market_cap(pick):
            deferred_for_diversity.append(pick)
            continue
        selected.append(pick)
        used_matches.add(mk)
        current_odds *= pick["odds"]
        league_counts[league] = league_counts.get(league, 0) + 1
        market_counts[mkt] = market_counts.get(mkt, 0) + 1

    if current_odds < effective_target:
        for pick in deferred_for_diversity:
            if current_odds >= effective_target:
                break
            mk = match_key(pick)
            if mk in used_matches or mk in disallowed_match_keys:
                continue
            if pick_key(pick) in disallowed_pick_keys:
                continue
            league = pick.get("league", "")
            if league_counts.get(league, 0) >= max_per_league + 1:
                continue
            selected.append(pick)
            used_matches.add(mk)
            current_odds *= pick["odds"]
            league_counts[league] = league_counts.get(league, 0) + 1
            mkt = pick.get("market", "")
            market_counts[mkt] = market_counts.get(mkt, 0) + 1

    return selected


def select_unique_ticket_from_pool(
    qualified: list[dict],
    reference_picks: list[dict],
    target: float,
    forbidden_pick_keys: set[str],
) -> list[dict]:
    """Build a unique ticket on the same fixtures using different markets."""
    forced_match_keys = [match_key(p) for p in reference_picks]
    ref_by_match = {match_key(p): p for p in reference_picks}
    by_match: dict[str, list[dict]] = {}

    for pick in qualified:
        mk = match_key(pick)
        if mk not in ref_by_match or pick_key(pick) in forbidden_pick_keys:
            continue
        by_match.setdefault(mk, []).append(pick)

    selected: list[dict] = []
    candidate_lists: dict[str, list[dict]] = {}
    for mk in forced_match_keys:
        ref_pick = ref_by_match[mk]

        def _unique_score(p, ref=ref_pick):
            return pick_selection_score(p) - abs(float(p.get("odds", 1.0)) - float(ref.get("odds", 1.0))) * 15

        candidates = sorted(
            by_match.get(mk, []),
            key=_unique_score,
            reverse=True,
        )
        if not candidates:
            continue
        selected.append(candidates[0])
        candidate_lists[mk] = candidates

    if not selected:
        return []

    overshoot = min(target * 0.10, 5.0)
    effective_target = target + overshoot
    current_odds, _ = ticket_totals(selected)
    if current_odds >= effective_target:
        return selected

    while current_odds < effective_target:
        best_upgrade = None
        for idx, current_pick in enumerate(selected):
            mk = match_key(current_pick)
            candidates = candidate_lists.get(mk, [])
            current_pk = pick_key(current_pick)
            try:
                current_idx = next(i for i, cand in enumerate(candidates) if pick_key(cand) == current_pk)
            except StopIteration:
                continue
            for alt in candidates[current_idx + 1:]:
                if float(alt.get("odds", 1.0)) <= float(current_pick.get("odds", 1.0)):
                    continue
                new_total = current_odds / max(float(current_pick.get("odds", 1.0)), 1.0) * float(alt.get("odds", 1.0))
                score_drop = pick_selection_score(current_pick) - pick_selection_score(alt)
                candidate_rank = (
                    0 if new_total >= effective_target else 1,
                    abs(effective_target - new_total),
                    score_drop,
                )
                if best_upgrade is None or candidate_rank < best_upgrade["rank"]:
                    best_upgrade = {"index": idx, "pick": alt, "new_total": new_total, "rank": candidate_rank}

        if best_upgrade is None:
            break

        selected[best_upgrade["index"]] = best_upgrade["pick"]
        current_odds = best_upgrade["new_total"]

    return selected


# ── Bundle generators ─────────────────────────────────────────────────────────

def generate_dynamic_bundle(
    all_scored: list[dict],
    ticket_count: int,
    target: float | list[float],
    pick_cfg: dict,
    excluded: set[str],
    market_slots: list[dict] | None = None,
    shuffle_seed: int = 0,
) -> tuple[list[dict], dict | None, bool]:
    """Build a dynamic multi-ticket bundle with soft fallback.

    target can be a single float (same for all) or a list of per-ticket targets.
    """
    targets = target if isinstance(target, list) else [target] * ticket_count
    best_bundle: list[dict] = []
    best_profile = None
    profiles = build_fallback_profiles(pick_cfg)

    for profile in profiles:
        bundle: list[dict] = []
        used_match_keys: set[str] = set()
        qualified = build_qualified_pool(all_scored, excluded, profile, market_slots, shuffle_seed)
        for ticket_id in range(1, ticket_count + 1):
            t = targets[ticket_id - 1] if ticket_id - 1 < len(targets) else targets[-1]
            picks = select_ticket_from_pool(
                qualified, t, market_slots, used_match_keys,
            )
            if not picks:
                break
            bundle.append(make_ticket_entry(ticket_id, "dynamic", t, picks, profile.get("notes", [])))
            used_match_keys.update({match_key(p) for p in picks})
        if len(bundle) > len(best_bundle):
            best_bundle = bundle
            best_profile = profile
        if len(bundle) == ticket_count:
            return bundle, profile, False

    if not best_bundle or best_profile is None:
        return best_bundle, best_profile, False

    # Reuse fallback: when pool is exhausted, reuse fixtures but with different markets
    reuse_bundle = [
        make_ticket_entry(
            ticket["id"],
            ticket["mode"],
            ticket["target_odds"],
            ticket["picks"],
            ticket["fallback_notes"],
            reused_fixtures=ticket.get("reused_fixtures", False),
            excluded_pick_keys=ticket.get("_excluded_pick_keys", set()),
        )
        for ticket in best_bundle
    ]
    used_pick_keys: set[str] = set()
    for ticket in best_bundle:
        used_pick_keys.update({pick_key(p) for p in ticket["picks"]})
    qualified = build_qualified_pool(all_scored, excluded, best_profile, market_slots, shuffle_seed)
    for ticket_id in range(len(reuse_bundle) + 1, ticket_count + 1):
        t = targets[ticket_id - 1] if ticket_id - 1 < len(targets) else targets[-1]
        picks = select_ticket_from_pool(
            qualified, t, market_slots,
            disallowed_match_keys=set(),
            disallowed_pick_keys=used_pick_keys,
        )
        if not picks:
            picks = select_ticket_from_pool(qualified, t, market_slots, set())
        if not picks:
            break
        notes = list(best_profile.get("notes", [])) + ["Reused fixtures after pool exhaustion"]
        reuse_bundle.append(make_ticket_entry(ticket_id, "dynamic", t, picks, notes, reused_fixtures=True))
        used_pick_keys.update({pick_key(p) for p in picks})

    if len(reuse_bundle) > len(best_bundle):
        return reuse_bundle, best_profile, True
    return best_bundle, best_profile, False


def generate_unique_bundle(
    all_scored: list[dict],
    ticket_count: int,
    target: float,
    pick_cfg: dict,
    excluded: set[str],
    shuffle_seed: int = 0,
) -> tuple[list[dict], dict | None]:
    """Build a unique-ticket bundle that keeps the same fixtures across tickets."""
    best_bundle: list[dict] = []
    best_profile = None
    profiles = build_fallback_profiles(pick_cfg)

    for profile in profiles:
        qualified = build_qualified_pool(all_scored, excluded, profile, None, shuffle_seed)
        base_picks = select_ticket_from_pool(qualified, target, None, set())
        if not base_picks:
            continue

        bundle = [make_ticket_entry(1, "unique", target, base_picks, profile.get("notes", []))]
        used_pick_keys = {pick_key(p) for p in base_picks}

        for ticket_id in range(2, ticket_count + 1):
            picks = select_unique_ticket_from_pool(
                qualified, base_picks, target, used_pick_keys,
            )
            if not picks:
                break
            bundle.append(make_ticket_entry(ticket_id, "unique", target, picks, profile.get("notes", [])))
            used_pick_keys.update({pick_key(p) for p in picks})

        if len(bundle) > len(best_bundle):
            best_bundle = bundle
            best_profile = profile
        if len(bundle) == ticket_count:
            return bundle, profile

    return best_bundle, best_profile
