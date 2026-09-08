"""
research.py
V1 Step 11 — Basic company/person research (P1).

Per the blueprint: "Basic company/person research with evidence and timestamps."
This deliberately does NOT ask the LLM to "tell us about the company" — that
risks fabrication (hallucinated facts presented as real). Instead, it does a
real web search and stores the actual snippet + source URL + retrieval time,
so a salesperson can verify every finding themselves.

Uses the `ddgs` package (unofficial DuckDuckGo search wrapper, no API key
needed, genuinely free). Since it's an unofficial scraper of public search
results (not an official supported API), it can occasionally fail or get
rate-limited — this module fails soft (logs a note, doesn't crash the
pipeline) if that happens.
"""

from ddgs import DDGS

from database import get_conn, log_activity, now_iso
from leads import get_lead

MAX_RESULTS_PER_QUERY = 3


def _search(query: str) -> list[dict]:
    """
    Runs a web search and returns up to MAX_RESULTS_PER_QUERY results as
    [{"title": ..., "body": ..., "href": ...}, ...]. Returns [] on any failure
    (network issue, rate limit, etc.) rather than raising — research is a
    P1 nice-to-have, it should never block the core V1 pipeline.
    """
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=MAX_RESULTS_PER_QUERY))
        return results
    except Exception as e:
        print(f"[research] Search failed for query '{query}': {e}")
        return []


def _save_findings(lead_id: int, results: list[dict], source_label: str):
    timestamp = now_iso()
    with get_conn() as conn:
        for r in results:
            conn.execute(
                """
                INSERT INTO research_records (lead_id, source, finding, evidence_url, retrieved_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (lead_id, source_label, r.get("body", r.get("title", "")), r.get("href", ""), timestamp),
            )


def research_company(lead_id: int, company_name: str) -> list[dict]:
    """Searches for basic public info about a company. Returns the raw results found."""
    if not company_name.strip():
        return []
    results = _search(f"{company_name} company overview")
    _save_findings(lead_id, results, "web_search:company")
    return results


def research_contact(lead_id: int, person_name: str, company_name: str = "") -> list[dict]:
    """Searches for basic public info about the contact person. Returns the raw results found."""
    if not person_name.strip():
        return []
    query = f"{person_name} {company_name} linkedin".strip()
    results = _search(query)
    _save_findings(lead_id, results, "web_search:contact")
    return results


def research_lead(lead_id: int) -> dict:
    """
    Full Step 11 pipeline for a given lead: research the company, then the
    contact person. Always succeeds (soft-fails internally) since this is a
    P1 enrichment step, not a P0 gate — a lead should never get stuck here.
    Returns {"company_results": [...], "contact_results": [...]}.
    """
    lead = get_lead(lead_id)
    if lead is None:
        raise ValueError(f"Lead {lead_id} not found.")

    company_results = research_company(lead_id, lead.get("company") or "")
    contact_results = research_contact(lead_id, lead.get("name") or "", lead.get("company") or "")

    total_found = len(company_results) + len(contact_results)
    log_activity(lead_id, "research_completed", f"Found {total_found} result(s) (company: {len(company_results)}, contact: {len(contact_results)})")

    return {"company_results": company_results, "contact_results": contact_results}


def get_research(lead_id: int) -> list[dict]:
    """Fetch all stored research findings for a lead, most recent first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM research_records WHERE lead_id = ? ORDER BY retrieved_at DESC",
            (lead_id,),
        ).fetchall()
        return [dict(r) for r in rows]