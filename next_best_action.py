"""
next_best_action.py
V3 — Next-best-action.

Looks at a lead's actual state (status, conversation content, pending
drafts, time since contact) and recommends the single most appropriate
next step: ask (qualification), reply, follow-up (nudge), escalate,
schedule, or wait. Deterministic rule-based logic — per the blueprint's
own principle, the LLM shouldn't own decisions better expressed as
explicit business rules; the LLM's job stays limited to drafting the
actual message text once an action is chosen.

This is a RECOMMENDATION layer: it tells staff (or, combined with
policy.py's autonomy controls, could trigger automatically) what to do
next — it doesn't decide alone whether to auto-execute (that's policy.py).
"""

from leads import get_lead, list_leads
from draft import get_pending_messages_for_lead
from followup import get_leads_due_for_followup

SCHEDULING_KEYWORDS = [
    "schedule", "book a call", "available", "meet up", "set up a time",
    "book a meeting", "call this week", "talk on the phone", "hop on a call",
    "time works", "when can we", "free to chat", "jump on a call",
]

ACTIVE_STATUSES = ["new", "awaiting_info", "qualifying", "qualified", "drafted", "sent", "replied", "send_failed"]


def get_next_best_action(lead_id: int) -> dict:
    """
    Returns {"action": str, "reason": str} — the single recommended next
    step for this lead right now. Possible actions:
    - "wait" — nothing to do, ball is in the lead's court
    - "run_pipeline" — needs qualification/scoring/initial draft
    - "review_draft" — a draft is waiting for approval
    - "escalate" — a pending draft needs personal staff attention
    - "propose_meeting" — lead seems ready to schedule, no meeting booked yet
    - "generate_reply" — lead replied, needs a response
    - "send_nudge" — gone quiet past the follow-up threshold
    - "retry_send" — a previous send attempt failed
    - "none" — terminal state (opted out / rejected), no action
    """
    lead = get_lead(lead_id)
    if lead is None:
        raise ValueError(f"Lead {lead_id} not found.")

    status = lead["status"]

    if status == "opted_out":
        return {"action": "none", "reason": "Lead has opted out — no further contact."}
    if status == "rejected":
        return {"action": "none", "reason": "Draft was rejected — no automatic next step."}

    if status == "awaiting_info":
        return {"action": "wait", "reason": "Waiting on the lead to answer our qualification question."}

    if status in ("new", "qualifying", "qualified"):
        return {"action": "run_pipeline", "reason": "Needs qualification, scoring, and an initial draft."}

    if status == "drafted":
        pending = get_pending_messages_for_lead(lead_id)
        if pending and pending[0].get("needs_escalation"):
            return {"action": "escalate", "reason": pending[0].get("escalation_reason") or "This draft needs your review before sending."}
        return {"action": "review_draft", "reason": "A draft is ready for your approval."}

    if status == "send_failed":
        return {"action": "retry_send", "reason": "A previous send attempt failed — retry when ready."}

    if status == "replied":
        # Local import to avoid a circular import at module load time.
        from replies import get_conversation
        from calendar_booking import get_meetings_for_lead

        thread = get_conversation(lead_id)
        inbound = [m for m in thread if m["direction"] == "inbound"]
        if inbound:
            latest_text = inbound[-1]["body"].lower()
            if any(kw in latest_text for kw in SCHEDULING_KEYWORDS):
                meetings = get_meetings_for_lead(lead_id)
                if not any(m["status"] == "scheduled" for m in meetings):
                    return {"action": "propose_meeting", "reason": "The lead's message suggests they're ready to schedule a call."}
        return {"action": "generate_reply", "reason": "The lead replied and hasn't been responded to yet."}

    if status == "sent":
        due_ids = {l["id"] for l in get_leads_due_for_followup()}
        if lead_id in due_ids:
            return {"action": "send_nudge", "reason": "No reply in a while — worth a brief check-in."}
        return {"action": "wait", "reason": "Recently contacted — waiting for a reply."}

    return {"action": "none", "reason": "No clear next action for this status."}


def get_all_next_actions() -> list[dict]:
    """
    Next-best-action for every non-terminal lead. Returns a list of
    {**lead, "next_action": str, "next_action_reason": str}, skipping
    leads where the action is "none" or "wait" (nothing actionable right now).
    """
    results = []
    for lead in list_leads():
        if lead["status"] not in ACTIVE_STATUSES:
            continue
        try:
            rec = get_next_best_action(lead["id"])
        except Exception:
            continue
        if rec["action"] in ("none", "wait"):
            continue
        results.append({**lead, "next_action": rec["action"], "next_action_reason": rec["reason"]})
    return results