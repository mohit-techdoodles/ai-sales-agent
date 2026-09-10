"""
draft.py
V1 Step 5 — Draft outreach message.
V2 Step 2 — Reply-aware drafting + opt-out block.
V2 Step 4 — Follow-up "nudge" drafting mode.

Three drafting modes, chosen automatically based on the lead's history:
1. INITIAL — no prior conversation: first outreach from verified requirements.
2. FOLLOWUP (reply) — lead has replied: continue the actual thread.
3. NUDGE — we've sent something, they haven't replied, time's passed:
   a brief, polite check-in (not a repeat of the original pitch).

Hard rule: never drafts anything for a lead who has opted out (blueprint's
V2 guardrail — "stop follow-ups on ... opt-out"). Enforced here AND again
at send time in approval.py, as defense in depth.
"""

import os
from groq import Groq
from dotenv import load_dotenv

from database import get_conn, log_activity, now_iso
from leads import get_lead
from qualify import get_requirements

load_dotenv()

MODEL = "openai/gpt-oss-120b"

INITIAL_SYSTEM_PROMPT = """You are a sales development rep writing a first outreach email to a lead \
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

FOLLOWUP_SYSTEM_PROMPT = """You are a sales development rep continuing an email conversation with a \
lead who has already replied. You'll be given the full conversation thread so far (oldest first).

Write a natural, direct reply to what they MOST RECENTLY said. Do NOT re-introduce yourself, do NOT \
repeat the original pitch, and do NOT summarize the whole conversation back to them — just respond \
like a human would to the latest message in an ongoing email thread.

CRITICAL: You have NOT been given any information about pricing, features, setup time, capabilities, \
or fit of our product/service. NEVER claim or imply that our offering matches their budget, timeline, \
or requirements, and never invent facts. If they asked a specific question you can't verify the answer \
to, acknowledge it honestly and offer to find out / connect them with someone who can confirm, rather \
than guessing.

Tone: warm, professional, concise — like a real reply, not a template. Keep it under 100 words.

Return ONLY a valid JSON object with exactly these keys:
- "subject": the reply subject line, typically "Re: <original subject>" (string)
- "body": the email body (string, plain text, no markdown)

Do not include any text before or after the JSON object. Do not use markdown code fences.
"""

NUDGE_SYSTEM_PROMPT = """You are a sales development rep sending a brief, polite follow-up to a lead \
who has NOT replied to a previous message. You'll be given their original stated needs and the \
message(s) already sent to them.

Do NOT repeat the full original pitch. Do NOT re-explain what was already said. Just a short, \
friendly check-in — acknowledge you haven't heard back, briefly restate genuine interest in helping \
with what they mentioned, and make it easy for them to reply (e.g. "just let me know if now isn't a \
good time" is fine too — don't be pushy).

CRITICAL: You have NOT been given any information about pricing, features, setup time, capabilities, \
or fit of our product/service. Never invent facts or claims about our offering.

Tone: warm, low-pressure, brief. Keep it under 60 words — this is a nudge, not a pitch.

Return ONLY a valid JSON object with exactly these keys:
- "subject": the follow-up subject line, typically "Re: <original subject>" (string)
- "body": the email body (string, plain text, no markdown)

Do not include any text before or after the JSON object. Do not use markdown code fences.
"""

MEETING_CONFIRMATION_SYSTEM_PROMPT = """You are a sales development rep confirming a meeting that was \
just scheduled with a lead. You'll be given the confirmed date/time, an optional video call link, and \
the conversation so far.

Write a short, warm confirmation message. Reference the specific confirmed time. Include the video \
call link if one was given. Do not repeat the whole prior conversation. Do not invent any details \
about pricing, features, or capabilities that weren't already discussed.

Tone: friendly, concise, professional. Keep it under 70 words.

Return ONLY a valid JSON object with exactly these keys:
- "subject": the email subject line, e.g. "Confirmed: our call on <date>" (string)
- "body": the email body (string, plain text, no markdown)

