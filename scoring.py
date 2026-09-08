"""
scoring.py
V1 Step 4 — Lead scoring.

Deterministic, explainable 0-100 score based on the requirements extracted
in Step 3 (use_case, budget, authority, timeline) — a simple BANT-style model:
Budget, Authority, Need (use_case), Timeline.

Per the blueprint's implementation principle: "The LLM should not control
decisions that are better expressed as explicit business rules." Scoring
is plain Python — no LLM call, no randomness, always reproducible.

Each factor contributes up to 25 points. Reasons are collected so every
score comes with a plain-English explanation a salesperson can trust.
"""

from database import get_conn, log_activity, now_iso
from qualify import get_requirements

MAX_POINTS_PER_FACTOR = 25

# Keywords used to judge authority/timeline strength from free-text fields.
# Plain string checks — no AI involved, deliberately simple and auditable.
DECISION_MAKER_KEYWORDS = ["owner", "founder", "ceo", "director", "head", "manager", "partner"]
EVALUATOR_KEYWORDS = ["evaluating", "researching", "on behalf", "for my", "for our team"]

URGENT_TIMELINE_KEYWORDS = ["asap", "immediately", "urgent", "this week", "right away"]
SOON_TIMELINE_KEYWORDS = ["week", "2 weeks", "month"]
LATER_TIMELINE_KEYWORDS = ["quarter", "next year", "few months", "later"]


def _score_budget(budget: str) -> tuple[int, str]:
    """Budget mentioned at all = strong signal for V1's simple model."""
    budget = (budget or "").strip()
    if budget:
        return MAX_POINTS_PER_FACTOR, f"+{MAX_POINTS_PER_FACTOR} budget mentioned ('{budget}')"
    return 0, "+0 no budget mentioned"


def _score_authority(authority: str) -> tuple[int, str]:
    authority_lower = (authority or "").strip().lower()
    if not authority_lower:
        return 0, "+0 authority unclear"

    if any(kw in authority_lower for kw in DECISION_MAKER_KEYWORDS):
        return MAX_POINTS_PER_FACTOR, f"+{MAX_POINTS_PER_FACTOR} likely decision-maker ('{authority}')"

    if any(kw in authority_lower for kw in EVALUATOR_KEYWORDS):
        partial = MAX_POINTS_PER_FACTOR // 2
        return partial, f"+{partial} evaluating on behalf of someone else ('{authority}')"

    # Some authority info given, but not clearly matched to either bucket
    partial = MAX_POINTS_PER_FACTOR // 2
    return partial, f"+{partial} authority mentioned but unclear ('{authority}')"


def _score_use_case(use_case: str) -> tuple[int, str]:
    """A clearly stated use case = real need identified."""
    use_case = (use_case or "").strip()
    if use_case:
        return MAX_POINTS_PER_FACTOR, f"+{MAX_POINTS_PER_FACTOR} clear use case ('{use_case}')"
    return 0, "+0 use case not specified"


def _score_timeline(timeline: str) -> tuple[int, str]:
    timeline_lower = (timeline or "").strip().lower()
    if not timeline_lower:
        return 0, "+0 no timeline given"

    if any(kw in timeline_lower for kw in URGENT_TIMELINE_KEYWORDS):
        return MAX_POINTS_PER_FACTOR, f"+{MAX_POINTS_PER_FACTOR} urgent timeline ('{timeline}')"

    if any(kw in timeline_lower for kw in SOON_TIMELINE_KEYWORDS):
        points = 20
        return points, f"+{points} near-term timeline ('{timeline}')"

    if any(kw in timeline_lower for kw in LATER_TIMELINE_KEYWORDS):
        points = 10
        return points, f"+{points} longer-term timeline ('{timeline}')"

    # Timeline mentioned but doesn't match known patterns
    points = 15
    return points, f"+{points} timeline mentioned ('{timeline}')"


def calculate_score(requirements: dict) -> tuple[int, list[str]]:
    """
    Pure function: takes a requirements dict (use_case, budget, authority, timeline)
    and returns (score_0_to_100, list_of_reason_strings).
    No side effects, no database access — easy to unit test.
    """
    budget_pts, budget_reason = _score_budget(requirements.get("budget", ""))
    authority_pts, authority_reason = _score_authority(requirements.get("authority", ""))
    use_case_pts, use_case_reason = _score_use_case(requirements.get("use_case", ""))
    timeline_pts, timeline_reason = _score_timeline(requirements.get("timeline", ""))

    total = budget_pts + authority_pts + use_case_pts + timeline_pts
    reasons = [budget_reason, authority_reason, use_case_reason, timeline_reason]

    return total, reasons


def score_lead(lead_id: int) -> dict:
    """
    Full Step 4 pipeline for a given lead:
    1. Fetch the lead's most recent extracted requirements (from Step 3).
    2. Calculate the score + reasons.
    3. Save score and score_reasons to the leads table, update status to 'qualified'.
    4. Log the activity.
    Returns {"score": int, "reasons": list[str]}.
    Raises ValueError if the lead has no requirements yet (i.e. Step 3 wasn't run).
    """
    requirements = get_requirements(lead_id)
    if requirements is None:
        raise ValueError(f"Lead {lead_id} has no extracted requirements yet — run qualify_lead() first.")

    score, reasons = calculate_score(requirements)
    reasons_text = "; ".join(reasons)
    timestamp = now_iso()

    with get_conn() as conn:
        conn.execute(
            "UPDATE leads SET score = ?, score_reasons = ?, status = 'qualified', updated_at = ? WHERE id = ?",
            (score, reasons_text, timestamp, lead_id),
        )

    log_activity(lead_id, "scored", f"Score: {score}/100 — {reasons_text}")

    return {"score": score, "reasons": reasons}