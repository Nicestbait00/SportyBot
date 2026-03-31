"""
Ticket Splitter — select subsets from a pick pool to build smaller tickets.

Each target ticket independently selects picks from the full pool.
Picks CAN appear in multiple tickets — each ticket is a separate booking code.
Safety-first: always starts with the safest picks and adds riskier ones only
when needed to reach the target odds.

No Telegram imports. Pure splitting logic.
"""

from __future__ import annotations

import math
import logging

logger = logging.getLogger(__name__)

MAX_SPLITS = 5


# ── Validation ────────────────────────────────────────────────────────────────

def validate_split_request(
    picks: list[dict],
    targets: list[float],
) -> str | None:
    """Validate split parameters. Returns error message or None if valid."""
    if len(targets) < 2:
        return "Need at least 2 target odds to split."
    if len(targets) > MAX_SPLITS:
        return f"Maximum {MAX_SPLITS} splits allowed."
    if not picks or len(picks) < 2:
        return "Need at least 2 picks to split."
    if any(t <= 1.0 for t in targets):
        return "All target odds must be greater than 1.0."

    # Check that the largest target is achievable from the pool
    total_log = sum(_pick_log_odds(p) for p in picks)
    max_target = max(targets)
    if math.log(max(max_target, 1.001)) > total_log * 1.05:
        total_odds = round(math.exp(total_log), 2)
        return (
            f"Target {max_target} odds exceeds the total ticket odds ({total_odds}). "
            f"Each split ticket selects from the same pool, but can't exceed the full ticket."
        )
    return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pick_log_odds(pick: dict) -> float:
    """Safe log-odds for a pick, clamped above 1."""
    try:
        odds = float(pick.get("odds", 1.0))
    except (ValueError, TypeError):
        odds = 1.0
    return math.log(max(odds, 1.001))


def _pick_odds(pick: dict) -> float:
    """Get numeric odds from a pick dict."""
    try:
        return float(pick.get("odds", 1.0))
    except (ValueError, TypeError):
        return 1.0


def _product_odds(picks: list[dict]) -> float:
    """Product of odds for a list of picks (via log-sum to avoid overflow)."""
    if not picks:
        return 1.0
    return math.exp(sum(_pick_log_odds(p) for p in picks))


# ── Core: greedy subset selection ─────────────────────────────────────────────

def _select_for_target(sorted_picks: list[dict], target: float) -> list[dict]:
    """Select the safest subset of picks whose product ≈ target odds.

    sorted_picks must be pre-sorted ascending by odds (safest first).
    Greedy: adds picks from safest until we hit/exceed the target,
    then tries swapping the last pick for a tighter fit.
    """
    if target <= 1.0:
        return []

    log_target = math.log(target)
    selected = []
    running_log = 0.0

    for pick in sorted_picks:
        if running_log >= log_target:
            break
        selected.append(pick)
        running_log += _pick_log_odds(pick)

    if not selected:
        return [sorted_picks[0]] if sorted_picks else []

    # ── Fine-tune: if we overshot, try swapping the last pick ──
    # Check if removing the last pick and trying subsequent ones gets closer
    if len(selected) >= 2 and running_log > log_target:
        overshoot = running_log - log_target
        last = selected[-1]
        last_log = _pick_log_odds(last)
        without_last = running_log - last_log

        # How much do we still need without the last pick?
        needed = log_target - without_last

        if needed > 0:
            # Find the pick in the pool closest to 'needed' log-odds
            best_swap = last
            best_diff = abs(last_log - needed)

            for p in sorted_picks:
                if p is last:
                    continue
                if any(p.get("event_id") == s.get("event_id") for s in selected[:-1]):
                    continue  # already in the selection
                p_log = _pick_log_odds(p)
                diff = abs(p_log - needed)
                if diff < best_diff:
                    best_swap = p
                    best_diff = diff

            if best_swap is not last:
                selected[-1] = best_swap

    return selected


# ── Main split function ───────────────────────────────────────────────────────

