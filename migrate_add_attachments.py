"""
migrate_add_attachments.py
V5 — One-time migration: adds attachment_filename and attachment_data
columns to the existing messages table (for both SQLite and Turso —
uses your existing database.py connection, whichever backend is active).

CREATE TABLE IF NOT EXISTS in database.py only affects brand-new databases
— it won't add columns to a table that already exists, hence this
separate migration for your existing (persistent) data.

Safe to run multiple times — checks if each column already exists first.

Run: python3 migrate_add_attachments.py
"""

from database import get_conn


def _add_column_if_missing(conn, table: str, column: str, col_type: str = "TEXT"):
    """
    Tries to add the column; if it already exists, the database will raise
    an error (SQLite: 'duplicate column name', similar for Turso/libsql) —
    caught and treated as success, since the end state is the same either
    way. This is more portable across backends than relying on PRAGMA,
    which may behave differently depending on the exact Turso client used.
    """
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        print(f"Added {column} column.")
    except Exception as e:
        if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
            print(f"{column} already exists, skipping.")
        else:
            raise


def migrate():
    with get_conn() as conn:
        _add_column_if_missing(conn, "messages", "attachment_filename")
        _add_column_if_missing(conn, "messages", "attachment_data")

    print("Migration complete.")


if __name__ == "__main__":
    migrate()