"""
Ticket Splitter — partition a large ticket into smaller balanced tickets.

Algorithm:
  1. Sort all picks safest → riskiest (ascending odds).
  2. Sort target odds ascending — fill smallest (safest) first.
  3. Walk through sorted picks, accumulating log-odds.
  4. Cut into tickets at cumulative log-odds thresholds.
  5. Seed each non-safest ticket with duplicated safe "insurance" picks.

No Telegram imports. Pure splitting logic.
"""

from __future__ import annotations

import math
import logging
from copy import deepcopy

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
    if len(picks) < len(targets):
        return (
            f"Not enough picks ({len(picks)}) to split into {len(targets)} tickets. "
            f"Each ticket needs at least 1 pick."
        )
    if any(t <= 1.0 for t in targets):
        return "All target odds must be greater than 1.0."
    return None


# ── Core splitter ─────────────────────────────────────────────────────────────

def _pick_log_odds(pick: dict) -> float:
    """Safe log-odds for a pick, clamped above 1."""
    try:
        odds = float(pick.get("odds", 1.0))
    except (ValueError, TypeError):
        odds = 1.0
    return math.log(max(odds, 1.001))


def _bin_actual_odds(picks: list[dict]) -> float:
    """Product of odds for a list of picks (via log-sum)."""
    if not picks:
        return 1.0
    return math.exp(sum(_pick_log_odds(p) for p in picks))


