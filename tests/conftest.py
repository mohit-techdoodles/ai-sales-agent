"""
tests/conftest.py

Shared pytest setup for all regression tests.

CRITICAL: these tests must never touch the real Turso database — that's
production data (or will be). This fixture forces database.get_conn() back
onto a throwaway local SQLite file for every single test, regardless of
whatever TURSO_DATABASE_URL / TURSO_AUTH_TOKEN are sitting in your .env.
"""
import os
import sys

import pytest

# Make the project root importable when running `pytest` from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "_USE_TURSO", False)
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test_sales_agent.db"))
    database.init_db()
    yield