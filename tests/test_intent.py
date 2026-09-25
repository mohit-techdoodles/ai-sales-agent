"""
tests/test_intent.py

Regression tests for intent.py — the zero-infrastructure stand-in for the
blueprint's website-tracking intent_events, derived from conversation data
already in the messages table (reply latency, latency trend, engagement
depth, high-intent keywords).
"""
from datetime import datetime, timedelta, timezone

from database import get_conn
from leads import receive_lead
import intent


def _lead():
    lead = receive_lead(name="Intent Test", email=f"intent{id(object())}@example.com",
                         company="IntentCo", source="test")
    return lead["id"]


def _insert_message(lead_id, direction, body, at: datetime):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (lead_id, channel, direction, body, approval_status, created_at) "
            "VALUES (?, 'email', ?, ?, 'approved', ?)",
            (lead_id, direction, body, at.isoformat()),
        )


def test_no_messages_gives_insufficient_data_and_no_bonus():
    lead_id = _lead()
    signals = intent.compute_intent_signals(lead_id)
    assert signals["reply_count"] == 0
    assert signals["trend"] == "insufficient_data"
    assert signals["bonus_points"] == 0
    assert signals["latest_latency_seconds"] is None


def test_fast_reply_earns_bonus():
    lead_id = _lead()
    base = datetime.now(timezone.utc)
    _insert_message(lead_id, "outbound", "Hi, interested in a call?", base)
    _insert_message(lead_id, "inbound", "Sure!", base + timedelta(minutes=5))

    signals = intent.compute_intent_signals(lead_id)
    assert signals["latest_latency_seconds"] == 300
    assert signals["bonus_points"] >= intent.FAST_REPLY_BONUS
    assert any("fast reply" in r for r in signals["signal_reasons"])


def test_slow_reply_earns_no_fast_reply_bonus():
    lead_id = _lead()
    base = datetime.now(timezone.utc)
    _insert_message(lead_id, "outbound", "Hi, interested?", base)
    _insert_message(lead_id, "inbound", "Maybe, let me think.", base + timedelta(hours=5))

    signals = intent.compute_intent_signals(lead_id)
    assert not any("fast reply" in r for r in signals["signal_reasons"])


def test_warming_trend_detected_when_replies_get_faster():
    lead_id = _lead()
    base = datetime.now(timezone.utc)
    # First exchange: slow reply (2 hours)
    _insert_message(lead_id, "outbound", "Hi", base)
    _insert_message(lead_id, "inbound", "ok", base + timedelta(hours=2))
    # Second exchange: much faster reply (5 minutes) -> warming
    _insert_message(lead_id, "outbound", "Any thoughts?", base + timedelta(hours=3))
    _insert_message(lead_id, "inbound", "Yes!", base + timedelta(hours=3, minutes=5))

    signals = intent.compute_intent_signals(lead_id)
    assert signals["trend"] == "warming"
    assert any("warming up" in r for r in signals["signal_reasons"])


def test_cooling_trend_detected_when_replies_get_slower():
    lead_id = _lead()
    base = datetime.now(timezone.utc)
    _insert_message(lead_id, "outbound", "Hi", base)
    _insert_message(lead_id, "inbound", "ok", base + timedelta(minutes=5))
    _insert_message(lead_id, "outbound", "Following up", base + timedelta(hours=1))
    _insert_message(lead_id, "inbound", "sorry, been busy", base + timedelta(hours=25))

    signals = intent.compute_intent_signals(lead_id)
    assert signals["trend"] == "cooling"
    # Cooling gets flagged but earns no bonus points from that signal
    assert not any("warming up" in r for r in signals["signal_reasons"])


def test_engagement_depth_bonus_capped():
    lead_id = _lead()
    base = datetime.now(timezone.utc)
    for i in range(10):  # far more than needed to hit the cap
        _insert_message(lead_id, "outbound", f"msg {i}", base + timedelta(hours=i))
        _insert_message(lead_id, "inbound", f"reply {i}", base + timedelta(hours=i, minutes=1))

    signals = intent.compute_intent_signals(lead_id)
    assert signals["reply_count"] == 10
    depth_contribution = min(intent.DEPTH_BONUS_CAP, (signals["reply_count"] - 1) * intent.DEPTH_BONUS_PER_EXCHANGE)
    assert depth_contribution == intent.DEPTH_BONUS_CAP  # confirms the cap actually bound it


