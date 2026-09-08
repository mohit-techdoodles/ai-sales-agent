"""
leads.py
V1 Step 2 — Lead capture + duplicate check.
Plain Python / SQL. No AI involved in this step, per the blueprint
(dedup and business rules should be deterministic code, not LLM decisions).
"""

from database import get_conn, log_activity, now_iso


def check_duplicate(email: str = "", phone: str = "", company: str = "") -> dict | None:
    """
    Look for an existing lead matching on email OR phone (exact match),
    or company name (case-insensitive) if no email/phone given.
    Returns the matching lead row as a dict, or None if no duplicate found.
    """
    email = (email or "").strip().lower()
    phone = (phone or "").strip()
    company = (company or "").strip().lower()

    with get_conn() as conn:
        if email:
            row = conn.execute(
                "SELECT * FROM leads WHERE LOWER(email) = ?", (email,)
            ).fetchone()
            if row:
                return dict(row)

        if phone:
            row = conn.execute(
                "SELECT * FROM leads WHERE phone = ?", (phone,)
            ).fetchone()
            if row:
                return dict(row)

        if company:
            row = conn.execute(
                "SELECT * FROM leads WHERE LOWER(company) = ?", (company,)
            ).fetchone()
            if row:
                return dict(row)

    return None


def receive_lead(name: str, email: str = "", phone: str = "", company: str = "", source: str = "manual", message: str = "") -> dict:
    """
    Entry point for a new lead (called from a Streamlit form, webhook, etc.).

    Behavior:
    - If a duplicate is found (by email, phone, or company), returns the
      existing lead instead of creating a new one, with is_duplicate=True.
    - Otherwise creates a new lead with status='new' and returns it.
    """
    existing = check_duplicate(email=email, phone=phone, company=company)
    if existing:
        log_activity(existing["id"], "duplicate_detected", f"Duplicate submission from source={source}")
        return {**existing, "is_duplicate": True}

    timestamp = now_iso()
    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO leads (name, email, phone, company, source, message, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?)
            """,
            (name, email, phone, company, source, message, timestamp, timestamp),
        )
        lead_id = cursor.lastrowid

    log_activity(lead_id, "lead_created", f"New lead from source={source}")

    return {
        "id": lead_id,
        "name": name,
        "email": email,
        "phone": phone,
        "company": company,
        "source": source,
        "message": message,
        "status": "new",
        "score": None,
        "created_at": timestamp,
        "updated_at": timestamp,
        "is_duplicate": False,
    }


def get_lead(lead_id: int) -> dict | None:
    """Fetch a single lead by id."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return dict(row) if row else None


def list_leads(status: str | None = None) -> list[dict]:
    """List all leads, optionally filtered by status."""
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM leads WHERE status = ? ORDER BY created_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM leads ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]