"""
security_log.py
V6-A — writes to and reads from the security_audit table (compliance
monitoring / runtime safety tracking). See pii_guard.py for the actual
detection logic; this module just persists the results.
"""
import json

from database import get_conn, now_iso


def log_security_event(lead_id: int | None, step: str, model_name: str,
                        pii_entities: list[str], injection_flagged: bool, latency_ms: int) -> None:
    """
    Records one LLM call's safety metadata. Never stores the actual PII
    values or the raw text that tripped the injection check — only WHICH
    entity types were found and WHETHER injection patterns matched, so the
    audit log itself can't become a new source of leaked PII.
    """
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO security_audit (lead_id, step, model_name, pii_entities,
                                         injection_flagged, latency_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (lead_id, step, model_name, json.dumps(pii_entities),
             1 if injection_flagged else 0, latency_ms, now_iso()),
        )


def get_security_events(lead_id: int | None = None, limit: int = 100) -> list[dict]:
    """All audit rows, newest first. Filtered to one lead if lead_id is given."""
    with get_conn() as conn:
        if lead_id is not None:
            rows = conn.execute(
                "SELECT * FROM security_audit WHERE lead_id = ? ORDER BY created_at DESC LIMIT ?",
                (lead_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM security_audit ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()

    events = []
    for row in rows:
        event = dict(row)
        try:
            event["pii_entities"] = json.loads(event["pii_entities"]) if event["pii_entities"] else []
        except (json.JSONDecodeError, TypeError):
            event["pii_entities"] = []
        event["injection_flagged"] = bool(event["injection_flagged"])
        events.append(event)
    return events


def get_flagged_events(limit: int = 100) -> list[dict]:
    """Rows where PII was found or injection was flagged — the ones worth a human glance."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM security_audit
            WHERE injection_flagged = 1 OR (pii_entities IS NOT NULL AND pii_entities != '[]')
            ORDER BY created_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()

    events = []
    for row in rows:
        event = dict(row)
        try:
            event["pii_entities"] = json.loads(event["pii_entities"]) if event["pii_entities"] else []
        except (json.JSONDecodeError, TypeError):
            event["pii_entities"] = []
        event["injection_flagged"] = bool(event["injection_flagged"])
        events.append(event)
    return events