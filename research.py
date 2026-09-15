"""
research.py
V1 Step 11 — Basic company/person research (P1).
V3 — Deeper research: multi-source queries + freshness checks (P1).

Per the blueprint: "Multi-source research with evidence/freshness checks."
This deliberately does NOT ask the LLM to "tell us about the company" — that
risks fabrication (hallucinated facts presented as real). Instead, it does
several REAL, targeted web searches and stores the actual snippet + source
URL + retrieval time, so a salesperson can verify every finding themselves.

"Multi-source" here means several distinct search ANGLES (company overview,
recent news, contact's public presence, and — when the lead's email domain
looks like a real business, not gmail/yahoo/etc — a domain-targeted search),
since a single free-tier search provider (ddgs / DuckDuckGo) is what's
available without a paid API. Each angle is tagged separately so a
salesperson can see where each finding came from.

Uses the `ddgs` package (unofficial DuckDuckGo search wrapper, no API key
needed, genuinely free). Since it's an unofficial scraper of public search
results (not an official supported API), it can occasionally fail or get
rate-limited — this module fails soft (logs a note, doesn't crash the
pipeline) if that happens.
"""

import os
from datetime import datetime, timezone

from ddgs import DDGS

from database import get_conn, log_activity, now_iso
from leads import get_lead

MAX_RESULTS_PER_QUERY = 3
RESEARCH_STALE_DAYS = float(os.environ.get("RESEARCH_STALE_DAYS", "30"))

FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
    "aol.com", "protonmail.com", "live.com", "mail.com",
}


def _search(query: str) -> list[dict]:
    """
    Runs a web search and returns up to MAX_RESULTS_PER_QUERY results as
    [{"title": ..., "body": ..., "href": ...}, ...]. Returns [] on any failure
    (network issue, rate limit, etc.) rather than raising — research is a
    P1 nice-to-have, it should never block the core pipeline.
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


def _extract_business_domain(email: str) -> str | None:
    """Returns the email's domain if it looks like a real company (not a free consumer email provider)."""
    if not email or "@" not in email:
        return None
    domain = email.split("@")[-1].strip().lower()
    if not domain or domain in FREE_EMAIL_DOMAINS:
        return None
    return domain


def research_company(lead_id: int, company_name: str) -> list[dict]:
    """Searches for basic public info about a company. Returns the raw results found."""
    if not company_name.strip():
        return []
    results = _search(f"{company_name} company overview")
    _save_findings(lead_id, results, "web_search:company")
    return results


def research_company_news(lead_id: int, company_name: str) -> list[dict]:
    """V3 — a second angle: recent news/updates, so research isn't limited to a static 'about' snapshot."""
    if not company_name.strip():
        return []
    results = _search(f"{company_name} news recent")
    _save_findings(lead_id, results, "web_search:news")
    return results


def research_company_website(lead_id: int, domain: str) -> list[dict]:
    """V3 — a third angle: when the lead's email domain looks like a real business, search it directly."""
    if not domain:
        return []
    results = _search(domain)
    _save_findings(lead_id, results, "web_search:website")
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
    Full research pipeline for a given lead: company overview, company
    news, contact, and (if applicable) their business domain directly.
    Always succeeds (soft-fails internally) since this is a P1 enrichment
    step, not a P0 gate — a lead should never get stuck here.
    Returns counts per source angle.
    """
    lead = get_lead(lead_id)
    if lead is None:
        raise ValueError(f"Lead {lead_id} not found.")

    company_name = lead.get("company") or ""
    person_name = lead.get("name") or ""
    domain = _extract_business_domain(lead.get("email") or "")

    company_results = research_company(lead_id, company_name)
    news_results = research_company_news(lead_id, company_name)
    contact_results = research_contact(lead_id, person_name, company_name)
    website_results = research_company_website(lead_id, domain) if domain else []

    total_found = len(company_results) + len(news_results) + len(contact_results) + len(website_results)
    log_activity(
        lead_id,
        "research_completed",
        f"Found {total_found} result(s) across {4 if domain else 3} source angles "
        f"(company: {len(company_results)}, news: {len(news_results)}, contact: {len(contact_results)}, website: {len(website_results)})",
    )

    return {
        "company_results": company_results,
        "news_results": news_results,
        "contact_results": contact_results,
        "website_results": website_results,
    }


def get_research(lead_id: int) -> list[dict]:
    """Fetch all stored research findings for a lead, most recent first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM research_records WHERE lead_id = ? ORDER BY retrieved_at DESC",
            (lead_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_research_freshness(lead_id: int) -> dict:
    """
    V3 — freshness check: how old is the most recent research for this lead,
    and is it past the staleness threshold (RESEARCH_STALE_DAYS, default 30).
    Returns {"has_research": bool, "days_old": float|None, "is_stale": bool}.
    """
    findings = get_research(lead_id)
    if not findings:
        return {"has_research": False, "days_old": None, "is_stale": True}

    latest = findings[0]["retrieved_at"]  # already ordered most-recent-first
    then = datetime.fromisoformat(latest)
    now = datetime.now(timezone.utc)
    days_old = (now - then).total_seconds() / 86400

    return {"has_research": True, "days_old": round(days_old, 1), "is_stale": days_old >= RESEARCH_STALE_DAYS}