Do not include any text before or after the JSON object. Do not use markdown code fences.
"""


def _build_initial_context(lead: dict, requirements: dict) -> str:
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


def _build_conversation_context(lead: dict, thread: list[dict]) -> str:
    lines = [f"Lead name: {lead.get('name', '')}"]
    if lead.get("company"):
        lines.append(f"Company: {lead['company']}")
    lines.append("\nConversation so far (oldest first):")
    for msg in thread:
        speaker = lead.get("name", "Lead") if msg["direction"] == "inbound" else "Us"
        lines.append(f"[{speaker}]: {msg['body']}")
    return "\n".join(lines)


def _build_nudge_context(lead: dict, requirements: dict, thread: list[dict]) -> str:
    lines = [f"Lead name: {lead.get('name', '')}"]
    if lead.get("company"):
        lines.append(f"Company: {lead['company']}")
    if requirements.get("use_case"):
        lines.append(f"Their stated need: {requirements['use_case']}")
    lines.append("\nMessage(s) already sent to them (don't repeat verbatim):")
    for msg in thread:
        if msg["direction"] == "outbound":
            lines.append(f"- {msg['body']}")
    return "\n".join(lines)


def _build_meeting_confirmation_context(lead: dict, thread: list[dict], meeting_time_display: str, meet_link: str) -> str:
    lines = [f"Lead name: {lead.get('name', '')}"]
    if lead.get("company"):
        lines.append(f"Company: {lead['company']}")
    lines.append(f"\nConfirmed meeting time: {meeting_time_display}")
    if meet_link:
        lines.append(f"Video call link: {meet_link}")
    if thread:
        lines.append("\nRecent conversation (for context, don't repeat it):")
        for msg in thread[-4:]:  # just the last few messages for context
            speaker = lead.get("name", "Lead") if msg["direction"] == "inbound" else "Us"
            lines.append(f"[{speaker}]: {msg['body']}")
    return "\n".join(lines)


def _call_llm(system_prompt: str, context: str) -> str:
    # V2 Templates (P1): staff-configured brand voice/positioning rules,
    # applied here so every drafting mode picks them up automatically.
    from settings import get_setting
    guidelines = get_setting("brand_guidelines", "").strip()
    if guidelines:
        system_prompt = system_prompt + f"\n\nCompany brand voice and messaging rules to follow:\n{guidelines}"

    client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": context},
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


def generate_draft(lead_id: int, channel: str = None) -> dict:
    """
    Drafts an outreach message for a lead. Automatically picks the mode:
    - No prior conversation at all -> INITIAL (from requirements).
    - Lead has replied -> FOLLOWUP (continue the actual thread).
    - We've sent something, no reply yet -> NUDGE (brief check-in).

    Channel: if not explicitly given, auto-detects from the most recent
    message in the thread (so a conversation that moved to Telegram stays
    on Telegram). Defaults to 'email' for a brand new lead with no thread
    yet (we can't use Telegram until the lead has linked their chat_id).

    Hard-blocks drafting for opted-out leads (V2 guardrail).

    Returns {"message_id": int, "subject": str, "body": str}.
    """
    # Imported here (not at module top) to avoid a circular import, since
    # replies.py imports this module at the top level.
    from replies import get_conversation

    lead = get_lead(lead_id)
    if lead is None:
        raise ValueError(f"Lead {lead_id} not found.")

    if lead.get("status") == "opted_out":
        raise ValueError(f"Lead {lead_id} has opted out — cannot generate a draft for them.")

    thread = get_conversation(lead_id)
    has_replied = any(m["direction"] == "inbound" for m in thread)
    has_outbound = any(m["direction"] == "outbound" for m in thread)

    if channel is None:
        channel = thread[-1]["channel"] if thread else "email"

    if has_replied:
        mode = "followup"
        system_prompt = FOLLOWUP_SYSTEM_PROMPT
        context = _build_conversation_context(lead, thread)
    elif has_outbound:
        mode = "nudge"
        requirements = get_requirements(lead_id) or {}
        system_prompt = NUDGE_SYSTEM_PROMPT
        context = _build_nudge_context(lead, requirements, thread)
    else:
        mode = "initial"
        system_prompt = INITIAL_SYSTEM_PROMPT
        requirements = get_requirements(lead_id) or {}
        context = _build_initial_context(lead, requirements)

    raw = _call_llm(system_prompt, context)
    parsed = _parse_json_response(raw)

    if parsed is None:
        # Retry once with a stricter nudge, same pattern as qualify.py
        raw = _call_llm(system_prompt, context + "\n\nIMPORTANT: Respond with ONLY the raw JSON object.")
        parsed = _parse_json_response(raw)

    if parsed is None:
        raise RuntimeError(f"Could not parse valid JSON draft from LLM after retry. Last response:\n{raw}")

    timestamp = now_iso()
    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO messages (lead_id, channel, direction, subject, body, approval_status, created_at)
            VALUES (?, ?, 'outbound', ?, ?, 'pending', ?)
            """,
            (lead_id, channel, parsed.get("subject", ""), parsed.get("body", ""), timestamp),
        )
        message_id = cursor.lastrowid

        conn.execute(
            "UPDATE leads SET status = 'drafted', updated_at = ? WHERE id = ?",
            (timestamp, lead_id),
        )

    log_activity(
        lead_id,
        "draft_created",
        f"Draft message_id={message_id} (mode={mode}) subject='{parsed.get('subject', '')}'",
    )

    return {"message_id": message_id, "subject": parsed.get("subject", ""), "body": parsed.get("body", "")}


