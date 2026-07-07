"""觀察名單管理測試。"""

import sqlite3

from db.watchlist import (
    load_watchlist_seeded,
    watchlist_add,
    watchlist_list,
    watchlist_remove,
)


def test_watchlist_list_empty(memory_db):
    """空觀察名單回傳空 list。"""
    items = watchlist_list(conn=memory_db)
    assert items == []


def test_watchlist_add_and_list(memory_db):
    """新增後可列出。"""
    watchlist_add("2330", "台積電", "權值股", conn=memory_db)
    items = watchlist_list(conn=memory_db)
    assert len(items) == 1
    assert items[0]["stock_id"] == "2330"
    assert items[0]["stock_name"] == "台積電"
    assert items[0]["reason"] == "權值股"


def test_watchlist_add_replace(memory_db):
    """重複新增同一 stock_id 會覆蓋。"""
    watchlist_add("2330", "台積電", "理由1", conn=memory_db)
    watchlist_add("2330", "台積電", "理由2", conn=memory_db)
    items = watchlist_list(conn=memory_db)
    assert len(items) == 1
    assert items[0]["reason"] == "理由2"


def test_watchlist_remove(memory_db):
    """移除成功回傳 True。"""
    watchlist_add("2330", "台積電", "test", conn=memory_db)
    assert watchlist_remove("2330", conn=memory_db) is True
    items = watchlist_list(conn=memory_db)
    assert len(items) == 0


def test_watchlist_remove_not_found(memory_db):
    """移除不存在的 stock 回傳 False。"""
    assert watchlist_remove("9999", conn=memory_db) is False


def test_watchlist_multiple(memory_db):
    """多筆操作。"""
    watchlist_add("2330", "台積電", "權值股", conn=memory_db)
    watchlist_add("2409", "友達", "外資反手", conn=memory_db)
    watchlist_add("3481", "群創", "面板", conn=memory_db)

    items = watchlist_list(conn=memory_db)
    assert len(items) == 3

    watchlist_remove("3481", conn=memory_db)
    items = watchlist_list(conn=memory_db)
    assert len(items) == 2
    ids = {i["stock_id"] for i in items}
    assert "3481" not in ids


def test_load_watchlist_seeded_prefers_db(memory_db):
    """DB 有資料 -> 回傳 DB 名單（只含 stock_id/stock_name）。"""
    watchlist_add("2454", "聯發科", "web 加入", conn=memory_db)

    items = load_watchlist_seeded(conn=memory_db)

    ids = {i["stock_id"] for i in items}
    assert "2454" in ids
    assert set(items[0].keys()) == {"stock_id", "stock_name"}


def test_load_watchlist_seeded_falls_back_to_json_seed(memory_db):
    """DB 為空 -> fallback 到 config/watchlist.json 種子。"""
    items = load_watchlist_seeded(conn=memory_db)

    ids = {i["stock_id"] for i in items}
    assert "2330" in ids  # 來自種子檔


def test_load_watchlist_seeded_falls_back_when_table_missing():
    """watchlist 表不存在（DB 尚未建表）-> 走 except 分支 fallback 到種子。"""
    bare = sqlite3.connect(":memory:")  # 刻意不 create_all_tables
    try:
        items = load_watchlist_seeded(conn=bare)
    finally:
        bare.close()

    ids = {i["stock_id"] for i in items}
    assert "2330" in ids  # 來自種子檔
