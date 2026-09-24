"""
tests/test_send_attachment.py

Regression tests confirming that a message-level attachment (set via
attachments.py after a document/voice reply is processed) actually gets
included when the message is approved and sent — not just stored and
forgotten. Covers emailer.py's attachment support and approval.py's
plumbing between the two.
"""
import base64
from unittest.mock import patch, MagicMock

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


# ---------------------------------------------------------------------------
# Telegram channel — same plumbing, different transport
# ---------------------------------------------------------------------------

def _lead_with_pending_telegram_message(attachment_filename=None, attachment_data=None):
    lead = receive_lead(name="Telegram Send Test", email="tg@example.com", company="TgCo", source="test")
    with get_conn() as conn:
        conn.execute("UPDATE leads SET telegram_chat_id = ? WHERE id = ?", ("12345", lead["id"]))
        cursor = conn.execute(
            """
            INSERT INTO messages (lead_id, channel, direction, subject, body, approval_status,
                                   attachment_filename, attachment_data, created_at)
            VALUES (?, 'telegram', 'outbound', NULL, 'Body text', 'pending', ?, ?, ?)
            """,
            (lead["id"], attachment_filename, attachment_data, now_iso()),
        )
        message_id = cursor.lastrowid
    return lead["id"], message_id


def test_approve_telegram_message_passes_attachment_through():
    encoded = base64.b64encode(b"voice-note-bytes").decode("ascii")
    lead_id, message_id = _lead_with_pending_telegram_message(
        attachment_filename="voice_reply.wav", attachment_data=encoded
    )

    with patch("approval.send_telegram_message") as mock_send:
        mock_send.return_value = {"sent": True, "error": None}
        approve_message(message_id)

    mock_send.assert_called_once()
    _, kwargs = mock_send.call_args
    assert kwargs["attachment_filename"] == "voice_reply.wav"
    assert kwargs["attachment_bytes"] == b"voice-note-bytes"


def test_telegram_send_document_uses_send_document_endpoint_not_send_voice():
    """Deliberate choice: WAV audio goes through sendDocument, not sendVoice
    (which requires OGG/OPUS and could reject/mishandle our WAV files)."""
    import telegram_bot
    with patch("telegram_bot.BOT_TOKEN", "fake-token"), \
         patch("telegram_bot.API_BASE", "https://api.telegram.org/botfake-token"), \
         patch("telegram_bot.requests.post") as mock_post:
        mock_post.return_value.json.return_value = {"ok": True}
        telegram_bot.send_telegram_message(
            "12345", "Here's the recording", attachment_filename="voice_reply.wav",
            attachment_bytes=b"audio-bytes",
        )

    urls_called = [call.args[0] for call in mock_post.call_args_list]
    assert any(url.endswith("/sendMessage") for url in urls_called)
    assert any(url.endswith("/sendDocument") for url in urls_called)
    assert not any(url.endswith("/sendVoice") for url in urls_called)


def test_telegram_send_reports_partial_failure_when_attachment_fails_but_text_sent():
    import telegram_bot
    with patch("telegram_bot.BOT_TOKEN", "fake-token"), \
         patch("telegram_bot.API_BASE", "https://api.telegram.org/botfake-token"), \
         patch("telegram_bot.requests.post") as mock_post:
        # First call (sendMessage) succeeds, second (sendDocument) fails
        mock_post.side_effect = [
            MagicMock(json=lambda: {"ok": True}),
            MagicMock(json=lambda: {"ok": False, "description": "file too large"}),
        ]
        result = telegram_bot.send_telegram_message(
            "12345", "Here's the doc", attachment_filename="big.pdf", attachment_bytes=b"x" * 10,
        )

    assert result["sent"] is True  # text got through
    assert "attachment failed" in result["error"]
    assert "file too large" in result["error"]


def test_telegram_send_without_chat_id_fails_before_any_request():
    import telegram_bot
    with patch("telegram_bot.BOT_TOKEN", "fake-token"), patch("telegram_bot.requests.post") as mock_post:
        result = telegram_bot.send_telegram_message("", "hello")

    assert result["sent"] is False
    mock_post.assert_not_called()