def test_high_intent_keywords_detected_in_most_recent_message_only():
    lead_id = _lead()
    base = datetime.now(timezone.utc)
    _insert_message(lead_id, "outbound", "Hi", base)
    _insert_message(lead_id, "inbound", "just curious, tell me more", base + timedelta(minutes=10))
    _insert_message(lead_id, "outbound", "Sure, here's more info", base + timedelta(minutes=20))
    _insert_message(lead_id, "inbound", "What's the pricing and how does the API integration work?",
                     base + timedelta(minutes=30))

    signals = intent.compute_intent_signals(lead_id)
    assert "pricing" in signals["high_intent_keywords_found"]
    assert "integration" in signals["high_intent_keywords_found"]
    assert "api" in signals["high_intent_keywords_found"]
    assert any("high-intent question" in r for r in signals["signal_reasons"])


def test_high_intent_keywords_ignore_older_messages():
    """Only the most recent inbound message counts — an old pricing question
    followed by a vague reply shouldn't still show as high-intent right now."""
    lead_id = _lead()
    base = datetime.now(timezone.utc)
    _insert_message(lead_id, "outbound", "Hi", base)
    _insert_message(lead_id, "inbound", "What's the pricing?", base + timedelta(minutes=10))
    _insert_message(lead_id, "outbound", "Here's pricing info", base + timedelta(minutes=20))
    _insert_message(lead_id, "inbound", "ok thanks", base + timedelta(minutes=30))

    signals = intent.compute_intent_signals(lead_id)
    assert signals["high_intent_keywords_found"] == []


def test_record_intent_signals_logs_events():
    lead_id = _lead()
    base = datetime.now(timezone.utc)
    _insert_message(lead_id, "outbound", "Hi", base)
    _insert_message(lead_id, "inbound", "What's the pricing? Can we do a demo?", base + timedelta(minutes=5))

    signals = intent.record_intent_signals(lead_id)
    history = intent.get_intent_history(lead_id)

    assert len(history) >= 2  # at least fast_reply + high_intent_question
    event_types = {e["event_type"] for e in history}
    assert "fast_reply" in event_types
    assert "high_intent_question" in event_types
    assert signals["bonus_points"] > 0


def test_intent_label_reflects_trend():
    lead_id = _lead()
    assert intent.intent_label(intent.compute_intent_signals(lead_id)) == "—"

    base = datetime.now(timezone.utc)
    _insert_message(lead_id, "outbound", "Hi", base)
    _insert_message(lead_id, "inbound", "hey", base + timedelta(hours=5))
    signals = intent.compute_intent_signals(lead_id)
    assert intent.intent_label(signals) == "➡️ Engaged"


def test_rescore_lead_adds_intent_bonus_on_top_of_flat_engagement_bonus():
    from qualify import qualify_lead
    from scoring import rescore_lead, ENGAGEMENT_BONUS
    from unittest.mock import patch

    lead = receive_lead(name="Rescore Test", email="rescore@example.com", company="X",
                         source="test", message="hi")
    with patch("qualify.extract_requirements", return_value={
        "use_case": "", "budget": "", "authority": "", "timeline": "", "notes": "",
    }):
        qualify_lead(lead["id"])  # base score will be 0

    base = datetime.now(timezone.utc)
    _insert_message(lead["id"], "outbound", "Hi", base)
    _insert_message(lead["id"], "inbound", "What's the pricing? Let's set up a demo.",
                     base + timedelta(minutes=2))  # fast + high-intent

    result = rescore_lead(lead["id"])
    # base(0) + ENGAGEMENT_BONUS(10) + fast_reply(5) + high_intent(5) = 20, well above the flat 10 alone
    assert result["score"] > ENGAGEMENT_BONUS
    assert any("fast reply" in r for r in result["reasons"])
    assert any("high-intent question" in r for r in result["reasons"])