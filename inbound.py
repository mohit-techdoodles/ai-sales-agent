"""
inbound.py
Shared logic for processing an inbound message from ANY channel (email,
telegram, future channels) — opt-out detection, storage, and triggering
re-extraction/re-scoring. Extracted so email (replies.py) and Telegram
(telegram_bot.py) both go through the identical, well-tested pipeline
rather than duplicating this logic per channel.
"""

from database import get_conn, log_activity, now_iso

OPT_OUT_KEYWORDS = [
    "unsubscribe", "stop emailing", "stop messaging", "remove me", "opt out", "opt-out",
    "do not contact", "don't contact", "no longer interested", "take me off",
]


def is_opt_out(body: str) -> bool:
    body_lower = body.lower()
    return any(kw in body_lower for kw in OPT_OUT_KEYWORDS)


def store_inbound_message(lead_id: int, channel: str, subject: str, body: str, external_id: str = "") -> int:
    """
    Saves an inbound message from any channel, updates lead status
    (replied or opted_out), logs activity, and (for genuine replies)
    triggers re-extraction + re-scoring from the full conversation thread.
    Returns the new message's id.
    """
    # Local imports to avoid circular imports at module load time.
    from qualify import reextract_from_reply
    from scoring import rescore_lead

    timestamp = now_iso()
    opted_out = is_opt_out(body)

    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO messages (lead_id, channel, direction, subject, body, approval_status, external_id, created_at)
            VALUES (?, ?, 'inbound', ?, ?, 'received', ?, ?)
            """,
            (lead_id, channel, subject, body, external_id, timestamp),
        )
        message_id = cursor.lastrowid

        new_status = "opted_out" if opted_out else "replied"
        conn.execute(
            "UPDATE leads SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, timestamp, lead_id),
        )

    if opted_out:
        log_activity(lead_id, "opted_out", f"Reply via {channel} contained opt-out language: '{body[:100]}'")
    else:
        log_activity(lead_id, "reply_received", f"Reply via {channel}: '{body[:100]}'")
        try:
            reextract_from_reply(lead_id)
            rescore_lead(lead_id)
        except Exception as e:
            print(f"[inbound] Re-scoring after reply failed (non-blocking): {e}")

    return message_id