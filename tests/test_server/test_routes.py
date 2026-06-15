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

    def test_data_page_chinese_column_headers(self, client_with_data):
        """欄位名稱顯示中文（原名放 tooltip）。"""
        resp = client_with_data.get("/data?table=raw_index")
        assert "<th" in resp.text and "開盤" in resp.text and "收盤" in resp.text
        assert 'title="open・點擊排序"' in resp.text

        resp = client_with_data.get("/data?table=daily_metrics")
        assert "台幣升貶（Δ）" in resp.text
        assert "夜盤量比" in resp.text


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
    def test_create_scheduler_has_daily_jobs_plus_live_refresh(self):
        from server.scheduler import create_scheduler

        scheduler = create_scheduler(db_path=":memory:")
        jobs = {j.id for j in scheduler.get_jobs()}
        # 四個每日 job + 盤中即時行情背景刷新
        assert {"after_night", "before_open", "verify_close",
                "after_close"}.issubset(jobs)
        assert "live_refresh" in jobs

    def test_get_jobs_info_none_scheduler_lists_config(self, tmp_path):
        """scheduler 未啟用時仍列出四個 job 的設定（無 next_run）。"""
        from server.scheduler import get_jobs_info

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        create_all_tables(conn)
        conn.close()

        jobs = get_jobs_info(None, db_path=db_path)
        assert [j["id"] for j in jobs] == [
            "after_night", "before_open", "verify_close", "after_close"]
        assert all(j["next_run"] is None for j in jobs)
        assert jobs[1]["time_hhmm"] == "08:50"  # settings 預設

    def test_set_schedule_time_persists_and_reschedules(self, tmp_path):
        """改時間：寫入 DB、live scheduler 立即 reschedule、重建 scheduler 沿用。"""
        from server.scheduler import (
            create_scheduler, get_schedule_times, set_schedule_time,
        )

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        create_all_tables(conn)
        conn.close()

        scheduler = create_scheduler(db_path=db_path)
        ok = set_schedule_time("after_close", "20:15",
                               scheduler=scheduler, db_path=db_path)
        assert ok is True

        # live job 已改
        trigger = scheduler.get_job("after_close").trigger
        assert "hour='20'" in str(trigger) and "minute='15'" in str(trigger)
        # DB 持久化，重建 scheduler 沿用
        assert get_schedule_times(db_path)["after_close"] == "20:15"
        scheduler2 = create_scheduler(db_path=db_path)
        assert "hour='20'" in str(scheduler2.get_job("after_close").trigger)
        # 其他 job 不受影響
        assert get_schedule_times(db_path)["before_open"] == "08:50"

    def test_set_schedule_time_rejects_bad_input(self, tmp_path):
        from server.scheduler import set_schedule_time

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        create_all_tables(conn)
        conn.close()

        assert set_schedule_time("after_close", "25:00", db_path=db_path) is False
        assert set_schedule_time("after_close", "abc", db_path=db_path) is False
        assert set_schedule_time("nonexistent_job", "10:00", db_path=db_path) is False

    def test_run_job_now(self, tmp_path, monkeypatch):
        """run_job_now 在背景觸發一次 job 並記錄結果；None scheduler 回 False。"""
        import threading
        import server.scheduler as sched_mod
        from server.scheduler import create_scheduler, run_job_now

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        create_all_tables(conn)
        conn.close()

        assert run_job_now(None, "after_close", db_path=db_path) is False

        ran = threading.Event()

        def fake_job(db):
            ran.set()
            return {"status": "completed"}

        monkeypatch.setitem(sched_mod.JOB_DEFS["after_close"], "func", fake_job)
        scheduler = create_scheduler(db_path=db_path)
        scheduler.start()
        try:
            assert run_job_now(scheduler, "after_close", db_path=db_path) is True
            assert ran.wait(timeout=5), "manual job did not run within 5s"
            # 等 wrapper 寫完 _last_runs
            for _ in range(50):
                if "after_close" in sched_mod._last_runs:
                    break
                threading.Event().wait(0.1)
            assert sched_mod._last_runs["after_close"]["status"] == "completed"
        finally:
            scheduler.shutdown(wait=False)

        assert run_job_now(scheduler, "no_such_job", db_path=db_path) is False


