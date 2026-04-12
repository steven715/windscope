import logging
import sqlite3
from contextlib import contextmanager

from config import settings

logger = logging.getLogger(__name__)


@contextmanager
def get_connection(db_path: str | None = None):
    """Context manager，自動 commit / rollback / close。測試時傳 ':memory:'。"""
    db_path = db_path or settings.DB_PATH
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
