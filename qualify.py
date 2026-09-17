"""
qualify.py
V1 Step 3 (+ Step 8 interactive follow-up) — AI qualification.

Extracts structured requirement fields (use_case, budget, authority, timeline)
from a lead's message via Groq. If key fields are missing, asks ONE follow-up
question (a deterministic template, not another LLM call — fast, free, and
always well-formed) and pauses the lead in 'awaiting_info' status until they
reply. Capped at MAX_FOLLOWUP_ROUNDS so a lead is never endlessly pestered.

Design notes:
- The LLM ONLY extracts/interprets language. It does not decide business
  logic (that's Step 4's job — plain Python scoring) or which question to
  ask next (plain Python templates here, per the blueprint's principle that
  the LLM shouldn't control decisions better expressed as explicit rules).
"""

import json
import os
from groq import Groq
from dotenv import load_dotenv

from database import get_conn, log_activity, now_iso
from leads import get_lead

load_dotenv()

from model_tiers import MODEL_LIGHT
MODEL = MODEL_LIGHT  # V4-B: structured extraction + question selection are "light" tasks per hierarchical routing  # current Groq production model, good for structured extraction
# Note: llama-3.3-70b-versatile was decommissioned by Groq on Aug 16, 2026.
# If this model is ever retired too, check https://console.groq.com/docs/models

REQUIRED_FIELDS = ["use_case", "budget", "authority", "timeline"]

# Priority order for which missing field to ask about first (need-to-know order).
FOLLOWUP_PRIORITY = ["use_case", "authority", "budget", "timeline"]

FOLLOWUP_QUESTIONS = {
    "use_case": "Could you tell me a bit more about what you're looking to use this for?",
    "authority": "Are you the one who'd be making the final decision on this, or evaluating it for someone else?",
    "budget": "Do you have an approximate budget in mind for this?",
    "timeline": "What's your timeline for getting this in place?",
}

MAX_FOLLOWUP_ROUNDS = int(os.environ.get("FOLLOWUP_MAX_ROUNDS", "2"))  # V3: raised from 1, agent can ask more than once

DYNAMIC_QUESTION_SYSTEM_PROMPT = """You are qualifying a sales lead. You'll be given what's already \
known about them and a list of fields still missing: use_case, budget, authority, timeline.

Choose the SINGLE most valuable missing field to ask about next — the one whose answer would most \
help a salesperson understand if this is a good-fit, ready-to-buy lead. Then phrase one natural, \
low-effort, friendly question to get that specific piece of information. Reference what they've \
already told you if relevant, so it doesn't feel like a generic form question.

Return ONLY a valid JSON object with exactly these keys:
- "targeting_field": one of "use_case", "budget", "authority", "timeline" (must be one of the missing fields given)
- "question": the question to ask (string)

Do not include any text before or after the JSON object. Do not use markdown code fences.
"""

SYSTEM_PROMPT = """You are a sales qualification assistant. Extract structured information \
from what a lead has said about their needs.

Return ONLY a valid JSON object with exactly these keys:
- "use_case": what they need/want to use the product for (string, or "" if not mentioned)
- "budget": any budget or price range mentioned (string, or "" if not mentioned)
- "authority": whether they sound like the decision-maker, e.g. "owner", "manager", \
"evaluating for someone else", or "" if unclear
- "timeline": when they need this by (string, or "" if not mentioned)
- "notes": any other relevant detail worth a salesperson knowing (string, can be "")

Do not include any text before or after the JSON object. Do not use markdown code fences.
If a field isn't mentioned, use an empty string "" — do not guess or invent details.
"""


def _call_llm(lead_message: str, extra_context: str = "") -> str:
    """Raw call to Groq. Returns the model's text response."""
    client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

    user_prompt = f"Lead's message / notes:\n{lead_message}\n"
    if extra_context:
        user_prompt += f"\nAdditional context:\n{extra_context}\n"

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,  # deterministic extraction, not creative writing
        max_tokens=1200,
        reasoning_effort="low",  # gpt-oss-120b "thinks" before answering; low keeps that brief
    )
    return response.choices[0].message.content.strip()


