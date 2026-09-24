"""
call_reconciliation.py
V6-B — Post-meeting CRM reconciliation (manual-entry version).

Original blueprint: connect a webhook to Recall.ai/Gong to pull transcripts
automatically, then compare the AI's pre-call briefing against the actual
transcript to detect discrepancies and update the score dynamically.

Manual version, by explicit choice (Streamlit Community Cloud can't host a
persistent webhook receiver, so real Recall.ai/Gong integration needs either
polling or a separate always-on service — punted for now): staff paste in
the transcript themselves after a call. Everything downstream — comparison,
scoring — works identically either way, so wiring up a real provider later
only means replacing how add_and_reconcile_transcript() gets called, not
rewriting this logic.

Design choice worth flagging: the LLM's job here is ONLY to read the
transcript and identify (a) discrepancies vs. the briefing and (b) which
requirement fields it explicitly updates — never to invent a score
directly. The score itself is recomputed by scoring.calculate_score(), the
same pure deterministic function used everywhere else, fed the MERGED
requirements. This keeps the same discipline as the rest of the codebase:
the LLM interprets language, plain Python decides the business outcome.
"""
import json
import os
import time
from groq import Groq
from dotenv import load_dotenv

from briefing import get_latest_briefing
from database import get_conn, log_activity, now_iso
from leads import get_lead
from pii_guard import sanitize_input, detect_prompt_injection
from qualify import get_requirements, save_requirements_update
from scoring import rescore_lead
from security_log import log_security_event

load_dotenv()

from model_tiers import MODEL_REASONING
MODEL = MODEL_REASONING  # comparing two documents for factual contradictions needs careful judgment

RECONCILIATION_SYSTEM_PROMPT = """You are reconciling a pre-call briefing against what was ACTUALLY \
said on a sales call, based ONLY on the transcript text given to you. Do not invent or infer \
anything not explicitly stated in the transcript.

You will be given:
1. The pre-call briefing (what we believed going into the call, or "None available" if there wasn't one)
2. The actual call transcript

Identify:
1. "discrepancies": specific facts from the briefing that the transcript CONTRADICTS or calls into \
question. Each should be a short plain-English string like "Briefing assumed a $1500 total budget; \
transcript reveals it's actually $500/month ongoing." If the transcript doesn't contradict anything \
in the briefing, return an empty list — do not invent discrepancies to fill the list.
2. "requirements_update": an object with any of use_case, budget, authority, timeline that the \
transcript reveals should be UPDATED. Only include a field if the transcript gives new or different \
information for it — if a field isn't addressed in the transcript at all, omit it entirely rather \
than guessing or repeating what was already known.

Respond with ONLY a JSON object in this exact shape, no markdown, no explanation:
{"discrepancies": ["..."], "requirements_update": {"use_case": "...", "budget": "...", "authority": "...", "timeline": "..."}}
"""


def _call_llm(briefing_text: str, transcript_text: str, lead_id: int) -> str:
    client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

    # V6-A: transcripts are exactly the kind of free text likely to contain
    # PII (someone reading out a phone number, an email, etc. on a call) —
    # same sanitization + injection screening + audit logging as every
    # other LLM call site.
    injection = detect_prompt_injection(transcript_text)
    sanitized_transcript = sanitize_input(transcript_text)
    sanitized_briefing = sanitize_input(briefing_text) if briefing_text else {"text": "None available.", "entities_found": []}

    user_content = (
        f"PRE-CALL BRIEFING:\n{sanitized_briefing['text']}\n\n"
        f"ACTUAL CALL TRANSCRIPT:\n{sanitized_transcript['text']}"
    )

    start = time.perf_counter()
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": RECONCILIATION_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0,
        max_tokens=1000,
        reasoning_effort="low",
    )
    latency_ms = int((time.perf_counter() - start) * 1000)

    all_entities = sorted(set(sanitized_transcript["entities_found"]) | set(sanitized_briefing["entities_found"]))
    log_security_event(lead_id, "call_reconciliation", MODEL, all_entities, injection["flagged"], latency_ms)

    return response.choices[0].message.content.strip()


def _parse_json_response(raw_text: str) -> dict | None:
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
    if "discrepancies" not in data or "requirements_update" not in data:
        return None
    return data


def add_and_reconcile_transcript(lead_id: int, transcript_text: str, meeting_id: int | None = None,
                                  duration_sec: int | None = None) -> dict:
    """
    Stores a call transcript and reconciles it against the lead's most
    recent persisted briefing (see briefing.get_latest_briefing — if none
    exists yet, the comparison just runs with "None available" instead of
    failing, so this still works for a lead nobody generated a briefing for).

    Returns:
        {"transcript_id": int, "discrepancies": list[str],
         "requirements_update": dict, "score_before": int|None, "score_after": int}

    Raises ValueError if the lead doesn't exist.
    """
    lead = get_lead(lead_id)
    if lead is None:
        raise ValueError(f"Lead {lead_id} not found.")

    briefing = get_latest_briefing(lead_id)
    briefing_text = briefing["briefing_text"] if briefing else ""
    score_before = lead.get("score")

    raw = _call_llm(briefing_text, transcript_text, lead_id)
    parsed = _parse_json_response(raw)
    if parsed is None:
        raw = _call_llm(
            briefing_text,
            transcript_text + "\n\n[IMPORTANT: Respond with ONLY the raw JSON object, no other text.]",
            lead_id,
        )
        parsed = _parse_json_response(raw)
    if parsed is None:
        raise RuntimeError(f"Could not parse valid JSON from reconciliation LLM after retry. Last response:\n{raw}")

    discrepancies = parsed.get("discrepancies") or []
    requirements_update = {k: v for k, v in (parsed.get("requirements_update") or {}).items() if v}

    # Merge: only overwrite fields the transcript actually addressed — keep
    # everything else from the existing understanding of this lead.
    existing_requirements = get_requirements(lead_id) or {}
    merged_requirements = {**existing_requirements, **requirements_update}

    if requirements_update:
        save_requirements_update(lead_id, merged_requirements)

    # Score is recomputed by the same deterministic scoring.py used
    # everywhere else — never set directly from the LLM's output.
    score_result = rescore_lead(lead_id)
    score_after = score_result["score"]

    timestamp = now_iso()
    summary = {
        "discrepancies": discrepancies,
        "requirements_update": requirements_update,
        "score_before": score_before,
        "score_after": score_after,
    }

    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO call_transcripts (lead_id, meeting_id, duration_sec, transcript_text, summary_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (lead_id, meeting_id, duration_sec, transcript_text, json.dumps(summary), timestamp),
        )
        transcript_id = cursor.lastrowid

    log_activity(
        lead_id, "call_reconciled",
        f"Transcript reconciled: {len(discrepancies)} discrepancy(ies), "
        f"score {score_before} -> {score_after}" + (f", updated: {list(requirements_update)}" if requirements_update else ""),
    )

    return {
        "transcript_id": transcript_id,
        "discrepancies": discrepancies,
        "requirements_update": requirements_update,
        "score_before": score_before,
        "score_after": score_after,
    }


def get_transcripts_for_lead(lead_id: int) -> list[dict]:
    """All reconciled transcripts for a lead, newest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM call_transcripts WHERE lead_id = ? ORDER BY created_at DESC", (lead_id,)
        ).fetchall()

    transcripts = []
    for row in rows:
        t = dict(row)
        try:
            t["summary_json"] = json.loads(t["summary_json"]) if t["summary_json"] else {}
        except json.JSONDecodeError:
            t["summary_json"] = {}
        transcripts.append(t)
    return transcripts