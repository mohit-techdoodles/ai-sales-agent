"""
draft.py
V1 Step 5 — Draft outreach message.

Takes a lead's verified info + extracted requirements (from Step 3) and asks
the LLM to write a personalized outreach email. The draft is saved with
approval_status='pending' — it is NEVER sent automatically (see Step 6:
human approval, per the blueprint's V1 non-goal: no autonomous sending).
"""

import os
from groq import Groq
from dotenv import load_dotenv

from database import get_conn, log_activity, now_iso
from leads import get_lead
from qualify import get_requirements

load_dotenv()

MODEL = "openai/gpt-oss-120b"

SYSTEM_PROMPT = """You are a sales development rep writing a first outreach email to a lead \
who submitted an inquiry. Write ONLY based on the verified facts given to you — never invent \
details, statistics, or claims about the company/product that weren't provided.

CRITICAL: You have NOT been given any information about pricing, features, setup time, \
capabilities, or fit of our product/service. NEVER claim or imply that our offering matches \
their budget, timeline, or requirements — you don't know that. Only reference what THEY said \
about their own situation. Do not make promises on the company's behalf.

Tone: warm, professional, concise. No hard sell. Reference their specific stated need without \
claiming we can meet it. Keep the email under 120 words. End with a soft call-to-action inviting \
a conversation to learn more (e.g. suggest a short call to discuss their needs) — not a pitch.

Return ONLY a valid JSON object with exactly these keys:
- "subject": a short, specific email subject line (string)
- "body": the email body (string, plain text, no markdown)

Do not include any text before or after the JSON object. Do not use markdown code fences.
"""


def _build_context(lead: dict, requirements: dict) -> str:
    lines = [f"Lead name: {lead.get('name', '')}"]
    if lead.get("company"):
        lines.append(f"Company: {lead['company']}")
    if requirements.get("use_case"):
        lines.append(f"Stated need: {requirements['use_case']}")
    if requirements.get("budget"):
        lines.append(f"Budget mentioned: {requirements['budget']}")
    if requirements.get("timeline"):
        lines.append(f"Timeline: {requirements['timeline']}")
    if requirements.get("notes"):
        lines.append(f"Other notes: {requirements['notes']}")
    return "\n".join(lines)


def _call_llm(context: str) -> str:
    client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Verified facts about this lead:\n{context}"},
        ],
        temperature=0.4,  # a little creative latitude for natural-sounding copy
        max_tokens=1200,
        reasoning_effort="low",  # gpt-oss-120b "thinks" before answering; low keeps that brief
    )
    return response.choices[0].message.content.strip()


def _parse_json_response(raw_text: str) -> dict | None:
    import json

    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None

    if "subject" not in data or "body" not in data:
        return None

    return data


def generate_draft(lead_id: int, channel: str = "email") -> dict:
    """
    Full Step 5 pipeline for a given lead:
    1. Fetch lead + requirements (must already be qualified/scored).
    2. Call LLM to draft a personalized outreach message.
    3. Save the draft to the messages table with approval_status='pending'.
    4. Update lead status to 'drafted'.
    5. Log the activity.
    Returns {"message_id": int, "subject": str, "body": str}.
    """
    lead = get_lead(lead_id)
    if lead is None:
        raise ValueError(f"Lead {lead_id} not found.")

    requirements = get_requirements(lead_id) or {}

    context = _build_context(lead, requirements)
    raw = _call_llm(context)
    parsed = _parse_json_response(raw)

    if parsed is None:
        # Retry once with a stricter nudge, same pattern as qualify.py
        raw = _call_llm(context + "\n\nIMPORTANT: Respond with ONLY the raw JSON object.")
        parsed = _parse_json_response(raw)

    if parsed is None:
        raise RuntimeError(f"Could not parse valid JSON draft from LLM after retry. Last response:\n{raw}")

    timestamp = now_iso()
    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO messages (lead_id, channel, subject, body, approval_status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (lead_id, channel, parsed.get("subject", ""), parsed.get("body", ""), timestamp),
        )
        message_id = cursor.lastrowid

        conn.execute(
            "UPDATE leads SET status = 'drafted', updated_at = ? WHERE id = ?",
            (timestamp, lead_id),
        )

    log_activity(lead_id, "draft_created", f"Draft message_id={message_id} subject='{parsed.get('subject', '')}'")

    return {"message_id": message_id, "subject": parsed.get("subject", ""), "body": parsed.get("body", "")}


def get_message(message_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return dict(row) if row else None


def get_pending_messages_for_lead(lead_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE lead_id = ? AND approval_status = 'pending' ORDER BY created_at DESC",
            (lead_id,),
        ).fetchall()
        return [dict(r) for r in rows]

def list_all_messages() -> list[dict]:
    """Fetch every message across all leads — used for aggregate analytics (approval/rejection rate)."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM messages ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]    