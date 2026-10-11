"""BaseCollector 測試：db_path 注入、_try_collect_and_save 三分支、觀察名單載入。"""

import sqlite3

from collectors.base import BaseCollector, load_watchlist
from db.schema import create_all_tables


def test_db_path_injected():
    assert BaseCollector(db_path="/tmp/x.db").db_path == "/tmp/x.db"


def test_try_collect_and_save_branches():
    """有資料→存且 True；None→不存、False；例外→不 raise、False。"""
    c = BaseCollector(db_path=":memory:")
    saved = []
    assert c._try_collect_and_save(lambda: {"x": 1}, saved.append) is True
    assert c._try_collect_and_save(lambda: None, saved.append) is False

    def boom():
        raise RuntimeError("collect blew up")
    assert c._try_collect_and_save(boom, saved.append) is False
    assert saved == [{"x": 1}]


def test_load_watchlist_reads_db(tmp_path):
    db = str(tmp_path / "w.db")
    conn = sqlite3.connect(db)
    create_all_tables(conn)
    conn.execute("INSERT INTO watchlist (stock_id, stock_name) VALUES ('2330', '台積電')")
    conn.commit()
    conn.close()
    assert load_watchlist(db) == [{"stock_id": "2330", "stock_name": "台積電"}]
