"""
opportunities.py
V2 — Opportunity tracking.

Matches the blueprint's core data model (opportunities: stage, value,
probability, next_action). Creation is a manual staff action — when a
conversation looks promising, staff converts that lead into a tracked
deal. One active opportunity per lead (kept simple for V2); stage moves
forward (or to lost) as the deal progresses.
"""

from database import get_conn, log_activity, now_iso

STAGES = ["new", "qualified", "proposal", "negotiation", "won", "lost"]


def create_opportunity(lead_id: int, stage: str = "new", value: float = None, probability: int = None, next_action: str = "") -> dict:
    """Creates a new opportunity for a lead. Fails if one already exists (use update_opportunity instead)."""
    existing = get_opportunity_for_lead(lead_id)
    if existing is not None:
        raise ValueError(f"Lead {lead_id} already has an opportunity (id {existing['id']}). Use update_opportunity() instead.")

    timestamp = now_iso()
    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO opportunities (lead_id, stage, value, probability, next_action, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (lead_id, stage, value, probability, next_action, timestamp, timestamp),
        )
        opp_id = cursor.lastrowid

    log_activity(lead_id, "opportunity_created", f"Opportunity {opp_id} created at stage '{stage}'")

    return get_opportunity(opp_id)


def update_opportunity(opportunity_id: int, stage: str = None, value: float = None, probability: int = None, next_action: str = None) -> dict:
    """Updates only the fields provided (None = leave unchanged)."""
    existing = get_opportunity(opportunity_id)
    if existing is None:
        raise ValueError(f"Opportunity {opportunity_id} not found.")

    final_stage = stage if stage is not None else existing["stage"]
    final_value = value if value is not None else existing["value"]
    final_probability = probability if probability is not None else existing["probability"]
    final_next_action = next_action if next_action is not None else existing["next_action"]
    timestamp = now_iso()

    with get_conn() as conn:
        conn.execute(
            """
            UPDATE opportunities
            SET stage = ?, value = ?, probability = ?, next_action = ?, updated_at = ?
            WHERE id = ?
            """,
            (final_stage, final_value, final_probability, final_next_action, timestamp, opportunity_id),
        )

    if stage is not None and stage != existing["stage"]:
        log_activity(existing["lead_id"], "opportunity_stage_changed", f"Opportunity {opportunity_id}: '{existing['stage']}' -> '{stage}'")

    return get_opportunity(opportunity_id)


def get_opportunity(opportunity_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()
        return dict(row) if row else None


def get_opportunity_for_lead(lead_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM opportunities WHERE lead_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (lead_id,),
        ).fetchone()
        return dict(row) if row else None


def list_opportunities() -> list[dict]:
    """All opportunities with the lead's name/company joined in, for display."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT o.*, l.name AS lead_name, l.company AS lead_company, l.email AS lead_email
            FROM opportunities o
            JOIN leads l ON l.id = o.lead_id
            ORDER BY o.updated_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def pipeline_value_by_stage() -> dict:
    """Sum of opportunity values grouped by stage — a quick pipeline snapshot."""
    opps = list_opportunities()
    totals = {stage: 0.0 for stage in STAGES}
    counts = {stage: 0 for stage in STAGES}
    for o in opps:
        stage = o.get("stage", "new")
        if stage in totals:
            totals[stage] += o.get("value") or 0
            counts[stage] += 1
    return {"totals": totals, "counts": counts}