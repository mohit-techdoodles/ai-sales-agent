"""
tests/test_send_attachment.py

Regression tests confirming that a message-level attachment (set via
attachments.py after a document/voice reply is processed) actually gets
included when the message is approved and sent — not just stored and
forgotten. Covers emailer.py's attachment support and approval.py's
plumbing between the two.
"""
import base64
from unittest.mock import patch

from approval import approve_message
from database import get_conn, now_iso
from leads import receive_lead


def _lead_with_pending_message(attachment_filename=None, attachment_data=None):
    lead = receive_lead(name="Send Test", email="send@example.com", company="SendCo", source="test")
    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO messages (lead_id, channel, direction, subject, body, approval_status,
                                   attachment_filename, attachment_data, created_at)
            VALUES (?, 'email', 'outbound', 'Subj', 'Body', 'pending', ?, ?, ?)
            """,
            (lead["id"], attachment_filename, attachment_data, now_iso()),
        )
        message_id = cursor.lastrowid
    return lead["id"], message_id


def test_emailer_includes_attachment_when_given():
    from emailer import send_email
    with patch.dict("os.environ", {"SEND_REAL_EMAILS": "false"}):
        result = send_email("lead@example.com", "Subj", "Body",
                             attachment_filename="brochure.pdf", attachment_bytes=b"pdf-bytes")
    assert result["sent"] is True  # mocked mode always "succeeds" — this just confirms it doesn't crash


def test_emailer_works_without_attachment():
    from emailer import send_email
    with patch.dict("os.environ", {"SEND_REAL_EMAILS": "false"}):
        result = send_email("lead@example.com", "Subj", "Body")
    assert result["sent"] is True


def test_approve_message_passes_attachment_through_to_send():
    """The real regression: does approving a message with an attachment
    actually forward that attachment to emailer.send_email(), or does it
    silently get dropped along the way?"""
    encoded = base64.b64encode(b"pdf-file-contents").decode("ascii")
    lead_id, message_id = _lead_with_pending_message(
        attachment_filename="proposal.pdf", attachment_data=encoded
    )

    with patch("approval.send_email") as mock_send:
        mock_send.return_value = {"sent": True, "method": "mocked", "error": None}
        approve_message(message_id)

    mock_send.assert_called_once()
    _, kwargs = mock_send.call_args
    assert kwargs["attachment_filename"] == "proposal.pdf"
    assert kwargs["attachment_bytes"] == b"pdf-file-contents"


def test_approve_message_without_attachment_sends_none():
    lead_id, message_id = _lead_with_pending_message()  # no attachment

    with patch("approval.send_email") as mock_send:
        mock_send.return_value = {"sent": True, "method": "mocked", "error": None}
        approve_message(message_id)

    mock_send.assert_called_once()
    _, kwargs = mock_send.call_args
    assert kwargs["attachment_filename"] is None
    assert kwargs["attachment_bytes"] is None