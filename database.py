"""
database.py
V1 database layer — now backed by Turso (libSQL) instead of a local SQLite file,
so data survives Streamlit Community Cloud reboots / redeploys / sleep-wake cycles.

Why this file changes and nothing else does:
Every other module (leads.py, draft.py, opportunities.py, ...) only ever talks to
the database through `get_conn()`, using `conn.execute(sql, params)`,
`cursor.lastrowid`, `dict(row)` and `row["col"]`. This file provides a thin
compatibility layer so all of that keeps working unmodified, while the actual
storage is now a remote, persistent libSQL database (Turso's free tier).

Local development still works with zero setup: if no Turso credentials are
found, it transparently falls back to the old local sqlite_agent.db file.

We use the `libsql-client` package (NOT libsql-experimental). libsql-experimental
compiles a Rust extension on install (needs Cargo/maturin and can hang or fail
on machines without a Rust toolchain). libsql-client is pure Python, talks to
Turso over HTTP/WebSocket, and is what Turso's own docs recommend as the
stable client — `pip install libsql-client` just works, no compiler needed.

Setup:
1. pip install libsql-client   (added to requirements.txt)
2. In the Turso dashboard (or `turso db create sales-agent`), create a NEW,
   empty database — don't upload a .sql file, init_db() below creates all
   the tables automatically the first time the app runs.
3. Get the URL + token:
     turso db show --url sales-agent
     turso db tokens create sales-agent
4. Locally: put them in a .env file:
     TURSO_DATABASE_URL=libsql://sales-agent-yourorg.turso.io
     TURSO_AUTH_TOKEN=eyJ...
   On Streamlit Community Cloud: put the same two keys in
   App -> Settings -> Secrets (TOML format, same key names).
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()  # so TURSO_DATABASE_URL / TURSO_AUTH_TOKEN are picked up even
                # when this file (or a script that imports it) is run directly,
                # not just through app.py

DB_PATH = "sales_agent.db"  # local dev fallback only


# ---------------------------------------------------------------------------
# Credential lookup — works from .env locally and from st.secrets on Cloud
# ---------------------------------------------------------------------------
def _get_turso_credentials():
    url = os.environ.get("TURSO_DATABASE_URL")
    token = os.environ.get("TURSO_AUTH_TOKEN")

    if not url or not token:
        try:
            import streamlit as st
            url = url or st.secrets.get("TURSO_DATABASE_URL")
            token = token or st.secrets.get("TURSO_AUTH_TOKEN")
        except Exception:
            pass

    return url, token


def _to_http_url(url: str) -> str:
    """libsql:// and wss:// both mean 'use WebSocket' to libsql-client, which
    has been hitting a handshake error against current Turso servers. https://
    forces the simpler HTTP transport instead (fine for us — we don't use
    multi-statement transactions)."""
    if url.startswith("libsql://"):
        return "https://" + url[len("libsql://"):]
    if url.startswith("wss://"):
        return "https://" + url[len("wss://"):]
    return url


_TURSO_URL, _TURSO_TOKEN = _get_turso_credentials()
if _TURSO_URL:
    _TURSO_URL = _to_http_url(_TURSO_URL)
_USE_TURSO = bool(_TURSO_URL and _TURSO_TOKEN)

if _USE_TURSO:
    import libsql_client


# ---------------------------------------------------------------------------
# Compatibility wrapper: makes libsql_client's Client/ResultSet behave like
# sqlite3's Connection/Cursor (dict-style rows + cursor.lastrowid), which is
# what the rest of the app already assumes.
# ---------------------------------------------------------------------------
class _CursorProxy:
    def __init__(self, result_set, client):
        self._rs = result_set
        self._client = client
        self._idx = 0

    def fetchone(self):
        if self._idx >= len(self._rs.rows):
            return None
        row = self._rs.rows[self._idx]
        self._idx += 1
        return dict(zip(self._rs.columns, row))

    def fetchall(self):
        rows = [dict(zip(self._rs.columns, r)) for r in self._rs.rows[self._idx:]]
        self._idx = len(self._rs.rows)
        return rows

    @property
    def lastrowid(self):
        # Ask the same session for the rowid of the row just inserted — this
        # works because libSQL speaks the same SQL dialect as SQLite.
        rs = self._client.execute("SELECT last_insert_rowid()")
        return rs.rows[0][0]


class _ConnProxy:
    def __init__(self, client):
        self._client = client

    def execute(self, sql, params=()):
        rs = self._client.execute(sql, list(params) if params else [])
        return _CursorProxy(rs, self._client)

    def commit(self):
        pass  # libsql-client statements commit as they run; nothing to flush

    def close(self):
        self._client.close()


@contextmanager
def get_conn():
    """Context-managed connection with row access by column name.

    Uses Turso (remote, persistent) if TURSO_DATABASE_URL/TURSO_AUTH_TOKEN are
    set; otherwise falls back to the local sqlite file for easy local dev.
    """
    if _USE_TURSO:
        client = libsql_client.create_client_sync(_TURSO_URL, auth_token=_TURSO_TOKEN)
        conn = _ConnProxy(client)
        try:
            yield conn
        finally:
            conn.close()
    else:
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
                message TEXT,
                status TEXT DEFAULT 'new',
                pending_question TEXT,
                followup_rounds INTEGER DEFAULT 0,
                nudge_count INTEGER DEFAULT 0,
                telegram_chat_id TEXT,
                escalation_note TEXT,
                score INTEGER,
                score_reasons TEXT,
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
                action TEXT,
                detail TEXT,
                created_at TEXT,
                FOREIGN KEY (lead_id) REFERENCES leads (id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                channel TEXT DEFAULT 'email',
                direction TEXT DEFAULT 'outbound',
                subject TEXT,
                body TEXT,
                approval_status TEXT DEFAULT 'pending',
                external_id TEXT,
                needs_escalation INTEGER DEFAULT 0,
                escalation_reason TEXT,
                attachment_filename TEXT,
                attachment_data TEXT,
                created_at TEXT,
                decided_at TEXT,
                FOREIGN KEY (lead_id) REFERENCES leads (id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS research_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                source TEXT,
                finding TEXT,
                evidence_url TEXT,
                retrieved_at TEXT,
                FOREIGN KEY (lead_id) REFERENCES leads (id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                stage TEXT DEFAULT 'new',
                value REAL,
                probability INTEGER,
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
                status TEXT DEFAULT 'scheduled',
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

        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_base (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT,
                answer TEXT,
                created_at TEXT
            )
        """)

        # V6-B — persists what generate_briefing() produced, so a later call
        # transcript can be reconciled against what we actually believed
        # going into the call (not just re-derived after the fact).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS briefings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                briefing_text TEXT,
                requirements_snapshot TEXT,
                score_snapshot INTEGER,
                created_at TEXT,
                FOREIGN KEY (lead_id) REFERENCES leads (id)
            )
        """)

        # V6-B — manual-entry version for now (see call_reconciliation.py):
        # staff paste in a transcript rather than it arriving via a Recall.ai/
        # Gong webhook, since Streamlit Community Cloud can't host a
        # persistent webhook receiver. Same columns as the original blueprint.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS call_transcripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                meeting_id INTEGER,
                duration_sec INTEGER,
                transcript_text TEXT,
                summary_json TEXT,
                created_at TEXT,
                FOREIGN KEY (lead_id) REFERENCES leads (id),
                FOREIGN KEY (meeting_id) REFERENCES meetings (id)
            )
        """)

        # V6-A — compliance/safety trail for every LLM call. Note: the
        # blueprint's original schema used "agent_run_id" referencing an
        # agent_runs table, but this codebase never built that table (see
        # README/blueprint deviations) — leads and pipeline steps ARE
        # tracked here (lead_id, step), so we key off those instead.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS security_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER,
                step TEXT,
                model_name TEXT,
                pii_entities TEXT,
                injection_flagged INTEGER DEFAULT 0,
                latency_ms INTEGER,
                created_at TEXT,
                FOREIGN KEY (lead_id) REFERENCES leads (id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS company_enrichment (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                domain TEXT,
                page_title TEXT,
                meta_description TEXT,
                tech_stack TEXT,
                fetched_at TEXT,
                FOREIGN KEY (lead_id) REFERENCES leads (id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS faq_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT,
                answer TEXT,
                embedding TEXT,            -- JSON-encoded list of floats
                hit_count INTEGER DEFAULT 0,
                last_accessed_at TEXT,
                created_at TEXT,
                updated_at TEXT
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
    where = "Turso (remote)" if _USE_TURSO else f"./{DB_PATH} (local fallback)"
    print(f"Database initialized -> {where}")