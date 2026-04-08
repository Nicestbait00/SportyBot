"""Shared utilities for SportyBot handler modules.

Functions that are used across multiple handler modules live here to avoid
circular imports and duplication.
"""

from __future__ import annotations

import logging
import time

from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


# ── Job tracking (used by pick, check, sort) ──

def _utc_now_iso() -> str:
    """Return an ISO UTC timestamp without microseconds."""
    from datetime import datetime
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _mark_job_start(context: ContextTypes.DEFAULT_TYPE, job_name: str) -> tuple[str, float]:
    """Track the beginning of a long-running bot job."""
    state = context.application.bot_data.get("health")
    if not isinstance(state, dict):
        return job_name, time.monotonic()
    state["active_jobs"] = int(state.get("active_jobs", 0)) + 1
    state["last_job_name"] = job_name
    state["last_job_started_at"] = _utc_now_iso()
    return job_name, time.monotonic()


def _mark_job_finish(
    context: ContextTypes.DEFAULT_TYPE,
    token: tuple[str, float],
    *,
    success: bool = True,
    error: Exception | None = None,
) -> None:
    """Track the end of a long-running bot job."""
    state = context.application.bot_data.get("health")
    if not isinstance(state, dict):
        return
    state["active_jobs"] = max(0, int(state.get("active_jobs", 0)) - 1)
    state["last_job_name"] = token[0]
    state["last_job_finished_at"] = _utc_now_iso()
    state["last_job_duration_ms"] = int((time.monotonic() - token[1]) * 1000)
    if success:
        state["last_success_at"] = state["last_job_finished_at"]
    elif error is not None:
        state["last_error_at"] = _utc_now_iso()
        state["last_error"] = f"{type(error).__name__}: {error}"


# ── Send/edit helper (used by pick, check, sort, split) ──

async def _send_or_edit(message, text: str, reply_markup=None, edit: bool = False, parse_mode=None):
    """Reply or edit a Telegram message depending on context."""
    if edit and hasattr(message, "edit_text"):
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        await message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)


# ── Rate limiting (used by multiple handlers) ──

def _check_rate_limit(context: ContextTypes.DEFAULT_TYPE, command: str, cooldown: int = 10) -> str | None:
    """Return a user-facing message if rate-limited, else None (allowed)."""
    ts_key = f"_rl_{command}"
    now = time.time()
    last = context.user_data.get(ts_key, 0)
    remaining = cooldown - (now - last)
    if remaining > 0:
        return f"Please wait {int(remaining)}s before using /{command} again."
    context.user_data[ts_key] = now
    return None
