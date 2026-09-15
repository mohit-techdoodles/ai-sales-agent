"""
tests/test_grounding.py

Regression tests for the blueprint's grounding principle: "Ground factual
claims in verified data. Never fabricate research."

Covers:
- research.py: every stored finding must carry verifiable evidence (a
  source URL + retrieval timestamp), staleness must be computed correctly,
  and a failed search must never crash the pipeline.
- draft.py: the anti-fabrication instructions in the LLM prompts must stay
  present verbatim — a prompt-contract test, so an edit to these prompts
  can't silently drop the safety rule without a test failing.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import draft
import research
from leads import receive_lead
from database import get_conn


def _lead(email="jane@acmewidgets.com"):
    return receive_lead(name="Grounding Test", email=email, company="AcmeWidgets", source="test")


# ---------------------------------------------------------------------------
# research.py — every finding must be traceable to a real source
# ---------------------------------------------------------------------------

def test_business_domain_extraction_ignores_free_email_providers():
    assert research._extract_business_domain("person@gmail.com") is None
    assert research._extract_business_domain("person@acmewidgets.com") == "acmewidgets.com"
    assert research._extract_business_domain("") is None
    assert research._extract_business_domain("not-an-email") is None


def test_research_lead_stores_evidence_for_every_finding():
    lead = _lead()
    fake_results = [{"title": "Acme Widgets", "body": "Acme makes widgets.", "href": "https://acme.example/about"}]

    with patch("research._search", return_value=fake_results):
        research.research_lead(lead["id"])

    findings = research.get_research(lead["id"])
    assert findings, "expected at least one stored finding"
    for f in findings:
        # Grounding requirement: no finding may exist without a verifiable
        # source URL and a retrieval timestamp — that's what lets a
        # salesperson (or a human reviewer) check the claim themselves.
        assert f["evidence_url"], f"finding stored without evidence_url: {f}"
        assert f["retrieved_at"], f"finding stored without a retrieval timestamp: {f}"
        assert f["finding"]


def test_research_lead_never_crashes_when_every_search_angle_is_empty():
    lead = _lead()
    with patch("research._search", return_value=[]):
        result = research.research_lead(lead["id"])
    assert result["company_results"] == []
    assert research.get_research(lead["id"]) == []


def test_research_freshness_flags_stale_findings():
    lead = _lead()
    old_timestamp = (
        datetime.now(timezone.utc) - timedelta(days=research.RESEARCH_STALE_DAYS + 5)
    ).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO research_records (lead_id, source, finding, evidence_url, retrieved_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (lead["id"], "web_search:company", "old finding", "https://acme.example", old_timestamp),
        )

    freshness = research.get_research_freshness(lead["id"])
    assert freshness["has_research"] is True
    assert freshness["is_stale"] is True


def test_research_freshness_with_no_research_counts_as_stale():
    lead = _lead(email="new@acmewidgets.com")
    assert research.get_research_freshness(lead["id"]) == {
        "has_research": False, "days_old": None, "is_stale": True,
    }


# ---------------------------------------------------------------------------
# draft.py — the anti-fabrication instruction must stay in every prompt
# ---------------------------------------------------------------------------

def test_initial_prompt_forbids_inventing_facts():
    prompt = draft.INITIAL_SYSTEM_PROMPT
    assert "never invent" in prompt.lower()
    assert "NEVER claim or imply" in prompt


def test_followup_prompt_enforces_escalation_over_guessing():
    prompt = draft.FOLLOWUP_SYSTEM_PROMPT
    assert "do NOT guess or invent" in prompt
    assert "needs_escalation" in prompt


def test_nudge_prompt_forbids_inventing_facts():
    assert "Never invent facts" in draft.NUDGE_SYSTEM_PROMPT