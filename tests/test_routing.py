"""
tests/test_routing.py

Regression tests for next_best_action.py — the deterministic routing logic
that decides what should happen next for a lead (blueprint V3: "Next-best-
action: choose ask/reply/follow-up/route/schedule based on state and
policies").

Each test pins a lead to a specific state/status combination and asserts
the exact action returned, so a future refactor can't silently change
routing behavior (e.g. nudging an opted-out lead, or skipping a needed
escalation) without a test failing.
"""
from datetime import datetime, timedelta, timezone

import next_best_action as nba
from database import get_conn
from leads import receive_lead


_counter = [0]


def _lead(status="new", **extra_fields):
    _counter[0] += 1
    lead = receive_lead(
        name="Routing Test", email=f"route{_counter[0]}@example.com",
        company="RouteCo", source="test", message="hello",
    )
    fields = {"status": status, **extra_fields}
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE leads SET {set_clause} WHERE id = ?", (*fields.values(), lead["id"]))
    return lead["id"]


def _insert_message(lead_id, direction="outbound", approval_status="pending",
                     needs_escalation=0, body="hi", days_ago=0):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO messages (lead_id, channel, direction, subject, body,
               approval_status, needs_escalation, created_at)
               VALUES (?, 'email', ?, 'Subj', ?, ?, ?, ?)""",
            (lead_id, direction, body, approval_status, needs_escalation, ts),
        )


def test_opted_out_and_rejected_are_terminal():
    for status in ("opted_out", "rejected"):
        lead_id = _lead(status=status)
        assert nba.get_next_best_action(lead_id)["action"] == "none"


def test_awaiting_info_waits_on_the_lead():
    lead_id = _lead(status="awaiting_info")
    assert nba.get_next_best_action(lead_id)["action"] == "wait"


def test_new_qualifying_qualified_need_the_pipeline():
    for status in ("new", "qualifying", "qualified"):
        lead_id = _lead(status=status)
        assert nba.get_next_best_action(lead_id)["action"] == "run_pipeline"


def test_drafted_with_pending_escalation_routes_to_escalate():
    lead_id = _lead(status="drafted")
    _insert_message(lead_id, approval_status="pending", needs_escalation=1)
    assert nba.get_next_best_action(lead_id)["action"] == "escalate"


def test_drafted_without_escalation_routes_to_review():
    lead_id = _lead(status="drafted")
    _insert_message(lead_id, approval_status="pending", needs_escalation=0)
    assert nba.get_next_best_action(lead_id)["action"] == "review_draft"


def test_send_failed_routes_to_retry():
    lead_id = _lead(status="send_failed")
    assert nba.get_next_best_action(lead_id)["action"] == "retry_send"


def test_sent_lead_waits_before_followup_threshold(monkeypatch):
    import followup
    monkeypatch.setattr(followup, "FOLLOWUP_DUE_DAYS", 3.0)  # pinned, independent of .env
    lead_id = _lead(status="sent")
    _insert_message(lead_id, approval_status="approved", days_ago=1)
    assert nba.get_next_best_action(lead_id)["action"] == "wait"


def test_sent_lead_nudges_after_followup_threshold(monkeypatch):
    import followup
    monkeypatch.setattr(followup, "FOLLOWUP_DUE_DAYS", 3.0)  # pinned, independent of .env
    lead_id = _lead(status="sent")
    _insert_message(lead_id, approval_status="approved", days_ago=10)
    assert nba.get_next_best_action(lead_id)["action"] == "send_nudge"


def test_sent_lead_at_max_nudges_does_not_nudge_again(monkeypatch):
    import followup
    monkeypatch.setattr(followup, "FOLLOWUP_DUE_DAYS", 3.0)  # pinned, independent of .env
    lead_id = _lead(status="sent", nudge_count=99)
    _insert_message(lead_id, approval_status="approved", days_ago=10)
    assert nba.get_next_best_action(lead_id)["action"] == "wait"


def test_replied_with_scheduling_language_proposes_meeting():
    lead_id = _lead(status="replied")
    _insert_message(lead_id, direction="inbound", approval_status="received",
                     body="Sure, when can we schedule a call this week?")
    assert nba.get_next_best_action(lead_id)["action"] == "propose_meeting"


def test_replied_without_scheduling_language_needs_a_reply():
    lead_id = _lead(status="replied")
    _insert_message(lead_id, direction="inbound", approval_status="received",
                     body="Can you tell me more about pricing?")
    assert nba.get_next_best_action(lead_id)["action"] == "generate_reply"


def test_get_all_next_actions_excludes_none_and_wait():
    _lead(status="opted_out")
    active_id = _lead(status="send_failed")
    results = nba.get_all_next_actions()
    ids = {r["id"] for r in results}
    assert active_id in ids
    for r in results:
        assert r["next_action"] not in ("none", "wait")