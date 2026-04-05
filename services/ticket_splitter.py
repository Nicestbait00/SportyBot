"""
Ticket Splitter — select subsets from a pick pool to build smaller tickets.

Unique-first: each ticket gets its own picks to avoid correlated risk.
If a shared "safe" pick upsets, all tickets sharing it lose together —
so we only reuse picks when the remaining pool can't reach the target.

Algorithm:
  1. Sort picks safest→riskiest, targets smallest→largest
  2. For each target: greedily select from REMAINING pool only
  3. If remaining pool can't reach target: supplement with safest
     already-used picks (marked shared=True)
  4. Remove uniquely-selected picks from pool after each ticket

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


def _pick_kickoff_key(pick: dict):
    """Sort key for ordering picks by kickoff date. Uses the 'date' string field."""
    return pick.get("date", "") or "9999"


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

def _select_from_pool(pool: list[dict], target: float) -> list[dict]:
    """Greedy selection from pool (must be sorted safest-first).

    Adds picks until product >= target, then swap-tunes the last pick
    for a tighter fit.
    """
    if target <= 1.0 or not pool:
        return []

    log_target = math.log(target)
    selected = []
    running_log = 0.0

    for pick in pool:
        if running_log >= log_target:
            break
        selected.append(pick)
        running_log += _pick_log_odds(pick)

    if not selected:
        return [pool[0]] if pool else []

    # Swap-tune: if overshot, try replacing last pick with a closer fit
    if len(selected) >= 2 and running_log > log_target:
        last = selected[-1]
        last_log = _pick_log_odds(last)
        needed = log_target - (running_log - last_log)

        if needed > 0:
            best_swap, best_diff = last, abs(last_log - needed)
            sel_ids = {id(s) for s in selected[:-1]}
            for p in pool:
                if id(p) in sel_ids or p is last:
                    continue
                diff = abs(_pick_log_odds(p) - needed)
                if diff < best_diff:
                    best_swap, best_diff = p, diff
            if best_swap is not last:
                selected[-1] = best_swap

    return selected


# ── Main split function ───────────────────────────────────────────────────────

def split_ticket(
    picks: list[dict],
    targets: list[float],
) -> dict:
    """Split picks into multiple tickets, unique-first.

    Each ticket draws from its own subset of the pool. Picks are only
    reused across tickets when the remaining pool can't reach the target —
    in that case, the safest already-used picks are borrowed and marked
    shared=True so the user can see (and optionally remove) them.

    Targets are processed smallest-first so small tickets consume fewer
    picks, leaving more for bigger targets.
    """
    total_log = sum(_pick_log_odds(p) for p in picks)
    total_odds = math.exp(total_log)

    # Sort targets ascending but remember original order for output
    indexed_targets = sorted(enumerate(targets), key=lambda x: x[1])

    # Available pool — picks not yet claimed by any ticket
    remaining = sorted(picks, key=lambda p: _pick_odds(p))
    used_picks: list[dict] = []  # picks already assigned (safest first)

    results: list[dict] = [None] * len(targets)  # type: ignore

    for orig_idx, target in indexed_targets:
        capped = min(target, total_odds)
        remaining_max = _product_odds(remaining)

        if remaining_max >= capped * 0.90:
            # ── Unique path: enough odds in remaining pool ──
            selected = _select_from_pool(remaining, capped)
            for p in selected:
                p["shared"] = False
            # Remove selected from remaining
            sel_ids = {id(p) for p in selected}
            remaining = [p for p in remaining if id(p) not in sel_ids]
            used_picks.extend(sorted(selected, key=lambda p: _pick_odds(p)))
        else:
            # ── Fallback: supplement with safest already-used picks ──
            combined = list(remaining)
            # Add already-used picks (safest first) until we have enough
            for up in used_picks:
                if _product_odds(combined) >= capped:
                    break
                if not any(id(c) == id(up) for c in combined):
                    shared_copy = dict(up)
                    shared_copy["shared"] = True
                    combined.append(shared_copy)
            combined.sort(key=lambda p: _pick_odds(p))

            selected = _select_from_pool(combined, capped)

            # Mark which are shared vs unique
            remaining_ids = {id(p) for p in remaining}
            unique_in_sel = []
            for p in selected:
                if id(p) in remaining_ids:
                    p["shared"] = False
                    unique_in_sel.append(p)
                # shared copies already have shared=True from above

            # Remove only uniquely-claimed picks from remaining
            unique_ids = {id(p) for p in unique_in_sel}
            remaining = [p for p in remaining if id(p) not in unique_ids]
            used_picks.extend(sorted(unique_in_sel, key=lambda p: _pick_odds(p)))

        actual = _product_odds(selected)
        shared_count = sum(1 for p in selected if p.get("shared"))

        results[orig_idx] = {
            "ticket_num": orig_idx + 1,
            "target_odds": target,
            "actual_odds": round(actual, 2),
            "picks": selected,
            "pick_count": len(selected),
            "shared_count": shared_count,
            "unique": shared_count == 0,
        }

    return {
        "tickets": results,
        "total_original_odds": round(total_odds, 2),
    }


# ── Formatting ────────────────────────────────────────────────────────────────

def format_split_summary(result: dict) -> str:
    """Format the split result as user-friendly Telegram text."""
    lines = ["🔀 *Ticket Split Summary*"]
    lines.append(
        f"Original ticket: *{result['total_original_odds']:.2f}* total odds\n"
    )

    total_shared = 0
    for t in result["tickets"]:
        diff_pct = ((t["actual_odds"] - t["target_odds"]) / t["target_odds"]) * 100
        if abs(diff_pct) < 5:
            arrow = "✅"
        elif diff_pct > 0:
            arrow = "🔺"
        else:
            arrow = "🔻"

        unique_tag = "  🟢 All unique" if t.get("unique") else ""
        lines.append(f"━━━ *Ticket {t['ticket_num']}* ━━━{unique_tag}")
        lines.append(
            f"🎯 Target: {t['target_odds']:.2f}  →  "
            f"Actual: *{t['actual_odds']:.2f}* odds  {arrow}"
        )
        lines.append(f"📊 {t['pick_count']} picks\n")

        sorted_picks = sorted(t["picks"], key=_pick_kickoff_key)
        for j, pick in enumerate(sorted_picks, 1):
            odds = _pick_odds(pick)
            home = pick.get("home", "?")
            away = pick.get("away", "?")
            market = pick.get("market", "?")
            pick_label = pick.get("pick", "?")
            shared_tag = " 🔁" if pick.get("shared") else ""
            lines.append(f"  {j}. {home} vs {away}{shared_tag}")
            date_str = pick.get("date", "")
            if date_str:
                lines.append(f"     {date_str}")
            lines.append(f"     {market}: {pick_label} @ {odds:.2f}")

        shared_count = t.get("shared_count", 0)
        total_shared += shared_count
        if shared_count:
            lines.append(
                f"\n  ⚠️ {shared_count} shared pick(s) — "
                f"also in other ticket(s). Remove if you prefer independence."
            )

        lines.append("")

    if total_shared == 0:
        lines.append("✅ All tickets are fully independent — no shared picks.")
    else:
        lines.append(
            f"ℹ️ {total_shared} pick(s) shared across tickets (marked 🔁). "
            f"These were needed to reach the target odds."
        )

    return "\n".join(lines)


def format_single_ticket(ticket: dict, ticket_idx: int) -> str:
    """Format a single split ticket for display."""
    t = ticket
    lines = [f"🎫 *Split Ticket {ticket_idx + 1}*"]
    lines.append(f"🎯 Target: {t['target_odds']:.2f}  →  Actual: *{t['actual_odds']:.2f}* odds")
    lines.append(f"📊 {t['pick_count']} picks\n")

    sorted_picks = sorted(t["picks"], key=_pick_kickoff_key)
    for j, pick in enumerate(sorted_picks, 1):
        odds = _pick_odds(pick)
        home = pick.get("home", "?")
        away = pick.get("away", "?")
        market = pick.get("market", "?")
        pick_label = pick.get("pick", "?")
        lines.append(f"{j}. {home} vs {away}")
        date_str = pick.get("date", "")
        if date_str:
            lines.append(f"   {date_str}")
        lines.append(f"   {market}: {pick_label} @ {odds:.2f}")

    return "\n".join(lines)
