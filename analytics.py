"""
analytics.py
V1 Step 12 — Analytics (P1).

Computes the metrics from the blueprint's V1 analytics scope that are
genuinely measurable at V1 (no reply-tracking or calendar integration exist
yet — those are V2 features per the blueprint itself). Pure functions that
take already-fetched leads/messages lists — no DB access here — so they're
easy to test and reuse.

NOT included (honest gaps, need V2):
- Response rate: requires a channel that captures inbound replies.
- Meeting-booking rate: requires calendar integration (explicitly V2).
- Cost per qualified opportunity: meaningless at $0 (Groq free tier) —
  token/call counts are tracked instead, ready to multiply by real
  pricing once you're on a paid tier.
"""

QUALIFIED_OR_LATER = ("qualified", "drafted", "sent", "send_failed")
DECIDED_STATUSES = ("approved", "edited", "rejected")


def compute_funnel(leads: list[dict]) -> dict:
    """Counts of leads at each stage of the pipeline."""
    funnel = {
        "total": len(leads),
        "new": 0,
        "awaiting_info": 0,
        "qualifying": 0,
        "qualified": 0,
        "drafted": 0,
        "sent": 0,
        "send_failed": 0,
        "rejected": 0,
    }
    for lead in leads:
        status = lead.get("status", "new")
        if status in funnel:
            funnel[status] += 1
    return funnel


def compute_qualification_rate(leads: list[dict]) -> float | None:
    """% of all leads that made it to at least 'qualified' status."""
    if not leads:
        return None
    qualified_count = len([l for l in leads if l.get("status") in QUALIFIED_OR_LATER])
    return round(100 * qualified_count / len(leads), 1)


def compute_score_stats(leads: list[dict]) -> dict:
    """Average score and a simple bucketed distribution, over leads that have been scored."""
    scores = [l["score"] for l in leads if l.get("score") is not None]
    if not scores:
        return {"average": None, "count_scored": 0, "distribution": {}}

    buckets = {"0-25": 0, "26-50": 0, "51-75": 0, "76-100": 0}
    for s in scores:
        if s <= 25:
            buckets["0-25"] += 1
        elif s <= 50:
            buckets["26-50"] += 1
        elif s <= 75:
            buckets["51-75"] += 1
        else:
            buckets["76-100"] += 1

    return {
        "average": round(sum(scores) / len(scores), 1),
        "count_scored": len(scores),
        "distribution": buckets,
    }


def compute_approval_rejection_rate(messages: list[dict]) -> dict:
    """
    Out of drafts that have actually been DECIDED (approved/edited/rejected —
    excludes still-pending drafts), what fraction were approved (with or
    without edits) vs rejected.
    """
    decided = [m for m in messages if m.get("approval_status") in DECIDED_STATUSES]
    if not decided:
        return {"decided_count": 0, "approval_rate": None, "rejection_rate": None}

    approved_count = len([m for m in decided if m["approval_status"] in ("approved", "edited")])
    rejected_count = len([m for m in decided if m["approval_status"] == "rejected"])

    return {
        "decided_count": len(decided),
        "approval_rate": round(100 * approved_count / len(decided), 1),
        "rejection_rate": round(100 * rejected_count / len(decided), 1),
    }


def compute_followup_completion_rate(leads: list[dict]) -> dict:
    """
    Of leads that were ever asked a follow-up question (followup_rounds >= 1),
    what fraction progressed past 'awaiting_info' (i.e. the lead actually replied).
    """
    asked = [l for l in leads if (l.get("followup_rounds") or 0) >= 1]
    if not asked:
        return {"asked_count": 0, "completion_rate": None}

    completed = [l for l in asked if l.get("status") != "awaiting_info"]
    return {
        "asked_count": len(asked),
        "still_waiting": len(asked) - len(completed),
        "completion_rate": round(100 * len(completed) / len(asked), 1),
    }


def compute_extended_funnel(leads: list[dict], messages: list[dict], meetings: list[dict], opportunities: list[dict]) -> dict:
    """
    V2 — the exact funnel shape from the blueprint's North-Star KPI section:
    Lead -> qualified -> contacted -> replied -> meeting -> opportunity.

    Built from actual EVENTS that happened (has a score, has an approved
    outbound message, has an inbound message, has a meeting row, has an
    opportunity row) rather than the current lead.status snapshot — status
    changes over time (e.g. sent -> replied), so counting live status would
    undercount earlier funnel stages once a lead moves further along.
    """
    lead_ids = {l["id"] for l in leads}
    qualified_ids = {l["id"] for l in leads if l.get("score") is not None}
    contacted_ids = {
        m["lead_id"] for m in messages
        if m.get("direction") == "outbound" and m.get("approval_status") in ("approved", "edited")
    }
    replied_ids = {m["lead_id"] for m in messages if m.get("direction") == "inbound"}
    meeting_ids = {m["lead_id"] for m in meetings}
    opportunity_ids = {o["lead_id"] for o in opportunities}

    return {
        "lead": len(lead_ids),
        "qualified": len(qualified_ids),
        "contacted": len(contacted_ids),
        "replied": len(replied_ids),
        "meeting": len(meeting_ids),
        "opportunity": len(opportunity_ids),
    }


def compute_response_rate(messages: list[dict]) -> dict:
    """
    Of leads actually contacted (an approved outbound message sent), what
    fraction replied. Previously blocked on no reply-tracking existing —
    unblocked now that email + Telegram reply detection exist.
    """
    contacted_ids = {
        m["lead_id"] for m in messages
        if m.get("direction") == "outbound" and m.get("approval_status") in ("approved", "edited")
    }
    replied_ids = {m["lead_id"] for m in messages if m.get("direction") == "inbound"}

    if not contacted_ids:
        return {"contacted_count": 0, "response_rate": None}

    responded = contacted_ids & replied_ids
    return {
        "contacted_count": len(contacted_ids),
        "response_rate": round(100 * len(responded) / len(contacted_ids), 1),
    }


def compute_meeting_rate(messages: list[dict], meetings: list[dict]) -> dict:
    """
    Of leads actually contacted, what fraction had a meeting booked.
    Previously blocked on no calendar integration existing — unblocked now.
    """
    contacted_ids = {
        m["lead_id"] for m in messages
        if m.get("direction") == "outbound" and m.get("approval_status") in ("approved", "edited")
    }
    meeting_ids = {m["lead_id"] for m in meetings}

    if not contacted_ids:
        return {"contacted_count": 0, "meeting_rate": None}

    booked = contacted_ids & meeting_ids
    return {
        "contacted_count": len(contacted_ids),
        "meeting_rate": round(100 * len(booked) / len(contacted_ids), 1),
    }