class TestSchedulerRoutes:
    def test_scheduler_page_lists_jobs_when_disabled(self, client):
        """排程器未啟用仍顯示四個 job 的設定列。"""
        resp = client.get("/scheduler")
        assert resp.status_code == 200
        assert "after_close" in resp.text
        assert "排程器未啟用" in resp.text

    def test_post_time_updates_and_shows_msg(self, client):
        resp = client.post("/scheduler/time",
                           data={"job_id": "after_close", "time_hhmm": "19:00"},
                           follow_redirects=True)
        assert resp.status_code == 200
        assert "已改為 19:00" in resp.text
        assert 'value="19:00"' in resp.text

    def test_post_time_invalid_shows_error(self, client):
        resp = client.post("/scheduler/time",
                           data={"job_id": "after_close", "time_hhmm": "99:99"},
                           follow_redirects=True)
        assert "更新失敗" in resp.text

    def test_post_run_without_scheduler_fails_gracefully(self, client):
        resp = client.post("/scheduler/run", data={"job_id": "after_close"},
                           follow_redirects=True)
        assert resp.status_code == 200
        assert "觸發失敗" in resp.text


class TestTableUX:
    def test_data_page_sortable_and_filter(self, client_with_data):
        """資料瀏覽表格有排序 class 與過濾輸入框。"""
        resp = client_with_data.get("/data?table=raw_index")
        assert 'class="sortable"' in resp.text
        assert "data-filter" in resp.text

    def test_signals_page_sortable_and_filter(self, client_with_data):
        resp = client_with_data.get("/signals")
        assert 'class="sortable"' in resp.text
        assert "data-filter" in resp.text


class TestPagination:
    @staticmethod
    def _seed(tmp_path, n):
        """建立含 n 筆 raw_index 的 DB，回傳 TestClient。"""
        from datetime import datetime, timedelta
        db_path = str(tmp_path / "page.db")
        conn = sqlite3.connect(db_path)
        create_all_tables(conn)
        base = datetime(2026, 1, 1)
        for i in range(n):
            d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
            conn.execute("INSERT INTO raw_index (date, open, close) VALUES (?, ?, ?)",
                         (d, 100 + i, 200 + i))
        conn.commit()
        conn.close()
        return TestClient(create_app(db_path=db_path, enable_scheduler=False))

    def test_api_raw_pagination(self, tmp_path):
        """/api/raw 回 total 與分頁 limit/offset，一次只拉一頁。"""
        client = self._seed(tmp_path, 60)
        r = client.get("/api/raw/raw_index?limit=20&offset=0").json()
        assert r["total"] == 60
        assert r["count"] == 20
        assert r["limit"] == 20 and r["offset"] == 0
        # 第二頁
        r2 = client.get("/api/raw/raw_index?limit=20&offset=20").json()
        assert r2["count"] == 20
        assert r2["rows"][0]["date"] != r["rows"][0]["date"]

    def test_data_page_shows_pager(self, tmp_path):
        """資料頁超過一頁時顯示分頁控制。"""
        client = self._seed(tmp_path, 60)  # > DATA_PAGE_SIZE(50)
        resp = client.get("/data?table=raw_index")
        assert "下一頁" in resp.text
        assert "第 1 /" in resp.text

    def test_data_page_second_page(self, tmp_path):
        client = self._seed(tmp_path, 60)
        resp = client.get("/data?table=raw_index&page=2")
        assert resp.status_code == 200
        assert "第 2 /" in resp.text


class TestMobilePWA:
    def test_viewport_meta_present(self, client):
        """頁面含 viewport meta（手機正確縮放的關鍵）。"""
        resp = client.get("/")
        assert 'name="viewport"' in resp.text
        assert "width=device-width" in resp.text

    def test_manifest_served(self, client):
        """PWA manifest 回 200 且為 manifest 型別，含圖示。"""
        resp = client.get("/manifest.webmanifest")
        assert resp.status_code == 200
        assert "manifest" in resp.headers["content-type"]
        assert "icon-192.png" in resp.text

    def test_service_worker_served_at_root(self, client):
        """service worker 從根路徑提供（scope 涵蓋全站）。"""
        resp = client.get("/sw.js")
        assert resp.status_code == 200
        assert "javascript" in resp.headers["content-type"]
        assert "addEventListener" in resp.text

    def test_icons_served(self, client):
        """PWA 圖示靜態檔可取得且為 PNG。"""
        resp = client.get("/static/icons/icon-192.png")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"