def split_ticket(
    picks: list[dict],
    targets: list[float],
    insurance_pct: float = 0.20,
) -> dict:
    """Split picks into multiple tickets targeting specified odds.

    Args:
        picks: List of pick dicts, each must have an "odds" key.
        targets: Target odds per ticket (e.g., [3.0, 8.0, 5.0]).
        insurance_pct: Fraction of safest picks to use as insurance pool.

    Returns:
        {
            "tickets": [{
                "ticket_num": int,          # 1-indexed
                "target_odds": float,
                "actual_odds": float,       # product of base picks only
                "picks": [pick_dict, ...],  # the assigned picks
                "insurance_picks": [...],   # duplicated safe picks (marked)
                "total_odds": float,        # actual_odds × insurance odds
                "pick_count": int,          # base + insurance
            }],
            "total_original_odds": float,
        }
    """
    n_tickets = len(targets)

    # ── 1. Sort picks safest first (lowest odds) ──
    sorted_picks = sorted(picks, key=lambda p: float(p.get("odds", 1.0)))

    # ── 2. Sort targets ascending, track original positions ──
    target_order = sorted(range(n_tickets), key=lambda i: targets[i])
    sorted_targets = [targets[i] for i in target_order]

    # Total available log-odds
    total_log = sum(_pick_log_odds(p) for p in sorted_picks)

    # ── 3. Compute cumulative log thresholds ──
    # Each threshold is where we cut to the next ticket.
    target_log_total = sum(math.log(max(t, 1.001)) for t in sorted_targets)

    # Scale targets proportionally if they exceed available odds
    if target_log_total > 0:
        scale = total_log / target_log_total
    else:
        scale = 1.0

    log_thresholds = []
    cumulative = 0.0
    for t in sorted_targets[:-1]:  # last ticket gets the remainder
        cumulative += math.log(max(t, 1.001)) * scale
        log_thresholds.append(cumulative)

    # ── 4. Walk through picks, cut at thresholds ──
    bins: list[list[dict]] = [[] for _ in range(n_tickets)]
    current_bin = 0
    running_log = 0.0

    for pick in sorted_picks:
        log_odds = _pick_log_odds(pick)
        bins[current_bin].append(pick)
        running_log += log_odds

        # Advance to next bin when we pass the threshold
        if (current_bin < len(log_thresholds)
                and running_log >= log_thresholds[current_bin]):
            current_bin = min(current_bin + 1, n_tickets - 1)

    # ── 5. Ensure every bin has at least 1 pick ──
    for i in range(n_tickets):
        if not bins[i]:
            # Steal from the largest bin
            largest = max(range(n_tickets), key=lambda x: len(bins[x]))
            if len(bins[largest]) > 1:
                # Take the last pick (riskiest in that bin since sorted ascending)
                bins[i].append(bins[largest].pop())

    # ── 6. Map bins back to original target order ──
    result_bins: list[list[dict]] = [[] for _ in range(n_tickets)]
    for sorted_idx, orig_idx in enumerate(target_order):
        result_bins[orig_idx] = bins[sorted_idx]

    # ── 7. Insurance: duplicate safe picks into riskier tickets ──
    n_insurance = max(1, int(len(sorted_picks) * insurance_pct))
    insurance_pool = sorted_picks[:n_insurance]

    safest_ticket_idx = target_order[0]  # ticket with smallest target
    insurance_assignments: list[list[dict]] = [[] for _ in range(n_tickets)]

    if insurance_pool:
        # Determine threshold: max odds in the insurance pool
        ins_threshold = max(float(p.get("odds", 1.0)) for p in insurance_pool)

        for i in range(n_tickets):
            if i == safest_ticket_idx:
                continue  # safest ticket IS the insurance — skip

            # Check if this ticket already has a pick within the safe range
            ticket_event_ids = {p.get("event_id") for p in result_bins[i]}
            has_safe = any(
                float(p.get("odds", 99)) <= ins_threshold
                for p in result_bins[i]
            )

            if not has_safe:
                # Find an insurance pick not already in this ticket (by event_id)
                for ins_pick in insurance_pool:
                    if ins_pick.get("event_id") not in ticket_event_ids:
                        dup = deepcopy(ins_pick)
                        dup["_insurance"] = True
                        insurance_assignments[i].append(dup)
                        break  # one insurance pick per ticket

    # ── 8. Build result ──
    tickets = []
    for i in range(n_tickets):
        base = result_bins[i] or []
        ins = insurance_assignments[i]

        actual = _bin_actual_odds(base)
        total = _bin_actual_odds(base + ins)

        tickets.append({
            "ticket_num": i + 1,
            "target_odds": targets[i],
            "actual_odds": round(actual, 2),
            "picks": base,
            "insurance_picks": ins,
            "total_odds": round(total, 2),
            "pick_count": len(base) + len(ins),
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
        diff = t["total_odds"] - t["target_odds"]
        arrow = "🔺" if diff > 0 else "🔻" if diff < 0 else "✅"
        lines.append(f"━━━ *Ticket {t['ticket_num']}* ━━━")
        lines.append(
            f"🎯 Target: {t['target_odds']:.2f}  →  "
            f"Actual: *{t['total_odds']:.2f}* odds  {arrow}"
        )
        lines.append(f"📊 {t['pick_count']} picks\n")

        for j, pick in enumerate(t["picks"], 1):
            odds = float(pick.get("odds", 0))
            home = pick.get("home", "?")
            away = pick.get("away", "?")
            market = pick.get("market", "?")
            pick_label = pick.get("pick", "?")
            lines.append(f"  {j}. {home} vs {away}")
            lines.append(f"     {market}: {pick_label} @ {odds:.2f}")

        if t["insurance_picks"]:
            lines.append("")
            base_count = len(t["picks"])
            for k, pick in enumerate(t["insurance_picks"], base_count + 1):
                odds = float(pick.get("odds", 0))
                home = pick.get("home", "?")
                away = pick.get("away", "?")
                market = pick.get("market", "?")
                pick_label = pick.get("pick", "?")
                lines.append(f"  🔁 {k}. {home} vs {away}")
                lines.append(f"     {market}: {pick_label} @ {odds:.2f}")
                lines.append(f"     _Shared from safest pool — remove if you prefer_")

        lines.append("")

    return "\n".join(lines)


def format_single_ticket(ticket: dict, ticket_idx: int) -> str:
    """Format a single split ticket for display."""
    t = ticket
    lines = [f"🎫 *Split Ticket {ticket_idx + 1}*"]
    lines.append(f"🎯 Target: {t['target_odds']:.2f}  →  Actual: *{t['total_odds']:.2f}* odds")
    lines.append(f"📊 {t['pick_count']} picks\n")

    for j, pick in enumerate(t["picks"], 1):
        odds = float(pick.get("odds", 0))
        home = pick.get("home", "?")
        away = pick.get("away", "?")
        market = pick.get("market", "?")
        pick_label = pick.get("pick", "?")
        lines.append(f"{j}. {home} vs {away}")
        lines.append(f"   {market}: {pick_label} @ {odds:.2f}")

    if t["insurance_picks"]:
        lines.append("")
        base_count = len(t["picks"])
        for k, pick in enumerate(t["insurance_picks"], base_count + 1):
            odds = float(pick.get("odds", 0))
            home = pick.get("home", "?")
            away = pick.get("away", "?")
            market = pick.get("market", "?")
            pick_label = pick.get("pick", "?")
            lines.append(f"🔁 {k}. {home} vs {away}")
            lines.append(f"   {market}: {pick_label} @ {odds:.2f}")
            lines.append(f"   _Insurance — shared from safest pool_")

    return "\n".join(lines)
