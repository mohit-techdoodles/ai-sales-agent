"""
replies.py
V2 Step 1 — Reply detection.

Polls the same inbox we send from (via IMAP, using the same Gmail App
Password already configured for SMTP) for new emails, matches the sender
to an existing lead by email address, and stores the reply as an inbound
message — the foundation for V2's conversation memory, follow-up stop
conditions, and "replied" funnel stage.

Also does basic opt-out detection while processing each reply (per the
blueprint's V2 guardrail: "stop follow-ups on reply, opt-out, or explicit
do-not-contact requests") — this is the natural place to catch it, since
we're already reading the reply's content here.

Note: this is polling-based (call check_for_replies() to check), not a
live push notification — Streamlit apps don't have a persistent background
process on the free tier, so this runs on-demand (e.g. a "Check for
replies" button) rather than continuously in the background.
"""

import email
import imaplib
import os
from email.header import decode_header
from email.utils import parseaddr

from dotenv import load_dotenv

from database import get_conn, log_activity, now_iso
from qualify import reextract_from_reply
from scoring import rescore_lead

load_dotenv()

OPT_OUT_KEYWORDS = [
    "unsubscribe", "stop emailing", "remove me", "opt out", "opt-out",
    "do not contact", "don't contact", "no longer interested", "take me off",
]


def _connect_imap():
    """Connects to the IMAP server using the same credentials as SMTP sending."""
    host = os.environ.get("IMAP_HOST", "imap.gmail.com")
    port = int(os.environ.get("IMAP_PORT", "993"))
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")

    if not all([username, password]):
        raise RuntimeError("SMTP_USERNAME/SMTP_PASSWORD not set — needed for IMAP login too.")

    conn = imaplib.IMAP4_SSL(host, port)
    conn.login(username, password)
    return conn


def _decode_str(value) -> str:
    """Decodes an email header that may be MIME-encoded (e.g. '=?UTF-8?...?=')."""
    if value is None:
        return ""
    parts = decode_header(value)
    decoded = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            decoded += text.decode(enc or "utf-8", errors="replace")
        else:
            decoded += text
    return decoded


def _get_plain_text_body(msg) -> str:
    """Extracts the plain-text body from an email.message.Message, handling multipart."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in disposition:
                charset = part.get_content_charset() or "utf-8"
                try:
                    return part.get_payload(decode=True).decode(charset, errors="replace")
                except Exception:
                    continue
        return ""
    else:
        charset = msg.get_content_charset() or "utf-8"
        try:
            return msg.get_payload(decode=True).decode(charset, errors="replace")
        except Exception:
            return str(msg.get_payload())


def _parse_email(raw_bytes: bytes) -> dict:
    """Parses raw email bytes into {from_email, subject, body, message_id}."""
    msg = email.message_from_bytes(raw_bytes)
    _, from_email = parseaddr(msg.get("From", ""))
    subject = _decode_str(msg.get("Subject", ""))
    body = _get_plain_text_body(msg).strip()
    message_id = msg.get("Message-ID", "") or ""
    return {
        "from_email": from_email.strip().lower(),
        "subject": subject,
        "body": body,
        "message_id": message_id,
    }


def _find_lead_by_email(email_address: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM leads WHERE LOWER(email) = ?", (email_address.strip().lower(),)
        ).fetchone()
        return dict(row) if row else None


def _already_imported(message_id: str) -> bool:
    if not message_id:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM messages WHERE external_id = ?", (message_id,)
        ).fetchone()
        return row is not None


def _is_opt_out(body: str) -> bool:
    body_lower = body.lower()
    return any(kw in body_lower for kw in OPT_OUT_KEYWORDS)


def _store_inbound_message(lead_id: int, parsed: dict) -> int:
    """Saves the reply, updates lead status (replied or opted_out), logs activity."""
    timestamp = now_iso()
    is_opt_out = _is_opt_out(parsed["body"])

    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO messages (lead_id, channel, direction, subject, body, approval_status, external_id, created_at)
            VALUES (?, 'email', 'inbound', ?, ?, 'received', ?, ?)
            """,
            (lead_id, parsed["subject"], parsed["body"], parsed["message_id"], timestamp),
        )
        message_id_row = cursor.lastrowid

        new_status = "opted_out" if is_opt_out else "replied"
        conn.execute(
            "UPDATE leads SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, timestamp, lead_id),
        )

    if is_opt_out:
        log_activity(lead_id, "opted_out", f"Reply contained opt-out language: '{parsed['body'][:100]}'")
    else:
        log_activity(lead_id, "reply_received", f"Reply: '{parsed['body'][:100]}'")
        # V2 Step 3: a genuine reply may reveal new info (budget, timeline, etc.)
        # and is itself a positive engagement signal — re-extract and re-score.
        # Never done for opt-outs (nothing to gain from scoring someone leaving).
        try:
            reextract_from_reply(lead_id)
            rescore_lead(lead_id)
        except Exception as e:
            print(f"[replies] Re-scoring after reply failed (non-blocking): {e}")

    return message_id_row


