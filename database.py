"""
database.py
V1 database layer — plain SQLite, no ORM, no server needed.
Deployable as-is on Streamlit Community Cloud (file-based DB).

Tables (V1 scope only — see blueprint):
- leads
- lead_requirements
- activity_log  (simple audit trail for V1)
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = "sales_agent.db"


@contextmanager
def get_conn():
    """Context-managed SQLite connection with row access by column name."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create all V1 tables if they don't exist yet. Safe to call every app startup."""
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                email TEXT,
                phone TEXT,
                company TEXT,
                source TEXT,
                message TEXT,                     -- the lead's original inquiry/message, for retry if AI steps fail
                status TEXT DEFAULT 'new',       -- new | awaiting_info | qualifying | qualified | drafted | sent | send_failed | replied | opted_out | rejected
                pending_question TEXT,            -- follow-up question waiting on the lead's reply, if any
                followup_rounds INTEGER DEFAULT 0,  -- how many follow-up questions have been asked (capped)
                nudge_count INTEGER DEFAULT 0,    -- how many "haven't heard back" follow-ups sent (capped, V2)
                telegram_chat_id TEXT,             -- set once the lead starts our Telegram bot (V2)
                score INTEGER,
                score_reasons TEXT,               -- human-readable explanation of the score
                created_at TEXT,
                updated_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS lead_requirements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                use_case TEXT,
                budget TEXT,
                authority TEXT,
                timeline TEXT,
                notes TEXT,
                created_at TEXT,
                FOREIGN KEY (lead_id) REFERENCES leads (id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                action TEXT,          -- e.g. 'lead_created', 'qualified', 'scored', 'draft_created', 'approved', 'sent', 'rejected'
                detail TEXT,
                created_at TEXT,
                FOREIGN KEY (lead_id) REFERENCES leads (id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                channel TEXT DEFAULT 'email',     -- email | whatsapp
                direction TEXT DEFAULT 'outbound', -- outbound (our drafts) | inbound (lead's replies, V2)
                subject TEXT,                      -- used for email; blank for whatsapp
                body TEXT,
                approval_status TEXT DEFAULT 'pending',  -- pending | approved | edited | rejected | received (inbound)
                external_id TEXT,                  -- email Message-ID, prevents re-importing the same reply twice
                created_at TEXT,
                decided_at TEXT,
                FOREIGN KEY (lead_id) REFERENCES leads (id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS research_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                source TEXT,           -- e.g. 'web_search'
                finding TEXT,          -- the actual snippet/fact found
                evidence_url TEXT,     -- where this came from, for verification
                retrieved_at TEXT,     -- timestamp, so staleness is visible
                FOREIGN KEY (lead_id) REFERENCES leads (id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                stage TEXT DEFAULT 'new',       -- new | qualified | proposal | negotiation | won | lost
                value REAL,                      -- estimated deal value
                probability INTEGER,             -- 0-100, win likelihood
                next_action TEXT,
                created_at TEXT,
                updated_at TEXT,
                FOREIGN KEY (lead_id) REFERENCES leads (id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS meetings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                calendar_event_id TEXT,
                start_at TEXT,
                end_at TEXT,
                status TEXT DEFAULT 'scheduled',  -- scheduled | cancelled
                meet_link TEXT,
                created_at TEXT,
                FOREIGN KEY (lead_id) REFERENCES leads (id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_activity(lead_id: int, action: str, detail: str = ""):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO activity_log (lead_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
            (lead_id, action, detail, now_iso()),
        )


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at ./{DB_PATH}")