def split_ticket(
    picks: list[dict],
    targets: list[float],
) -> dict:
    """Split picks into multiple tickets by selecting subsets for each target.

    Each ticket independently selects from the full pool (picks can repeat
    across tickets). Safety-first: safest picks are selected first.

    Args:
        picks: List of pick dicts, each must have an "odds" key.
        targets: Target odds per ticket (e.g., [10.0, 50.0, 100.0]).

    Returns:
        {
            "tickets": [{
                "ticket_num": int,
                "target_odds": float,
                "actual_odds": float,
                "picks": [pick_dict, ...],
                "pick_count": int,
            }],
            "total_original_odds": float,
        }
    """
    # Sort the full pool safest first
    sorted_pool = sorted(picks, key=lambda p: _pick_odds(p))

    total_log = sum(_pick_log_odds(p) for p in picks)

    tickets = []
    for i, target in enumerate(targets):
        # Cap target at total odds — can't exceed the full ticket
        capped = min(target, math.exp(total_log))
        selected = _select_for_target(sorted_pool, capped)
        actual = _product_odds(selected)

        tickets.append({
            "ticket_num": i + 1,
            "target_odds": target,
            "actual_odds": round(actual, 2),
            "picks": selected,
            "pick_count": len(selected),
        })

    return {
        "tickets": tickets,
        "total_original_odds": round(math.exp(total_log), 2),
    }


# ── Formatting ────────────────────────────────────────────────────────────────

def format_split_summary(result: dict) -> str:
    """Format the split result as user-friendly Telegram text."""
    lines = ["🔀 *Ticket Split Summary*"]
    lines.append(
        f"Original ticket: *{result['total_original_odds']:.2f}* total odds\n"
    )

    for t in result["tickets"]:
        diff_pct = ((t["actual_odds"] - t["target_odds"]) / t["target_odds"]) * 100
        if abs(diff_pct) < 5:
            arrow = "✅"
        elif diff_pct > 0:
            arrow = "🔺"
        else:
            arrow = "🔻"

        lines.append(f"━━━ *Ticket {t['ticket_num']}* ━━━")
        lines.append(
            f"🎯 Target: {t['target_odds']:.2f}  →  "
            f"Actual: *{t['actual_odds']:.2f}* odds  {arrow}"
        )
        lines.append(f"📊 {t['pick_count']} picks\n")

        for j, pick in enumerate(t["picks"], 1):
            odds = _pick_odds(pick)
            home = pick.get("home", "?")
            away = pick.get("away", "?")
            market = pick.get("market", "?")
            pick_label = pick.get("pick", "?")
            lines.append(f"  {j}. {home} vs {away}")
            lines.append(f"     {market}: {pick_label} @ {odds:.2f}")

        lines.append("")

    # Show overlap info
    all_event_ids = []
    for t in result["tickets"]:
        all_event_ids.append({p.get("event_id") for p in t["picks"]})

    if len(all_event_ids) >= 2:
        shared = all_event_ids[0]
        for s in all_event_ids[1:]:
            shared = shared & s
        if shared:
            lines.append(
                f"ℹ️ {len(shared)} pick(s) appear in multiple tickets "
                f"(safest picks selected across tickets)"
            )

    return "\n".join(lines)


def format_single_ticket(ticket: dict, ticket_idx: int) -> str:
    """Format a single split ticket for display."""
    t = ticket
    lines = [f"🎫 *Split Ticket {ticket_idx + 1}*"]
    lines.append(f"🎯 Target: {t['target_odds']:.2f}  →  Actual: *{t['actual_odds']:.2f}* odds")
    lines.append(f"📊 {t['pick_count']} picks\n")

    for j, pick in enumerate(t["picks"], 1):
        odds = _pick_odds(pick)
        home = pick.get("home", "?")
        away = pick.get("away", "?")
        market = pick.get("market", "?")
        pick_label = pick.get("pick", "?")
        lines.append(f"{j}. {home} vs {away}")
        lines.append(f"   {market}: {pick_label} @ {odds:.2f}")

    return "\n".join(lines)