def _parse_json_response(raw_text: str, required_fields: list[str] = None) -> dict | None:
    """Try to parse the model's response as JSON. Strips markdown fences if present."""
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

    fields_to_check = required_fields if required_fields is not None else REQUIRED_FIELDS
    if not all(field in data for field in fields_to_check):
        return None

    return data


def extract_requirements(lead_message: str, extra_context: str = "") -> dict:
    """
    Calls the LLM to extract structured fields from a lead's message.
    Retries once with a stricter reminder if the first response isn't valid JSON.
    Returns a dict with use_case, budget, authority, timeline, notes.
    Raises RuntimeError if both attempts fail to produce valid JSON.
    """
    raw = _call_llm(lead_message, extra_context)
    parsed = _parse_json_response(raw)

    if parsed is None:
        retry_message = (
            lead_message
            + "\n\nIMPORTANT: Respond with ONLY the raw JSON object. No explanation, no markdown."
        )
        raw = _call_llm(retry_message, extra_context)
        parsed = _parse_json_response(raw)

    if parsed is None:
        raise RuntimeError(f"Could not parse valid JSON from LLM after retry. Last response:\n{raw}")

    return parsed


def get_missing_fields(requirements: dict) -> list[str]:
    """Returns required fields that are still empty, in priority order."""
    return [f for f in FOLLOWUP_PRIORITY if not (requirements.get(f) or "").strip()]


def _call_llm_with_prompt(system_prompt: str, user_content: str) -> str:
    """Like _call_llm but with a caller-supplied system prompt (used for non-extraction calls, e.g. dynamic question selection)."""
    client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=0.3,
        max_tokens=600,
        reasoning_effort="low",
    )
    return response.choices[0].message.content.strip()


def choose_followup_question(requirements: dict, missing: list[str]) -> dict:
    """
    V3 — dynamic qualification: asks the LLM to pick the single most
    valuable missing field and phrase a natural, contextual question for
    it, rather than always following a fixed priority order + template.

    The DECISION of what's missing (get_missing_fields) stays deterministic
    business logic; only WHICH one to prioritize and HOW to phrase it is
    delegated to the LLM here — a conversational choice, not a business
    rule like scoring or approval.

    Falls back to the fixed template (old V1/V2 behavior) if the LLM call
    fails or returns something unusable, so this never breaks the pipeline.
    """
    known = {k: v for k, v in requirements.items() if v}
    context = f"Known so far: {known}\nMissing fields: {missing}"

    try:
        raw = _call_llm_with_prompt(DYNAMIC_QUESTION_SYSTEM_PROMPT, context)
        parsed = _parse_json_response(raw, required_fields=["targeting_field", "question"])
        if parsed and parsed.get("targeting_field") in missing and parsed.get("question"):
            return {"question": parsed["question"], "targeting_field": parsed["targeting_field"]}
    except Exception as e:
        print(f"[qualify] Dynamic question selection failed, using fallback template: {e}")

    # Fallback: fixed priority order + canned template (always works, never fails to parse)
    fallback_field = missing[0]
    return {"question": FOLLOWUP_QUESTIONS[fallback_field], "targeting_field": fallback_field}


def _save_requirements(lead_id: int, requirements: dict, timestamp: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO lead_requirements (lead_id, use_case, budget, authority, timeline, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lead_id,
                requirements.get("use_case", ""),
                requirements.get("budget", ""),
                requirements.get("authority", ""),
                requirements.get("timeline", ""),
                requirements.get("notes", ""),
                timestamp,
            ),
        )


