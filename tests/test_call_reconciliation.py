"""
tests/test_call_reconciliation.py

Regression tests for call_reconciliation.py (V6-B, manual-entry version).
The LLM comparison call is mocked (same pattern as test_llm_pii_wiring.py)
— these tests lock in OUR logic: that requirements only get overwritten
for fields the transcript actually addressed, that the score comes from
scoring.py's deterministic calculation (never a raw LLM-provided number),
and that the reconciliation still works when no briefing exists yet.
"""
from unittest.mock import patch, MagicMock

from leads import receive_lead
from qualify import qualify_lead
from scoring import score_lead
from briefing import get_latest_briefing
from database import get_conn, now_iso
import call_reconciliation


def _fake_groq_client(response_text: str) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create.return_value.choices = [MagicMock(message=MagicMock(content=response_text))]
    return client


def _qualified_lead(budget="", authority="owner", timeline="soon", use_case="CRM"):
    lead = receive_lead(name="Recon Test", email=f"recon{id(object())}@example.com",
                         company="ReconCo", source="test", message="We need something.")
    with patch("qualify.extract_requirements", return_value={
        "use_case": use_case, "budget": budget, "authority": authority, "timeline": timeline, "notes": "",
    }):
        qualify_lead(lead["id"])
    score_lead(lead["id"])
    return lead["id"]


def _seed_briefing(lead_id: int, briefing_text: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO briefings (lead_id, briefing_text, requirements_snapshot, score_snapshot, created_at) "
            "VALUES (?, ?, '{}', 50, ?)",
            (lead_id, briefing_text, now_iso()),
        )


def test_reconcile_updates_only_fields_the_transcript_addressed():
    lead_id = _qualified_lead(budget="", authority="owner", timeline="soon")
    _seed_briefing(lead_id, "Budget unknown. Authority: owner. Timeline: soon.")

    fake_response = '{"discrepancies": [], "requirements_update": {"budget": "$2000/mo"}}'
    with patch("call_reconciliation.Groq", return_value=_fake_groq_client(fake_response)):
        result = call_reconciliation.add_and_reconcile_transcript(lead_id, "They said budget is $2000/month.")

    assert result["requirements_update"] == {"budget": "$2000/mo"}
    from qualify import get_requirements
    updated = get_requirements(lead_id)
    assert updated["budget"] == "$2000/mo"
    assert updated["authority"] == "owner"  # untouched field preserved
    assert updated["timeline"] == "soon"    # untouched field preserved


def test_reconcile_recomputes_score_deterministically_not_from_llm():
    """The LLM never sets a score directly — only flags what changed;
    scoring.py's pure function computes the actual number."""
    lead_id = _qualified_lead(budget="", authority="", timeline="")  # weak lead, low score
    _seed_briefing(lead_id, "Not much known yet.")

    fake_response = (
        '{"discrepancies": [], "requirements_update": '
        '{"budget": "$5000/mo", "authority": "CEO", "timeline": "asap"}}'
    )
    with patch("call_reconciliation.Groq", return_value=_fake_groq_client(fake_response)):
        result = call_reconciliation.add_and_reconcile_transcript(lead_id, "Turns out the CEO wants this ASAP, $5000/month budget.")

    # calculate_score would give budget(25)+authority(25, CEO matches decision-maker)+use_case(25)+timeline(25, urgent)
    # + rescore_lead's engagement bonus, capped at 100.
    assert result["score_after"] == 100
    assert result["score_after"] > (result["score_before"] or 0)


def test_reconcile_reports_discrepancies_from_llm():
    lead_id = _qualified_lead(budget="$500 total", authority="owner", timeline="soon")
    _seed_briefing(lead_id, "Budget: $500 total, one-time.")

    fake_response = (
        '{"discrepancies": ["Briefing assumed a $500 one-time budget; '
        'transcript reveals it\'s actually $500/month ongoing."], '
        '"requirements_update": {"budget": "$500/month ongoing"}}'
    )
    with patch("call_reconciliation.Groq", return_value=_fake_groq_client(fake_response)):
        result = call_reconciliation.add_and_reconcile_transcript(lead_id, "transcript text here")

    assert len(result["discrepancies"]) == 1
    assert "$500/month ongoing" in result["discrepancies"][0]


def test_reconcile_works_without_a_prior_briefing():
    """A lead nobody generated a briefing for shouldn't break reconciliation
    — it should just run the comparison with 'None available'."""
    lead_id = _qualified_lead()
    assert get_latest_briefing(lead_id) is None  # confirm no briefing exists

    fake_response = '{"discrepancies": [], "requirements_update": {}}'
    with patch("call_reconciliation.Groq", return_value=_fake_groq_client(fake_response)) as mock_groq:
        result = call_reconciliation.add_and_reconcile_transcript(lead_id, "Some transcript text.")

    assert result["transcript_id"] is not None
    sent_messages = mock_groq.return_value.chat.completions.create.call_args.kwargs["messages"]
    assert "None available" in sent_messages[1]["content"]


def test_reconcile_sanitizes_pii_in_transcript_before_sending_to_groq():
    lead_id = _qualified_lead()
    fake_response = '{"discrepancies": [], "requirements_update": {}}'
    fake_client = _fake_groq_client(fake_response)

    with patch("call_reconciliation.Groq", return_value=fake_client):
        call_reconciliation.add_and_reconcile_transcript(
            lead_id, "Sure, my email is personal@example.com if you need it."
        )

    sent_messages = fake_client.chat.completions.create.call_args.kwargs["messages"]
    sent_content = sent_messages[1]["content"]
    assert "personal@example.com" not in sent_content
    assert "[REDACTED_EMAIL]" in sent_content


def test_get_transcripts_for_lead_returns_stored_history():
    lead_id = _qualified_lead()
    fake_response = '{"discrepancies": [], "requirements_update": {}}'
    with patch("call_reconciliation.Groq", return_value=_fake_groq_client(fake_response)):
        call_reconciliation.add_and_reconcile_transcript(lead_id, "First call.")
        call_reconciliation.add_and_reconcile_transcript(lead_id, "Second call.")

    transcripts = call_reconciliation.get_transcripts_for_lead(lead_id)
    assert len(transcripts) == 2
    assert transcripts[0]["transcript_text"] == "Second call."  # newest first
    assert "score_after" in transcripts[0]["summary_json"]


def test_reconcile_raises_for_missing_lead():
    try:
        call_reconciliation.add_and_reconcile_transcript(999999, "transcript")
        assert False, "expected ValueError"
    except ValueError:
        pass