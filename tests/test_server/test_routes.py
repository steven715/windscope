"""Server 路由測試：頁面 200 + 關鍵內容、API 區間查詢、空狀態。"""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from db.schema import create_all_tables
from server.app import create_app


@pytest.fixture
def client(tmp_path):
    """空 DB 的 TestClient（不啟排程器）。"""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    conn.close()
    app = create_app(db_path=db_path, enable_scheduler=False)
    return TestClient(app)


@pytest.fixture
def client_with_data(tmp_path):
    """塞了訊號 / 驗證 / 指標 / watchlist 的 TestClient。"""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    conn.execute(
        "INSERT INTO daily_metrics (date, fx_delta_twd, fx_direction, "
        "fx_asia_sync, futures_spread_adjusted, futures_volume_ratio, "
        "oi_net_foreign) VALUES ('2026-06-12', -0.15, 'bullish', 1, 45.0, 1.35, 38420)"
    )
    conn.execute(
        "INSERT INTO signals (date, direction, confidence, fx_vote, "
        "futures_vote, reasons, rule_version) VALUES "
        "('2026-06-12', 'bullish', 4, 'bullish', 'neutral', ?, 'v1')",
        (json.dumps(["匯率與期貨同向", "亞幣同步 +1"], ensure_ascii=False),),
    )
    conn.execute(
        "INSERT INTO verifications (date, predicted_direction, confidence, "
        "day_change_pct, day_change_class, open_gap_pct, open_gap_class, "
        "hit_day, hit_open) VALUES "
        "('2026-06-11', 'bullish', 3, 0.58, 'up', 0.4, 'up', 1, 1)"
    )
    conn.execute(
        "INSERT INTO watchlist (stock_id, stock_name, added_date, reason) "
        "VALUES ('2330', '台積電', '2026-04-08', '權值股')"
    )
    conn.execute(
        "INSERT INTO stock_signals (date, stock_id, broker_name, category, "
        "reasons, rule_version) VALUES "
        "('2026-06-12', '2330', '兆豐-嘉義', 'bottom_watch', '低檔連買 3 天', 'v1')"
    )
    conn.execute("INSERT INTO raw_index (date, open, close) VALUES ('2026-06-11', 42900, 43000)")
    conn.commit()
    conn.close()
    app = create_app(db_path=db_path, enable_scheduler=False)
    return TestClient(app)


class TestPagesEmpty:
    """空 DB 時所有頁面要能開、顯示空狀態，不能 500。"""

    @pytest.mark.parametrize("path", ["/", "/signals", "/data", "/watchlist", "/scheduler"])
    def test_page_returns_200(self, client, path):
        resp = client.get(path)
        assert resp.status_code == 200

    def test_dashboard_empty_state(self, client):
        resp = client.get("/")
        assert "尚無訊號資料" in resp.text

    def test_scheduler_disabled_state(self, client):
        resp = client.get("/scheduler")
        assert "排程器未啟用" in resp.text


class TestPagesWithData:
    def test_dashboard_shows_signal(self, client_with_data):
        resp = client_with_data.get("/")
        assert resp.status_code == 200
        assert "偏多" in resp.text
        assert "4 / 5" in resp.text
        assert "匯率與期貨同向" in resp.text

    def test_dashboard_shows_verification(self, client_with_data):
        resp = client_with_data.get("/")
        assert "✓ 命中" in resp.text
        assert "100.0%" in resp.text  # 近期命中率

    def test_signals_page_lists_record(self, client_with_data):
        resp = client_with_data.get("/signals")
        assert "2026-06-12" in resp.text
        assert "偏多" in resp.text

    def test_signals_page_date_filter(self, client_with_data):
        resp = client_with_data.get("/signals?date_from=2026-06-13")
        assert "查無訊號紀錄" in resp.text

    def test_data_page_renders_table(self, client_with_data):
        resp = client_with_data.get("/data?table=raw_index")
        assert "2026-06-11" in resp.text

    def test_data_page_rejects_unknown_table(self, client_with_data):
        """未知表名 fallback 到 daily_metrics，不報錯。"""
        resp = client_with_data.get("/data?table=sqlite_master")
        assert resp.status_code == 200
        assert "daily_metrics" in resp.text

    def test_watchlist_page(self, client_with_data):
        resp = client_with_data.get("/watchlist")
        assert "台積電" in resp.text
        assert "bottom_watch" in resp.text

    def test_watchlist_page_shows_taiex(self, client_with_data):
        """觀察名單頁置頂顯示大盤指數。"""
        resp = client_with_data.get("/watchlist")
        assert "加權指數" in resp.text
        assert "43000" in resp.text  # raw_index 的收盤

    def test_data_page_chinese_labels(self, client_with_data):
        """資料瀏覽下拉選單顯示中文標籤。"""
        resp = client_with_data.get("/data")
        assert "每日衍生指標" in resp.text
        assert "三大法人（原始）" in resp.text