def qualify_lead(lead_id: int) -> dict:
    """
    Full Step 3+8 pipeline for a given lead:
    1. Read the lead's current message from the database (includes any
       follow-up answers already appended by submit_followup_answer()).
    2. Extract structured requirements via LLM.
    3. Save them to lead_requirements table (keeps full history).
    4. If key fields are still missing AND we haven't used up our follow-up
       round, pause the lead in 'awaiting_info' with a pending_question.
       Otherwise, mark status 'qualifying' (ready for Step 4 scoring).
    5. Log the activity.

    Returns {"status": "awaiting_info" | "qualified", "requirements": dict, "question": str | None}
    """
    lead = get_lead(lead_id)
    if lead is None:
        raise ValueError(f"Lead {lead_id} not found.")

    requirements = extract_requirements(lead.get("message", "") or "")
    timestamp = now_iso()
    _save_requirements(lead_id, requirements, timestamp)

    missing = get_missing_fields(requirements)
    followup_rounds = lead.get("followup_rounds", 0) or 0

    if missing and followup_rounds < MAX_FOLLOWUP_ROUNDS:
        chosen = choose_followup_question(requirements, missing)
        question = chosen["question"]
        with get_conn() as conn:
            conn.execute(
                """
                UPDATE leads
                SET status = 'awaiting_info', pending_question = ?, followup_rounds = followup_rounds + 1, updated_at = ?
                WHERE id = ?
                """,
                (question, timestamp, lead_id),
            )
        log_activity(lead_id, "followup_requested", f"Asked about '{chosen['targeting_field']}' (missing: {missing}): '{question}'")
        return {"status": "awaiting_info", "requirements": requirements, "question": question}

    with get_conn() as conn:
        conn.execute(
            "UPDATE leads SET status = 'qualifying', pending_question = NULL, updated_at = ? WHERE id = ?",
            (timestamp, lead_id),
        )
    log_activity(lead_id, "qualified", f"Extracted requirements: {requirements}")
    return {"status": "qualified", "requirements": requirements, "question": None}


def submit_followup_answer(lead_id: int, answer_text: str) -> dict:
    """
    Appends the lead's reply to their stored message and re-runs qualification.
    Clears the pending_question. Since followup_rounds was already incremented
    when the question was asked, this second pass will proceed to 'qualifying'
    even if a field is still missing (we only ask once, per MAX_FOLLOWUP_ROUNDS).
    """
    lead = get_lead(lead_id)
    if lead is None:
        raise ValueError(f"Lead {lead_id} not found.")

    combined_message = (lead.get("message") or "") + f"\n\nFollow-up answer: {answer_text}"
    timestamp = now_iso()

    with get_conn() as conn:
        conn.execute(
            "UPDATE leads SET message = ?, pending_question = NULL, updated_at = ? WHERE id = ?",
            (combined_message, timestamp, lead_id),
        )

    log_activity(lead_id, "followup_answered", f"Lead replied: {answer_text}")

    return qualify_lead(lead_id)


def reextract_from_reply(lead_id: int) -> dict:
    """
    V2 Step 3 — re-runs requirement extraction after a reply comes in.

    IMPORTANT: builds context from the ENTIRE conversation thread (original
    message + every message since, both directions) — not just the latest
    reply. Using only the newest reply would silently forget facts stated
    in earlier turns (e.g. a timeline mentioned in reply #1 would vanish
    from scoring the moment reply #2 arrived without repeating it).

    Unlike qualify_lead(), this does NOT touch lead.status — that field is
    used by the Conversations tab to track 'replied'/'opted_out', and we
    don't want to reset it back to 'qualifying'. Saves a new
    lead_requirements row (keeping history). Returns the updated requirements.
    """
    # Local import to avoid a circular import: replies.py imports this
    # module at the top level, so this module must only reach back into
    # replies.py lazily, at call time (by which point both are fully loaded).
    from replies import get_conversation

    lead = get_lead(lead_id)
    if lead is None:
        raise ValueError(f"Lead {lead_id} not found.")

    thread = get_conversation(lead_id)
    lines = [lead.get("message", "") or ""]
    for msg in thread:
        speaker = "Lead" if msg["direction"] == "inbound" else "Us"
        lines.append(f"{speaker}: {msg['body']}")
    combined_message = "\n\n".join(lines)

    requirements = extract_requirements(combined_message)
    timestamp = now_iso()
    _save_requirements(lead_id, requirements, timestamp)

    log_activity(lead_id, "reextracted_from_reply", f"Updated requirements from full thread: {requirements}")

    return requirements


def get_requirements(lead_id: int) -> dict | None:
    """Fetch the most recent extracted requirements for a lead."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM lead_requirements WHERE lead_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (lead_id,),
        ).fetchone()
        return dict(row) if row else None