def check_for_replies() -> dict:
    """
    Connects to the inbox, checks unseen emails, matches to leads, stores
    matched replies as inbound messages. Returns a summary dict.
    Unmatched emails (from unknown senders) are left alone — not marked as
    seen — so a human can review the inbox directly if needed.
    """
    summary = {"checked": 0, "matched": 0, "unmatched": 0, "opted_out": 0, "duplicates_skipped": 0}

    conn = _connect_imap()
    try:
        conn.select("INBOX")
        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            return summary

        email_ids = data[0].split()
        summary["checked"] = len(email_ids)

        for eid in email_ids:
            status, msg_data = conn.fetch(eid, "(RFC822)")
            if status != "OK" or not msg_data or msg_data[0] is None:
                continue

            raw_bytes = msg_data[0][1]
            parsed = _parse_email(raw_bytes)

            if _already_imported(parsed["message_id"]):
                summary["duplicates_skipped"] += 1
                conn.store(eid, "+FLAGS", "\\Seen")
                continue

            lead = _find_lead_by_email(parsed["from_email"])
            if lead is None:
                summary["unmatched"] += 1
                continue  # leave unmatched emails unread for manual review

            _store_inbound_message(lead["id"], parsed)
            summary["matched"] += 1
            if _is_opt_out(parsed["body"]):
                summary["opted_out"] += 1

            conn.store(eid, "+FLAGS", "\\Seen")

    finally:
        conn.logout()
"""
replies.py
V2 Step 1 — Reply detection.

Polls the same inbox we send from (via IMAP, using the same Gmail App
Password already configured for SMTP) for new emails, matches the sender
to an existing lead by email address, and stores the reply as an inbound
message — the foundation for V2's conversation memory, follow-up stop
conditions, and "replied" funnel stage.

Also does basic opt-out detection while processing each reply (per the
blueprint's V2 guardrail: "stop follow-ups on reply, opt-out, or explicit
do-not-contact requests") — this is the natural place to catch it, since
we're already reading the reply's content here.

Note: this is polling-based (call check_for_replies() to check), not a
live push notification — Streamlit apps don't have a persistent background
process on the free tier, so this runs on-demand (e.g. a "Check for
replies" button) rather than continuously in the background.
"""

import email
import imaplib
import os
from email.header import decode_header
from email.utils import parseaddr

from dotenv import load_dotenv

from database import get_conn, log_activity, now_iso
from inbound import store_inbound_message, is_opt_out

load_dotenv()


def _connect_imap():
    """Connects to the IMAP server using the same credentials as SMTP sending."""
    host = os.environ.get("IMAP_HOST", "imap.gmail.com")
    port = int(os.environ.get("IMAP_PORT", "993"))
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")

    if not all([username, password]):
        raise RuntimeError("SMTP_USERNAME/SMTP_PASSWORD not set — needed for IMAP login too.")

    conn = imaplib.IMAP4_SSL(host, port)
    conn.login(username, password)
    return conn