class TestApi:
    def test_api_signals(self, client_with_data):
        resp = client_with_data.get("/api/signals")
        data = resp.json()
        assert data["count"] == 1
        assert data["rows"][0]["direction"] == "bullish"
        assert isinstance(data["rows"][0]["reasons"], list)

    def test_api_signals_date_range_excludes(self, client_with_data):
        resp = client_with_data.get("/api/signals?date_to=2026-06-11")
        assert resp.json()["count"] == 0

    def test_api_verifications(self, client_with_data):
        resp = client_with_data.get("/api/verifications")
        data = resp.json()
        assert data["count"] == 1
        assert data["rows"][0]["hit_day"] == 1

    def test_api_raw_whitelisted(self, client_with_data):
        resp = client_with_data.get("/api/raw/raw_index")
        assert resp.status_code == 200
        assert resp.json()["count"] == 1

    def test_api_raw_unknown_table_404(self, client_with_data):
        resp = client_with_data.get("/api/raw/sqlite_master")
        assert resp.status_code == 404

    def test_api_stats(self, client_with_data):
        resp = client_with_data.get("/api/stats")
        data = resp.json()
        assert data["total"] == 1
        assert data["hit_day_rate"] == 100.0

    def test_api_stats_empty(self, client):
        resp = client.get("/api/stats")
        assert resp.json()["total"] == 0


class TestWatchlistManagement:
    def test_add_stock(self, client):
        """網頁新增觀察股後出現在頁面上，且 stock_info 同步。"""
        resp = client.post("/watchlist/add", data={
            "stock_id": "2454", "stock_name": "聯發科", "reason": "測試",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert "聯發科" in resp.text

    def test_remove_stock(self, client_with_data):
        resp = client_with_data.post("/watchlist/remove",
                                     data={"stock_id": "2330"},
                                     follow_redirects=True)
        assert resp.status_code == 200
        assert "台積電（2330）" not in resp.text

    def test_remove_nonexistent_no_error(self, client):
        resp = client.post("/watchlist/remove", data={"stock_id": "9999"},
                           follow_redirects=True)
        assert resp.status_code == 200


class TestDataPageChipFriendly:
    def test_chip_markers_translated(self, tmp_path):
        """raw_chip 的內部標記列以人話顯示，且股名從 stock_info 補齊。"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        create_all_tables(conn)
        conn.execute(
            "INSERT INTO raw_chip (date, stock_id, broker_name, net_volume) "
            "VALUES ('2026-06-11', '2330', '__FOREIGN__', 9111)"
        )
        conn.execute(
            "INSERT INTO stock_info (stock_id, stock_name) VALUES ('2330', '台積電')"
        )
        conn.commit()
        conn.close()
        app = create_app(db_path=db_path, enable_scheduler=False)
        resp = TestClient(app).get("/data?table=raw_chip")

        assert "__FOREIGN__" not in resp.text
        assert "外資合計" in resp.text
        assert "台積電" in resp.text
        assert "此表混合三種列" in resp.text


class TestScheduler:
    def test_create_scheduler_has_four_jobs(self):
        from server.scheduler import create_scheduler, get_jobs_info

        scheduler = create_scheduler(db_path=":memory:")
        jobs = {j.id for j in scheduler.get_jobs()}
        assert jobs == {"after_night", "before_open", "verify_close", "after_close"}

    def test_get_jobs_info_none_scheduler(self):
        from server.scheduler import get_jobs_info

        assert get_jobs_info(None) == []
