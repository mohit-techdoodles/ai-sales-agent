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
import re
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


QUOTE_MARKERS = [
    r"^On .{0,120}?wrote:\s*$",                # Gmail/most clients: "On <date>, <name> <email> wrote:"
    r"^-{2,}\s*Original Message\s*-{2,}\s*$",  # Outlook-style "-----Original Message-----"
    r"^From:\s*.+\nSent:\s*.+\nTo:\s*.+",      # Outlook-style quoted header block
]


def _drop_trailing_quote_lines(text: str) -> str:
    """Keep lines up to (not including) the first '>'-prefixed quote line."""
    lines = []
    for line in text.split("\n"):
        if line.strip().startswith(">"):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _first_non_quoted_paragraph(text_from_marker: str) -> str:
    """Given text starting AT a quote marker line, skip the marker itself
    and any '>'-quoted / blank lines right after it, and return whatever
    real content follows — this is the bottom-posting case, where the
    lead's new reply comes AFTER the quoted block instead of before it."""
    lines = text_from_marker.split("\n")
    idx = 1  # skip the marker line itself
    while idx < len(lines) and (lines[idx].strip().startswith(">") or not lines[idx].strip()):
        idx += 1
    return "\n".join(lines[idx:]).strip()


def _strip_quoted_reply(body: str) -> str:
    """
    Extracts only the NEW content the lead actually typed, cutting off the
    quoted previous message + signature that most email clients append.
    Without this, semantic cache matching (and reextraction, next-best-
    action keyword checks, etc.) get diluted by the entire quoted thread
    instead of just the new reply — a short "how much does this cost?"
    reply can otherwise carry hundreds of words of quoted boilerplate along
    with it, which meaningfully hurts embedding similarity matching.

    Handles both reply styles: top-posting (new text above the "On...wrote:"
    marker — most common in Gmail/Outlook) and bottom-posting/inline (marker
    first, then the quoted lines, then the lead's new text below all of it —
    common with plain IMAP clients, mailing-list-style replies, etc.).
    """
    body = body.replace("\r\n", "\n")

    earliest_cut = None
    for pattern in QUOTE_MARKERS:
        match = re.search(pattern, body, re.IGNORECASE | re.MULTILINE)
        if match and (earliest_cut is None or match.start() < earliest_cut):
            earliest_cut = match.start()

    if earliest_cut is not None:
        before_marker = _drop_trailing_quote_lines(body[:earliest_cut])
        if before_marker:
            return before_marker  # top-posting: new text was above the marker

        after_marker = _first_non_quoted_paragraph(body[earliest_cut:])
        if after_marker:
            return after_marker  # bottom-posting: new text is below the quoted block

    # No marker found at all — just drop any leading ">"-quoted lines wherever they are.
    stripped = _drop_trailing_quote_lines(body)
    return stripped if stripped else body.strip()  # never return empty — fall back to raw if stripping ate everything


def _parse_email(raw_bytes: bytes) -> dict:
    """Parses raw email bytes into {from_email, subject, body, message_id}."""
    msg = email.message_from_bytes(raw_bytes)
    _, from_email = parseaddr(msg.get("From", ""))
    subject = _decode_str(msg.get("Subject", ""))
    raw_body = _get_plain_text_body(msg).strip()
    body = _strip_quoted_reply(raw_body)
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