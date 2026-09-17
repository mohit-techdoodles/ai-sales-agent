"""
briefing.py
V3 — Sales briefing.

Compiles everything a salesperson needs before a call into one readable
summary: lead info, research findings, requirements + score, conversation
history, and any objections/questions raised. Assembled via LLM from real
data already in our system — never invents facts, only summarizes what's
actually on file (same grounding discipline as draft.py).
"""

import os
from groq import Groq
from dotenv import load_dotenv

from leads import get_lead
from qualify import get_requirements
from research import get_research
from opportunities import get_opportunity_for_lead
from calendar_booking import get_meetings_for_lead

load_dotenv()

from model_tiers import MODEL_REASONING
MODEL = MODEL_REASONING  # V4-B: briefing synthesis needs careful judgment, stays on the reasoning tier

BRIEFING_SYSTEM_PROMPT = """You are preparing a pre-call briefing for a salesperson, based ONLY on \
the verified facts given to you below. Do not invent or infer anything not explicitly stated.

Write a concise briefing with these sections:
1. **Who they are** — name, company, role/authority if known
2. **What they need** — their stated use case, budget, timeline
3. **Where things stand** — score and why, current stage
4. **Key conversation points** — anything notable they said (questions, concerns, objections, specific requests)
5. **Suggested talking points** — 2-3 things worth raising on the call, based ONLY on what's actually known

IMPORTANT: research findings come from free public web search with no identity verification — a
result may actually be about a different person or company that happens to share a similar name
(this has happened in practice). Any finding marked "⚠️ UNVERIFIED" must NOT be stated as fact —
mention it only as "unverified, needs checking" if at all. For other research findings, stay
cautious rather than presenting them as certain, especially for common names or generic company names.Keep it tight — a salesperson should be able to read this in under a minute before a call. \
Use plain text with clear section headers, no markdown tables.
"""


def _build_context(lead: dict, requirements: dict, research_findings: list[dict], thread: list[dict], opportunity: dict | None, meetings: list[dict], enrichment_data: dict | None = None) -> str:
    lines = [f"Lead: {lead.get('name', '')}"]
    if lead.get("company"):
        lines.append(f"Company: {lead['company']}")
    if lead.get("email"):
        lines.append(f"Email: {lead['email']}")
    if lead.get("phone"):
        lines.append(f"Phone: {lead['phone']}")

    lines.append(f"\nCurrent status: {lead.get('status', '')}")
    if lead.get("score") is not None:
        lines.append(f"Score: {lead['score']}/100")
    if lead.get("score_reasons"):
        lines.append(f"Score reasons: {lead['score_reasons']}")

    if requirements:
        lines.append("\nStated requirements:")
        for field in ("use_case", "budget", "authority", "timeline", "notes"):
            if requirements.get(field):
                lines.append(f"- {field}: {requirements[field]}")

    if enrichment_data:
        lines.append(f"\nCompany website ({enrichment_data.get('domain', '')}):")
        if enrichment_data.get("page_title"):
            lines.append(f"- Page title: {enrichment_data['page_title']}")
        if enrichment_data.get("meta_description"):
            lines.append(f"- Description: {enrichment_data['meta_description']}")
        if enrichment_data.get("tech_stack"):
            lines.append(f"- Detected tech stack: {', '.join(enrichment_data['tech_stack'])}")

    if research_findings:
        lines.append("\nResearch findings (from public web search):")
        for f in research_findings[:5]:
            lines.append(f"- {f['finding']} (source: {f['evidence_url']})")

    if opportunity:
        lines.append(f"\nOpportunity: stage={opportunity['stage']}, value=${opportunity.get('value') or 0}, probability={opportunity.get('probability') or 0}%")
        if opportunity.get("next_action"):
            lines.append(f"Next action on file: {opportunity['next_action']}")

    if meetings:
        upcoming = [m for m in meetings if m["status"] == "scheduled"]
        if upcoming:
            lines.append(f"\nUpcoming meeting: {upcoming[0]['start_at']}")

    if thread:
        lines.append("\nFull conversation so far (oldest first):")
        for msg in thread:
            speaker = lead.get("name", "Lead") if msg["direction"] == "inbound" else "Us"
            channel_tag = f" [{msg.get('channel', 'email')}]" if msg.get("channel") else ""
            lines.append(f"[{speaker}]{channel_tag}: {msg['body']}")

    return "\n".join(lines)


def generate_briefing(lead_id: int) -> str:
    """
    Compiles a pre-call briefing for a lead. Returns plain-text briefing.
    Raises ValueError if the lead doesn't exist.
    """
    # Local imports to avoid circular imports at module load time.
    from replies import get_conversation
    from enrichment import get_company_enrichment

    lead = get_lead(lead_id)
    if lead is None:
        raise ValueError(f"Lead {lead_id} not found.")

    requirements = get_requirements(lead_id) or {}
    research_findings = get_research(lead_id)
    thread = get_conversation(lead_id)
    opportunity = get_opportunity_for_lead(lead_id)
    meetings = get_meetings_for_lead(lead_id)
    enrichment_data = get_company_enrichment(lead_id)

    context = _build_context(lead, requirements, research_findings, thread, opportunity, meetings, enrichment_data)

    client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": BRIEFING_SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ],
        temperature=0.2,
        max_tokens=1200,
        reasoning_effort="low",
    )

    return response.choices[0].message.content.strip()