def generate_meeting_confirmation_draft(lead_id: int, meeting_time_display: str, meet_link: str = "") -> dict:
    """
    V2 — Called automatically right after a meeting is booked (calendar_booking.py
    sends the raw calendar invite directly, which bypasses our approval flow —
    this generates a proper confirmation message that DOES go through the normal
    Approval Inbox, so a human still reviews what's said to the lead).
    """
    from replies import get_conversation

    lead = get_lead(lead_id)
    if lead is None:
        raise ValueError(f"Lead {lead_id} not found.")

    if lead.get("status") == "opted_out":
        raise ValueError(f"Lead {lead_id} has opted out — cannot generate a draft for them.")

    thread = get_conversation(lead_id)
    context = _build_meeting_confirmation_context(lead, thread, meeting_time_display, meet_link)

    raw = _call_llm(MEETING_CONFIRMATION_SYSTEM_PROMPT, context)
    parsed = _parse_json_response(raw)

    if parsed is None:
        raw = _call_llm(MEETING_CONFIRMATION_SYSTEM_PROMPT, context + "\n\nIMPORTANT: Respond with ONLY the raw JSON object.")
        parsed = _parse_json_response(raw)

    if parsed is None:
        raise RuntimeError(f"Could not parse valid JSON draft from LLM after retry. Last response:\n{raw}")

    timestamp = now_iso()
    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO messages (lead_id, channel, direction, subject, body, approval_status, created_at)
            VALUES (?, 'email', 'outbound', ?, ?, 'pending', ?)
            """,
            (lead_id, parsed.get("subject", ""), parsed.get("body", ""), timestamp),
        )
        message_id = cursor.lastrowid
        # Note: does NOT change lead.status to 'drafted' — the lead stays 'replied'
        # so the Conversations tab keeps showing the meeting-booking UI correctly.

    log_activity(lead_id, "draft_created", f"Meeting confirmation draft message_id={message_id}")

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


def get_lead_ids_with_pending_messages() -> list[int]:
    """
    Lead IDs with at least one pending outbound draft. Used by the Approval
    Inbox instead of filtering by lead.status == 'drafted', since some
    drafts (e.g. meeting confirmations) are deliberately created without
    changing the lead's status.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT lead_id FROM messages WHERE approval_status = 'pending' AND direction = 'outbound'"
        ).fetchall()
        return [r["lead_id"] for r in rows]