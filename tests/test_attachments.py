"""
tests/test_attachments.py

Regression tests for attachments.py (V5 — attaching a document to a lead
from the Approval Inbox, as opposed to document_parser's use in the New
Lead form). Document parsing, requirement extraction, and the drafting
LLM call are all mocked — these tests lock in OUR orchestration logic:
that the old pending draft is superseded (not rejected — rejecting would
wrongly terminate the lead), that the attachment is recorded on the
thread, and that a fresh draft is generated from the updated context.
"""
from unittest.mock import patch

import attachments
from database import get_conn
from draft import get_pending_messages_for_lead
from leads import receive_lead, get_lead, mark_opted_out


def _lead_with_pending_draft():
    lead = receive_lead(name="Attach Test", email=f"attach{id(object())}@example.com",
                         company="AttachCo", source="test", message="We need a CRM.")
    with patch("qualify.extract_requirements", return_value={
        "use_case": "CRM", "budget": "$300/mo", "authority": "owner", "timeline": "soon", "notes": "",
    }):
        from qualify import qualify_lead
        qualify_lead(lead["id"])
    from scoring import score_lead
    score_lead(lead["id"])
    with patch("draft._call_llm", return_value='{"subject": "Hi", "body": "Original draft.", "needs_escalation": false, "escalation_reason": ""}'):
        from draft import generate_draft
        generate_draft(lead["id"])
    return lead["id"]


def _mock_generate_draft_response():
    return patch(
        "draft._call_llm",
        return_value='{"subject": "Re: your RFP", "body": "Thanks for the details.", '
                     '"needs_escalation": false, "escalation_reason": ""}',
    )


def test_attach_document_supersedes_old_draft_without_rejecting_the_lead():
    lead_id = _lead_with_pending_draft()
    original_pending = get_pending_messages_for_lead(lead_id)
    assert len(original_pending) == 1
    original_message_id = original_pending[0]["id"]

    with patch("attachments.extract_text_from_file",
               return_value={"text": "Budget approved: $5000/mo. Need it in 3 weeks.", "method": "pdf_direct", "error": None}), \
         patch("qualify.extract_requirements", return_value={
             "use_case": "CRM", "budget": "$5000/mo", "authority": "owner", "timeline": "3 weeks", "notes": "",
         }), \
         _mock_generate_draft_response():
        result = attachments.attach_document_to_lead(lead_id, "rfp.pdf", b"fake-pdf-bytes")

    assert result["success"] is True
    assert result["extracted_chars"] > 0

    # Old draft must be superseded, not rejected — the lead is still active.
    with get_conn() as conn:
        old = conn.execute("SELECT approval_status FROM messages WHERE id = ?", (original_message_id,)).fetchone()
    assert old["approval_status"] == "superseded"

    lead = get_lead(lead_id)
    assert lead["status"] != "rejected"

    # A new pending draft must exist, and it must be a different message than the old one.
    new_pending = get_pending_messages_for_lead(lead_id)
    assert len(new_pending) == 1
    assert new_pending[0]["id"] != original_message_id
    assert new_pending[0]["id"] == result["draft"]["message_id"]

    # The attachment must be carried forward onto the NEW outbound draft too,
    # so approving it sends the file back to the lead, not just text informed by it.
    assert new_pending[0]["attachment_filename"] == "rfp.pdf"
    assert new_pending[0]["attachment_data"]


def test_attach_document_records_the_attachment_on_the_thread():
    lead_id = _lead_with_pending_draft()

    with patch("attachments.extract_text_from_file",
               return_value={"text": "Some RFP content here.", "method": "pdf_direct", "error": None}), \
         patch("qualify.extract_requirements", return_value={
             "use_case": "CRM", "budget": "$300/mo", "authority": "owner", "timeline": "soon", "notes": "",
         }), \
         _mock_generate_draft_response():
        result = attachments.attach_document_to_lead(lead_id, "specs.pdf", b"fake-bytes")

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE id = ?", (result["message_id"],)
        ).fetchone()
    assert row["direction"] == "inbound"
    assert row["attachment_filename"] == "specs.pdf"
    assert row["attachment_data"]  # base64 payload stored
    assert "Some RFP content here." in row["body"]


def test_attach_document_fails_soft_on_unreadable_file():
    lead_id = _lead_with_pending_draft()

    with patch("attachments.extract_text_from_file",
               return_value={"text": "", "method": "failed", "error": "corrupt file"}):
        result = attachments.attach_document_to_lead(lead_id, "broken.pdf", b"garbage")

    assert result["success"] is False
    assert result["error"] == "corrupt file"

    # Nothing should have changed — the original draft is still pending.
    pending = get_pending_messages_for_lead(lead_id)
    assert len(pending) == 1


def test_attach_document_refuses_for_opted_out_lead():
    lead = receive_lead(name="Opted Out", email="optout@example.com", company="X", source="test", message="hi")
    mark_opted_out(lead["id"], reason="test")

    result = attachments.attach_document_to_lead(lead["id"], "doc.pdf", b"bytes")

    assert result["success"] is False
    assert "opted out" in result["error"].lower()


# ---------------------------------------------------------------------------
# Voice reply — shares the same reprocessing core as documents
# ---------------------------------------------------------------------------

def test_attach_voice_reply_supersedes_old_draft_and_transcribes():
    lead_id = _lead_with_pending_draft()
    original_pending = get_pending_messages_for_lead(lead_id)
    original_message_id = original_pending[0]["id"]

    with patch("attachments.transcribe_audio",
               return_value={"text": "Actually our budget is $2000 a month.", "error": None}), \
         patch("qualify.extract_requirements", return_value={
             "use_case": "CRM", "budget": "$2000/mo", "authority": "owner", "timeline": "soon", "notes": "",
         }), \
         _mock_generate_draft_response():
        result = attachments.attach_voice_reply_to_lead(lead_id, b"fake-audio-bytes")

    assert result["success"] is True
    assert result["extracted_chars"] > 0

    with get_conn() as conn:
        old = conn.execute("SELECT approval_status FROM messages WHERE id = ?", (original_message_id,)).fetchone()
        new_row = conn.execute("SELECT * FROM messages WHERE id = ?", (result["message_id"],)).fetchone()
    assert old["approval_status"] == "superseded"
    assert new_row["direction"] == "inbound"
    assert new_row["attachment_data"]  # raw audio kept for reference
    assert "Actually our budget is $2000 a month." in new_row["body"]

    new_pending = get_pending_messages_for_lead(lead_id)
    assert len(new_pending) == 1
    assert new_pending[0]["id"] == result["draft"]["message_id"]
    # The voice recording itself carries forward onto the new draft too.
    assert new_pending[0]["attachment_data"]


def test_attach_voice_reply_fails_soft_on_transcription_error():
    lead_id = _lead_with_pending_draft()

    with patch("attachments.transcribe_audio", return_value={"text": "", "error": "Groq API down"}):
        result = attachments.attach_voice_reply_to_lead(lead_id, b"fake-audio-bytes")

    assert result["success"] is False
    assert result["error"] == "Groq API down"

    # Nothing should have changed — the original draft is still pending.
    pending = get_pending_messages_for_lead(lead_id)
    assert len(pending) == 1


def test_attach_voice_reply_refuses_for_opted_out_lead():
    lead = receive_lead(name="Opted Out V", email="optoutv@example.com", company="X", source="test", message="hi")
    mark_opted_out(lead["id"], reason="test")

    result = attachments.attach_voice_reply_to_lead(lead["id"], b"audio-bytes")

    assert result["success"] is False
    assert "opted out" in result["error"].lower()