"""觀察名單管理測試。"""

from db.watchlist import watchlist_add, watchlist_list, watchlist_remove


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