def _decode_str(value) -> str:
    """Decodes an email header that may be MIME-encoded (e.g. '=?UTF-8?...?=')."""
    if value is None:
        return ""
    parts = decode_header(value)
    decoded = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            decoded += text.decode(enc or "utf-8", errors="replace")
        else:
            decoded += text
    return decoded


def _get_plain_text_body(msg) -> str:
    """Extracts the plain-text body from an email.message.Message, handling multipart."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in disposition:
                charset = part.get_content_charset() or "utf-8"
                try:
                    return part.get_payload(decode=True).decode(charset, errors="replace")
                except Exception:
                    continue
        return ""
    else:
        charset = msg.get_content_charset() or "utf-8"
        try:
            return msg.get_payload(decode=True).decode(charset, errors="replace")
        except Exception:
            return str(msg.get_payload())


def _parse_email(raw_bytes: bytes) -> dict:
    """Parses raw email bytes into {from_email, subject, body, message_id}."""
    msg = email.message_from_bytes(raw_bytes)
    _, from_email = parseaddr(msg.get("From", ""))
    subject = _decode_str(msg.get("Subject", ""))
    body = _get_plain_text_body(msg).strip()
    message_id = msg.get("Message-ID", "") or ""
    return {
        "from_email": from_email.strip().lower(),
        "subject": subject,
        "body": body,
        "message_id": message_id,
    }


def _find_lead_by_email(email_address: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM leads WHERE LOWER(email) = ?", (email_address.strip().lower(),)
        ).fetchone()
        return dict(row) if row else None


def _already_imported(message_id: str) -> bool:
    if not message_id:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM messages WHERE external_id = ?", (message_id,)
        ).fetchone()
        return row is not None


def _store_inbound_message(lead_id: int, parsed: dict) -> int:
    """Thin wrapper: delegates to the shared channel-agnostic inbound pipeline."""
    return store_inbound_message(
        lead_id, channel="email", subject=parsed["subject"], body=parsed["body"], external_id=parsed["message_id"]
    )


def check_for_replies() -> dict:
    """
    Connects to the inbox, checks unseen emails, matches to leads, stores
    matched replies as inbound messages. Returns a summary dict.
    Unmatched emails (from unknown senders) are left alone — not marked as
    seen — so a human can review the inbox directly if needed.
    """
    summary = {"checked": 0, "matched": 0, "unmatched": 0, "opted_out": 0, "duplicates_skipped": 0}

    conn = _connect_imap()
    try:
        conn.select("INBOX")
        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            return summary

        email_ids = data[0].split()
        summary["checked"] = len(email_ids)

        for eid in email_ids:
            status, msg_data = conn.fetch(eid, "(RFC822)")
            if status != "OK" or not msg_data or msg_data[0] is None:
                continue

            raw_bytes = msg_data[0][1]
            parsed = _parse_email(raw_bytes)

            if _already_imported(parsed["message_id"]):
                summary["duplicates_skipped"] += 1
                conn.store(eid, "+FLAGS", "\\Seen")
                continue

            lead = _find_lead_by_email(parsed["from_email"])
            if lead is None:
                summary["unmatched"] += 1
                continue  # leave unmatched emails unread for manual review

            _store_inbound_message(lead["id"], parsed)
            summary["matched"] += 1
            if is_opt_out(parsed["body"]):
                summary["opted_out"] += 1

            conn.store(eid, "+FLAGS", "\\Seen")

    finally:
        conn.logout()

    return summary


def get_conversation(lead_id: int) -> list[dict]:
    """Full two-way message thread for a lead (inbound + outbound), oldest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE lead_id = ? ORDER BY created_at ASC",
            (lead_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    return summary


def get_conversation(lead_id: int) -> list[dict]:
    """Full two-way message thread for a lead (inbound + outbound), oldest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE lead_id = ? ORDER BY created_at ASC",
            (lead_id,),
        ).fetchall()
        return [dict(r) for r in rows]