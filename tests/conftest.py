import sqlite3

import pytest

from db.schema import create_all_tables


@pytest.fixture
def memory_db():
    """提供 in-memory SQLite，已建好所有表。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    yield conn
    conn.close()
