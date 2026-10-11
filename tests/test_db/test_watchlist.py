"""觀察名單管理測試。"""

import json

from db.watchlist import (
    watchlist_add,
    watchlist_get,
    watchlist_list,
    watchlist_remove,
    watchlist_seed,
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


def test_watchlist_get(memory_db):
    watchlist_add("2454", "聯發科", "web 加入", conn=memory_db)
    item = watchlist_get("2454", conn=memory_db)
    assert item["stock_name"] == "聯發科" and item["reason"] == "web 加入"
    assert watchlist_get("9999", conn=memory_db) is None


def _seed_file(tmp_path, reason="權值股"):
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps([{"stock_id": "2330", "stock_name": "台積電",
                                 "added_date": "2026-04-08", "reason": reason}],
                               ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_watchlist_seed_imports_and_writes_stock_info(memory_db, tmp_path):
    assert watchlist_seed(memory_db, _seed_file(tmp_path)) == 1
    assert [i["stock_id"] for i in watchlist_list(conn=memory_db)] == ["2330"]
    row = memory_db.execute(
        "SELECT stock_name FROM stock_info WHERE stock_id = '2330'").fetchone()
    assert row[0] == "台積電"


def test_watchlist_seed_idempotent(memory_db, tmp_path):
    """重複匯入冪等（INSERT OR REPLACE，以種子檔內容為準）。"""
    watchlist_seed(memory_db, _seed_file(tmp_path, "v1"))
    watchlist_seed(memory_db, _seed_file(tmp_path, "v2"))
    items = watchlist_list(conn=memory_db)
    assert len(items) == 1 and items[0]["reason"] == "v2"


def test_empty_table_means_empty_watchlist(memory_db):
    """表空就是觀察名單為空——不退回種子檔。"""
    assert watchlist_list(conn=memory_db) == []
