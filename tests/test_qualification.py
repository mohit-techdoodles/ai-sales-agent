"""
tests/test_qualification.py

Regression tests for qualify.py (blueprint V1 Step 3 / V3 dynamic
qualification). The LLM extraction call itself is mocked out — these tests
lock in OUR logic (missing-field detection, follow-up capping, status
transitions), not the model's output quality, so they run fast, free, and
deterministically in CI.
"""
from unittest.mock import patch

import qualify
from leads import receive_lead, get_lead


def _lead(message="We need something for our team."):
    return receive_lead(
        name="Test Lead", email=f"lead{id(message)}@example.com",
        company="TestCo", source="test", message=message,
    )


# ---------------------------------------------------------------------------
# Deterministic logic — no LLM involved
# ---------------------------------------------------------------------------

def test_missing_fields_detects_gaps_in_priority_order():
    reqs = {"use_case": "CRM for sales team", "budget": "", "authority": "", "timeline": ""}
    assert qualify.get_missing_fields(reqs) == ["authority", "budget", "timeline"]


def test_missing_fields_empty_when_all_present():
    reqs = {"use_case": "x", "budget": "x", "authority": "x", "timeline": "x"}
    assert qualify.get_missing_fields(reqs) == []


def test_choose_followup_question_falls_back_on_llm_failure():
    """If the dynamic-question LLM call fails, fall back to the fixed
    template — qualification must never crash or stall a lead."""
    missing = ["budget", "timeline"]
    with patch("qualify._call_llm_with_prompt", side_effect=RuntimeError("groq down")):
        chosen = qualify.choose_followup_question({"use_case": "CRM"}, missing)
    assert chosen["targeting_field"] == "budget"
    assert chosen["question"] == qualify.FOLLOWUP_QUESTIONS["budget"]


def test_choose_followup_question_ignores_llm_choice_outside_missing_fields():
    """If the LLM names a field that isn't actually missing, don't trust it."""
    missing = ["timeline"]
    with patch("qualify._call_llm_with_prompt",
               return_value='{"targeting_field": "budget", "question": "?"}'):
        chosen = qualify.choose_followup_question({}, missing)
    assert chosen["targeting_field"] == "timeline"


# ---------------------------------------------------------------------------
# Pipeline behavior — extraction mocked, our state machine tested for real
# ---------------------------------------------------------------------------

def test_qualify_lead_pauses_for_missing_fields():
    lead = _lead()
    with patch("qualify.extract_requirements", return_value={
        "use_case": "something for our team", "budget": "", "authority": "", "timeline": "", "notes": "",
    }):
        result = qualify.qualify_lead(lead["id"])

    assert result["status"] == "awaiting_info"
    assert result["question"]
    updated = get_lead(lead["id"])
    assert updated["status"] == "awaiting_info"
    assert updated["pending_question"] == result["question"]
    assert updated["followup_rounds"] == 1


def test_qualify_lead_proceeds_when_all_fields_present():
    lead = _lead("We need a CRM for 15 people, $400/mo, I'm the owner, need it in 2 months.")
    with patch("qualify.extract_requirements", return_value={
        "use_case": "CRM", "budget": "$400/mo", "authority": "owner", "timeline": "2 months", "notes": "",
    }):
        result = qualify.qualify_lead(lead["id"])

    assert result["status"] == "qualified"
    assert result["question"] is None
    assert get_lead(lead["id"])["status"] == "qualifying"


def test_qualify_lead_stops_asking_after_max_rounds():
    """Even with fields still missing, once MAX_FOLLOWUP_ROUNDS is used up
    the lead must proceed — never asked forever."""
    lead = _lead()
    incomplete = {"use_case": "something", "budget": "", "authority": "", "timeline": "", "notes": ""}

    with patch("qualify.extract_requirements", return_value=incomplete):
        for _ in range(qualify.MAX_FOLLOWUP_ROUNDS):
            result = qualify.qualify_lead(lead["id"])
            assert result["status"] == "awaiting_info"

        result = qualify.qualify_lead(lead["id"])  # one round past the cap

    assert result["status"] == "qualified"


def test_submit_followup_answer_reruns_qualification_and_advances():
    lead = _lead()
    with patch("qualify.extract_requirements", return_value={
        "use_case": "something", "budget": "", "authority": "", "timeline": "", "notes": "",
    }):
        qualify.qualify_lead(lead["id"])  # first pass -> awaiting_info

    with patch("qualify.extract_requirements", return_value={
        "use_case": "something", "budget": "$500", "authority": "owner", "timeline": "1 month", "notes": "",
    }):
        result = qualify.submit_followup_answer(
            lead["id"], "Budget is $500, I'm the owner, need it next month."
        )

    assert result["status"] == "qualified"
    updated = get_lead(lead["id"])
    assert "Follow-up answer:" in updated["message"]
    assert updated["pending_question"] is None