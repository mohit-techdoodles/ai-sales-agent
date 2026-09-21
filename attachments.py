"""
attachments.py
V5 — Attach new information to a lead that's already further along the
pipeline (used from the Approval Inbox), as opposed to document_parser.py /
voice.py's use in the New Lead form (info attached at first contact).

Two entry points, one shared mechanism:
  - attach_document_to_lead()   — a spec/RFP/screenshot the lead sent separately
  - attach_voice_reply_to_lead() — a voice note (e.g. staff relaying a phone call)

What happens to what you attach: it informs the AI's draft (folded into
requirements/scoring via reextract_from_reply/rescore_lead) AND is carried
forward onto the new outbound draft, so approving that draft sends the same
file/voice note back to the lead as a real email attachment (via emailer.py)
— not just text informed by its content.

Why this isn't just "extract text and append to lead.message" like the New
Lead form does: a lead in the Approval Inbox already has requirements
extracted, a score, and a pending draft. Bolting new information on needs to:
  1. Record it as a real inbound message (so it shows up in the conversation
     thread and the original file/audio is downloadable later) — this is
     what the messages.attachment_filename/attachment_data columns are for.
  2. Refresh requirements/score from the FULL thread (reextract_from_reply /
     rescore_lead), not just re-run initial extraction.
  3. Supersede the old pending draft — approving it now would ignore the new
     information — and generate a fresh one, carrying the attachment onto it.

Superseding a draft is NOT the same as rejecting it: reject_message() marks
the whole LEAD as 'rejected' (a terminal state), which would be wrong here —
we're actively continuing to work this lead, just with a better draft
incoming. So the old message row is marked 'superseded' directly, which
only removes it from the pending queue.
"""
import base64

from database import get_conn, log_activity, now_iso
from document_parser import extract_text_from_file
from draft import generate_draft, get_pending_messages_for_lead
from leads import get_lead
from qualify import reextract_from_reply
from scoring import rescore_lead
from voice import transcribe_audio

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10MB — generous for a spec/RFP, keeps DB rows sane

_EMPTY_RESULT = {"success": False, "error": None, "extracted_chars": 0,
                 "message_id": None, "score": None, "draft": None}


def _reprocess_with_new_inbound_content(lead_id: int, filename: str, raw_bytes: bytes,
                                         extracted_text: str, activity_label: str, activity_detail: str) -> dict:
    """
    Shared core for both attachment types: records the inbound content
    (with the original file/audio kept as an attachment for reference),
    supersedes the old pending draft, and re-runs qualification/scoring/
    drafting from the updated full thread.
    """
    pending = get_pending_messages_for_lead(lead_id)
    # Preserve whatever channel this lead's conversation is actually on
    # (email/telegram/etc.) so the regenerated draft goes out the right
    # way — the inbound record itself isn't a real send channel.
    channel = pending[0]["channel"] if pending else "email"

    timestamp = now_iso()
    encoded_raw = base64.b64encode(raw_bytes).decode("ascii")

    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO messages (lead_id, channel, direction, subject, body, approval_status,
                                   attachment_filename, attachment_data, created_at)
            VALUES (?, ?, 'inbound', ?, ?, 'received', ?, ?, ?)
            """,
            (lead_id, channel, filename, extracted_text, filename, encoded_raw, timestamp),
        )
        message_id = cursor.lastrowid

        # Supersede (not reject!) any existing pending draft(s) — reject_message()
        # would mark the whole lead 'rejected', which is wrong mid-pipeline.
        for msg in pending:
            conn.execute(
                "UPDATE messages SET approval_status = 'superseded', decided_at = ? WHERE id = ?",
                (timestamp, msg["id"]),
            )

    log_activity(lead_id, activity_label, activity_detail)

    reextract_from_reply(lead_id)
    score_result = rescore_lead(lead_id)
    draft_result = generate_draft(lead_id, channel=channel)

    # Carry the attachment forward onto the new outbound draft too, so
    # approving it sends the file/voice note back to the lead as a real
    # email attachment — not just text informed by its content.
    with get_conn() as conn:
        conn.execute(
            "UPDATE messages SET attachment_filename = ?, attachment_data = ? WHERE id = ?",
            (filename, encoded_raw, draft_result["message_id"]),
        )

    return {
        "success": True,
        "error": None,
        "extracted_chars": len(extracted_text),
        "message_id": message_id,
        "score": score_result["score"],
        "draft": draft_result,
    }


def _guard_lead(lead_id: int) -> dict | None:
    """Common precondition checks. Returns an error result dict, or None if OK to proceed."""
    lead = get_lead(lead_id)
    if lead is None:
        return {**_EMPTY_RESULT, "error": f"Lead {lead_id} not found."}
    if lead.get("status") == "opted_out":
        return {**_EMPTY_RESULT, "error": "This lead has opted out — cannot process further contact."}
    return None


def attach_document_to_lead(lead_id: int, filename: str, file_bytes: bytes) -> dict:
    """
    Parses an uploaded document, records it on the lead's conversation
    thread, and re-runs qualification/scoring/drafting with the new
    information folded in.

    Returns:
        {"success": bool, "error": str|None, "extracted_chars": int,
         "message_id": int|None, "score": int|None,
         "draft": {"message_id", "subject", "body", "needs_escalation"} | None}

    Never raises for a bad/unreadable file — returns success=False with a
    message instead, since this is staff-facing UI input, not a pipeline
    step that should crash the app.
    """
    guard = _guard_lead(lead_id)
    if guard:
        return guard

    if len(file_bytes) > MAX_ATTACHMENT_BYTES:
        return {**_EMPTY_RESULT, "error": f"File is too large (max {MAX_ATTACHMENT_BYTES // (1024*1024)}MB)."}

    doc_result = extract_text_from_file(filename, file_bytes)
    if not doc_result["text"]:
        return {**_EMPTY_RESULT, "error": doc_result.get("error") or "No text could be extracted from this file."}

    body_text = f"[Document attached by staff: {filename}]\n\n{doc_result['text']}"

    return _reprocess_with_new_inbound_content(
        lead_id, filename, file_bytes, body_text,
        activity_label="document_attached",
        activity_detail=f"Staff attached '{filename}' ({doc_result['method']}, {len(doc_result['text'])} chars extracted)",
    )


def attach_voice_reply_to_lead(lead_id: int, audio_bytes: bytes, filename: str = "voice_reply.wav") -> dict:
    """
    Transcribes a voice recording (e.g. staff relaying something said on a
    phone call), records it on the lead's conversation thread, and re-runs
    qualification/scoring/drafting with the new information folded in.

    Same return shape and never-raises behavior as attach_document_to_lead().
    """
    guard = _guard_lead(lead_id)
    if guard:
        return guard

    if len(audio_bytes) > MAX_ATTACHMENT_BYTES:
        return {**_EMPTY_RESULT, "error": f"Audio file is too large (max {MAX_ATTACHMENT_BYTES // (1024*1024)}MB)."}

    voice_result = transcribe_audio(audio_bytes, filename)
    if not voice_result["text"]:
        return {**_EMPTY_RESULT, "error": voice_result.get("error") or "Couldn't transcribe this recording."}

    body_text = f"[Voice note attached by staff: {filename}]\n\n{voice_result['text']}"

    return _reprocess_with_new_inbound_content(
        lead_id, filename, audio_bytes, body_text,
        activity_label="voice_reply_attached",
        activity_detail=f"Staff attached a voice note ({len(voice_result['text'])} chars transcribed)",
    )