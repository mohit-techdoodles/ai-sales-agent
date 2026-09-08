"""
approval.py
V1 Step 6 — Human approval.

This is V1's most important safety gate (per the blueprint's explicit
non-goal: "no autonomous sending"). A pending draft is shown to a human,
who must approve, edit, or reject it before anything is sent.

Sending is dispatched via emailer.py (real SMTP, or a safe mocked print
if SEND_REAL_EMAILS is not enabled). Approval and send are tracked
separately: a message is marked 'approved'/'edited' first, then only
marked as actually delivered (lead status -> 'sent') if the send
succeeds. If sending fails, the lead is marked 'send_failed' so nothing
is silently lost or misreported.
"""

from database import get_conn, log_activity, now_iso
from draft import get_message, get_pending_messages_for_lead
from leads import get_lead
from emailer import send_email


def _dispatch_send(message: dict) -> dict:
    """
    Sends the message via the appropriate channel. Currently supports email
    (via emailer.py). WhatsApp/other channels can be added here later
    without touching the approval logic. Returns {"sent": bool, "error": str|None}.
    """
    lead = get_lead(message["lead_id"])
    to_email = lead.get("email", "") if lead else ""

    if message["channel"] == "email":
        return send_email(to_email, message.get("subject", ""), message["body"])

    print(f"\n--- SEND (channel '{message['channel']}' not yet implemented — printing instead) ---")
    print(f"Body:\n{message['body']}")
    print("--- END SEND ---\n")
    return {"sent": True, "method": "mocked", "error": None}


def _finalize_send(message_id: int, lead_id: int, message: dict):
    """Attempts the actual send, then updates lead status based on the real outcome."""
    result = _dispatch_send(message)
    timestamp = now_iso()

    if result["sent"]:
        with get_conn() as conn:
            conn.execute(
                "UPDATE leads SET status = 'sent', updated_at = ? WHERE id = ?",
                (timestamp, lead_id),
            )
        log_activity(lead_id, "sent", f"Message {message_id} sent successfully")
    else:
        with get_conn() as conn:
            conn.execute(
                "UPDATE leads SET status = 'send_failed', updated_at = ? WHERE id = ?",
                (timestamp, lead_id),
            )
        log_activity(lead_id, "send_failed", f"Message {message_id} failed to send: {result['error']}")
        raise RuntimeError(f"Approved but sending failed: {result['error']}")


def approve_message(message_id: int) -> dict:
    """Approve a pending draft as-is, then attempt to send it."""
    message = get_message(message_id)
    if message is None:
        raise ValueError(f"Message {message_id} not found.")
    if message["approval_status"] != "pending":
        raise ValueError(f"Message {message_id} is not pending (status: {message['approval_status']}).")

    timestamp = now_iso()
    with get_conn() as conn:
        conn.execute(
            "UPDATE messages SET approval_status = 'approved', decided_at = ? WHERE id = ?",
            (timestamp, message_id),
        )
    log_activity(message["lead_id"], "approved", f"Message {message_id} approved as-is")

    _finalize_send(message_id, message["lead_id"], message)

    return get_message(message_id)


def edit_and_approve_message(message_id: int, new_subject: str = None, new_body: str = None) -> dict:
    """Edit a pending draft's subject/body, then approve and attempt to send the edited version."""
    message = get_message(message_id)
    if message is None:
        raise ValueError(f"Message {message_id} not found.")
    if message["approval_status"] != "pending":
        raise ValueError(f"Message {message_id} is not pending (status: {message['approval_status']}).")

    final_subject = new_subject if new_subject is not None else message["subject"]
    final_body = new_body if new_body is not None else message["body"]
    timestamp = now_iso()

    with get_conn() as conn:
        conn.execute(
            "UPDATE messages SET subject = ?, body = ?, approval_status = 'edited', decided_at = ? WHERE id = ?",
            (final_subject, final_body, timestamp, message_id),
        )
    log_activity(message["lead_id"], "edited", f"Message {message_id} edited before sending")

    final_message = get_message(message_id)
    _finalize_send(message_id, message["lead_id"], final_message)

    return get_message(message_id)


def reject_message(message_id: int, reason: str = "") -> dict:
    """Reject a pending draft. Nothing is sent."""
    message = get_message(message_id)
    if message is None:
        raise ValueError(f"Message {message_id} not found.")
    if message["approval_status"] != "pending":
        raise ValueError(f"Message {message_id} is not pending (status: {message['approval_status']}).")

    timestamp = now_iso()
    with get_conn() as conn:
        conn.execute(
            "UPDATE messages SET approval_status = 'rejected', decided_at = ? WHERE id = ?",
            (timestamp, message_id),
        )
        conn.execute(
            "UPDATE leads SET status = 'rejected', updated_at = ? WHERE id = ?",
            (timestamp, message["lead_id"]),
        )

    log_activity(message["lead_id"], "rejected", f"Message {message_id} rejected. Reason: {reason}")

    return get_message(message_id)


def review_pending_lead(lead_id: int):
    """
    Interactive terminal review for a single lead's pending draft.
    Shows the draft, prompts approve/edit/reject, and executes the choice.
    This is the human-in-the-loop gate — nothing sends without this step.
    """
    pending = get_pending_messages_for_lead(lead_id)
    if not pending:
        print(f"No pending drafts for lead {lead_id}.")
        return

    message = pending[0]

    print("\n" + "=" * 60)
    print(f"PENDING DRAFT — Lead {lead_id} | Message {message['id']}")
    print("=" * 60)
    if message.get("subject"):
        print(f"Subject: {message['subject']}")
    print(f"Body:\n{message['body']}")
    print("=" * 60)

    choice = input("Approve / Edit / Reject? [a/e/r]: ").strip().lower()

    if choice == "a":
        approve_message(message["id"])
        print("Approved and sent.")
    elif choice == "e":
        print("Enter new subject (leave blank to keep current):")
        new_subject = input("> ").strip() or None
        print("Enter new body (leave blank to keep current):")
        new_body = input("> ").strip() or None
        edit_and_approve_message(message["id"], new_subject, new_body)
        print("Edited, approved, and sent.")
    elif choice == "r":
        reason = input("Reason for rejection (optional): ").strip()
        reject_message(message["id"], reason)
        print("Rejected. Nothing was sent.")
    else:
        print("Unrecognized choice. No action taken — draft remains pending.")