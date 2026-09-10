"""
followup.py
V2 Step 4 — Follow-up scheduling.

Identifies leads who were sent a message but never replied, once enough
time has passed. Stays consistent with this project's human-approval
philosophy: this surfaces a "due for follow-up" queue for staff to review,
it does NOT auto-send anything. Capped at MAX_NUDGES per lead so a silent
lead isn't nudged forever.
"""

import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from database import get_conn, log_activity, now_iso
from leads import list_leads, get_lead

load_dotenv()

FOLLOWUP_DUE_DAYS = float(os.environ.get("FOLLOWUP_DUE_DAYS", "3"))
MAX_NUDGES = 2  # stop suggesting follow-ups after this many, so we don't pester a silent lead forever


def _get_last_outbound_time(lead_id: int) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT created_at FROM messages
            WHERE lead_id = ? AND direction = 'outbound'
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (lead_id,),
        ).fetchone()
        return row["created_at"] if row else None


def _days_since(timestamp_str: str) -> float:
    then = datetime.fromisoformat(timestamp_str)
    now = datetime.now(timezone.utc)
    return (now - then).total_seconds() / 86400


def get_leads_due_for_followup(days_threshold: float = None) -> list[dict]:
    """
    Returns leads where: status is 'sent' (no reply, not opted out), the
    last outbound message is older than the threshold, and we haven't
    already hit MAX_NUDGES for this lead. Each returned dict includes
    'days_since_sent' for display.
    """
    threshold = days_threshold if days_threshold is not None else FOLLOWUP_DUE_DAYS
    due = []

    for lead in list_leads(status="sent"):
        if (lead.get("nudge_count") or 0) >= MAX_NUDGES:
            continue

        last_sent = _get_last_outbound_time(lead["id"])
        if last_sent is None:
            continue

        days_since = _days_since(last_sent)
        if days_since >= threshold:
            due.append({**lead, "days_since_sent": round(days_since, 1)})

    return due


def generate_nudge_draft(lead_id: int) -> dict:
    """
    Generates a follow-up nudge draft for a silent lead (goes through the
    normal approval flow like any other draft) and increments nudge_count.
    """
    # Local import to avoid a circular import at module load time.
    from draft import generate_draft

    result = generate_draft(lead_id)

    timestamp = now_iso()
    with get_conn() as conn:
        conn.execute(
            "UPDATE leads SET nudge_count = nudge_count + 1, updated_at = ? WHERE id = ?",
            (timestamp, lead_id),
        )

    log_activity(lead_id, "followup_nudge_drafted", f"Nudge draft created, message_id={result['message_id']}")

    return result