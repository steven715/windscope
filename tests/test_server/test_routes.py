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
    conn.execute(
        "INSERT INTO market_holidays (date, name, source, fetched_at) "
        "VALUES ('2026-06-19', '端午節', 'twse', '2026-06-01')"
    )
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

    def test_data_page_holidays_viewable(self, client_with_data):
        """休市日曆可在資料瀏覽頁查看（下拉選項 + 資料列 + 中文欄名）。"""
        resp = client_with_data.get("/data")
        assert "休市日曆（國定假日）" in resp.text  # 下拉選項
        resp = client_with_data.get("/data?table=market_holidays")
        assert resp.status_code == 200
        assert "端午節" in resp.text       # 資料列
        assert "假日名稱" in resp.text       # 欄位中文標籤

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
        # 籌碼分點收集出廠停用 → 不加入排程
        assert "chip_collect" not in jobs

    def test_disabled_job_excluded_then_added_on_enable(self, tmp_path):
        """停用的 job 不在排程；set_job_enabled(True) 後即時加入 live scheduler。"""
        import server.scheduler as sched

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        create_all_tables(conn)
        conn.close()

        scheduler = sched.create_scheduler(db_path=db_path)
        assert scheduler.get_job("chip_collect") is None       # 出廠停用
        assert sched.set_job_enabled("chip_collect", True,
                                     scheduler=scheduler, db_path=db_path)
        assert scheduler.get_job("chip_collect") is not None    # 啟用後即時加入
        # get_jobs_info 反映啟用狀態
        jobs = {j["id"]: j for j in sched.get_jobs_info(scheduler, db_path=db_path)}
        assert jobs["chip_collect"]["enabled"] is True

    def test_set_job_enabled_disables_and_removes(self, tmp_path):
        """啟用中的 job 被停用 → 從 live scheduler 移除、get_jobs_info 顯示 enabled False。"""
        import server.scheduler as sched

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        create_all_tables(conn)
        conn.close()

        scheduler = sched.create_scheduler(db_path=db_path)
        assert scheduler.get_job("after_close") is not None
        assert sched.set_job_enabled("after_close", False,
                                     scheduler=scheduler, db_path=db_path)
        assert scheduler.get_job("after_close") is None
        jobs = {j["id"]: j for j in sched.get_jobs_info(scheduler, db_path=db_path)}
        assert jobs["after_close"]["enabled"] is False

    def test_format_job_notify_verify(self):
        from server.scheduler import _format_job_notify
        result = {"date": "2026-06-16", "status": "completed", "verification": {
            "predicted_direction": "bullish", "confidence": 1,
            "day_change_class": "up", "day_change_pct": 0.91,
            "hit_day": 1, "hit_open": 0, "open_gap_pct": 0.23}}
        msg = _format_job_notify("verify_close", result)
        assert "命中" in msg and "+0.91%" in msg

    def test_format_job_notify_generic(self):
        from server.scheduler import _format_job_notify
        result = {"date": "2026-06-16", "status": "partial",
                  "results": {"a": True, "b": False}}
        msg = _format_job_notify("after_close", result)
        assert "1/2" in msg

    def test_get_jobs_info_none_scheduler_lists_config(self, tmp_path):
        """scheduler 未啟用時仍列出所有 job（含基礎設施）的設定（無 next_run）。"""
        from server.scheduler import get_jobs_info

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        create_all_tables(conn)
        conn.close()

        jobs = get_jobs_info(None, db_path=db_path)
        # 每日情報 job + 籌碼分點收集 + 基礎設施 job 皆列出，順序＝JOB_DEFS
        assert [j["id"] for j in jobs] == [
            "after_night", "before_open", "verify_close", "after_close",
            "afternoon_fx", "chip_collect", "live_refresh", "refresh_holidays"]
        assert all(j["next_run"] is None for j in jobs)
        assert jobs[1]["time_hhmm"] == "08:50"  # settings 預設

    def test_get_jobs_info_infra_jobs_readonly(self, tmp_path):
        """基礎設施 job：editable=False、無 time_hhmm、附人類可讀 schedule_label。"""
        from server.scheduler import get_jobs_info

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        create_all_tables(conn)
        conn.close()

        jobs = {j["id"]: j for j in get_jobs_info(None, db_path=db_path)}
        rh = jobs["refresh_holidays"]
        assert rh["editable"] is False
        assert rh["time_hhmm"] is None
        assert "每月" in rh["schedule_label"]
        lr = jobs["live_refresh"]
        assert lr["editable"] is False
        assert "秒" in lr["schedule_label"]
        # 每日 job 仍可編輯
        assert jobs["before_open"]["editable"] is True
        assert jobs["before_open"]["schedule_label"] == "每日 08:50"

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

    def test_build_trigger_kinds(self):
        """_build_trigger 依 kind 產出 daily/interval/monthly 對應的 trigger。"""
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        from server.scheduler import _build_trigger

        t, k = _build_trigger({"kind": "daily", "days": "mon-fri", "default": "08:50"})
        assert isinstance(t, CronTrigger) and k == {}

        t, k = _build_trigger(
            {"kind": "interval", "seconds": 12, "run_at_start": True})
        assert isinstance(t, IntervalTrigger) and "next_run_time" in k

        t, k = _build_trigger({"kind": "monthly", "day": 1, "hour": 6, "minute": 0})
        assert isinstance(t, CronTrigger)

    def test_runner_notifies_exactly_once_for_announced_job(self, monkeypatch):
        """重構修掉 double-notify：announce=True 的 job 每次只發一封通知。"""
        import server.scheduler as sched

        calls = []
        monkeypatch.setattr(sched, "notify",
                            lambda title, msg: calls.append((title, msg)))
        monkeypatch.setitem(
            sched.JOB_DEFS["before_open"], "func",
            lambda db: {"date": "2026-06-19", "status": "completed", "summary": "S"})

        sched._make_runner("before_open")(":memory:")
        assert len(calls) == 1
        assert calls[0][1] == "S"  # before_open 通知內容為情報摘要

    def test_runner_silent_for_infra_job(self, monkeypatch):
        """基礎設施 job（announce=False）執行後不發通知。"""
        import server.scheduler as sched

        calls = []
        monkeypatch.setattr(sched, "notify", lambda title, msg: calls.append(1))
        monkeypatch.setitem(
            sched.JOB_DEFS["refresh_holidays"], "func",
            lambda db: {"status": "completed", "fetched": 1, "cached": 1})

        sched._make_runner("refresh_holidays")(":memory:")
        assert calls == []


class TestJobRuns:
    """執行紀錄持久化（Slice 1）：_record_run 寫入、skip_logging 排除、查詢頁。"""

    def _db(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        create_all_tables(conn)
        conn.close()
        return db_path

    def test_runner_persists_run(self, tmp_path, monkeypatch):
        """一般 job 執行後在 job_runs 留下一筆，含狀態/觸發/歸屬日。"""
        import server.scheduler as sched

        db_path = self._db(tmp_path)
        monkeypatch.setattr(sched, "notify", lambda *a: None)
        monkeypatch.setitem(
            sched.JOB_DEFS["after_close"], "func",
            lambda db: {"date": "2026-06-19", "status": "completed",
                        "results": {"a": True, "b": True}})

        sched._make_runner("after_close")(db_path)

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT job_id, trigger_type, run_date, status, duration_ms "
            "FROM job_runs").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "after_close"
        assert rows[0][1] == "scheduled"
        assert rows[0][2] == "2026-06-19"
        assert rows[0][3] == "completed"
        assert rows[0][4] is not None  # duration recorded

    def test_manual_trigger_recorded(self, tmp_path, monkeypatch):
        """trigger_type='manual' 透過 _make_runner 帶入並寫入。"""
        import server.scheduler as sched

        db_path = self._db(tmp_path)
        monkeypatch.setattr(sched, "notify", lambda *a: None)
        monkeypatch.setitem(
            sched.JOB_DEFS["after_close"], "func",
            lambda db: {"date": "2026-06-19", "status": "partial", "results": {}})

        sched._make_runner("after_close", trigger_type="manual")(db_path)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT trigger_type FROM job_runs").fetchone()
        conn.close()
        assert row[0] == "manual"

    def test_skip_logging_excludes_live_refresh(self, tmp_path, monkeypatch):
        """skip_logging 的盤中刷新不寫 job_runs（避免高頻灌爆）。"""
        import server.scheduler as sched

        db_path = self._db(tmp_path)
        monkeypatch.setitem(sched.JOB_DEFS["live_refresh"], "func", lambda db: None)

        sched._make_runner("live_refresh")(db_path)

        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0]
        conn.close()
        assert count == 0

    def test_error_run_recorded_as_error(self, tmp_path, monkeypatch):
        """job 例外時 status 正規化為 'error'、error 欄存訊息，且不 raise。"""
        import server.scheduler as sched

        db_path = self._db(tmp_path)
        monkeypatch.setattr(sched, "notify", lambda *a: None)

        def boom(db):
            raise RuntimeError("模擬炸裂")

        monkeypatch.setitem(sched.JOB_DEFS["after_close"], "func", boom)

        sched._make_runner("after_close")(db_path)  # 不應拋出

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT status, error FROM job_runs").fetchone()
        conn.close()
        assert row[0] == "error"
        assert "模擬炸裂" in row[1]

    def test_persist_failure_never_crashes_runner(self, monkeypatch):
        """job_runs 表不存在（裸 :memory:）時寫入失敗只記 log，不影響執行。"""
        import server.scheduler as sched

        calls = []
        monkeypatch.setattr(sched, "notify", lambda *a: calls.append(1))
        monkeypatch.setitem(
            sched.JOB_DEFS["after_close"], "func",
            lambda db: {"date": "2026-06-19", "status": "completed", "results": {}})

        # :memory: 每次連線都是新空庫，無 job_runs 表 → 寫入失敗但被吞掉
        sched._make_runner("after_close")(":memory:")
        assert calls == [1]  # 通知照常發出

    def test_notify_formatter_failure_persists_run_and_no_crash(self, tmp_path,
                                                                monkeypatch):
        """通知格式化炸裂時：runner 不掛、紀錄照寫（summary 留空）。"""
        import server.scheduler as sched

        db_path = self._db(tmp_path)
        sent = []
        monkeypatch.setattr(sched, "notify", lambda *a: sent.append(1))

        def bad_notify(result):
            raise KeyError("hit_day")  # 模擬 result 結構異常

        monkeypatch.setitem(sched.JOB_DEFS["after_close"], "notify", bad_notify)
        monkeypatch.setitem(
            sched.JOB_DEFS["after_close"], "func",
            lambda db: {"date": "2026-06-19", "status": "completed", "results": {}})

        sched._make_runner("after_close")(db_path)  # 不應拋出

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT status, summary FROM job_runs").fetchone()
        conn.close()
        assert row is not None and row[0] == "completed"  # 紀錄仍寫入
        assert row[1] is None  # 壞掉的 summary 留空
        assert sent == []  # 通知格式化失敗 → 該次不發，但不影響流程

    def test_runs_page_empty(self, client):
        resp = client.get("/scheduler/runs")
        assert resp.status_code == 200
        assert "查無執行紀錄" in resp.text

    def test_runs_page_drops_invalid_date_filter(self, tmp_path):
        """非法日期格式視為不篩，不會誤回空集合。"""
        db_path = self._db(tmp_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO job_runs (job_id, trigger_type, run_date, started_at, "
            "status, summary) VALUES ('before_open', 'scheduled', '2026-06-18', "
            "'2026-06-18 08:50:00', 'completed', 'MARK_X')")
        conn.commit()
        conn.close()
        client = TestClient(create_app(db_path=db_path, enable_scheduler=False))

        resp = client.get("/scheduler/runs", params={"date_from": "banana"})
        assert resp.status_code == 200
        assert "MARK_X" in resp.text  # 非法日期被丟棄，仍查得到

    def test_runs_page_lists_and_filters(self, tmp_path):
        db_path = self._db(tmp_path)
        # 用獨特 summary marker 斷言（job 名稱會出現在篩選下拉選單，無法當判別依據）
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO job_runs (job_id, trigger_type, run_date, started_at, "
            "status, summary) VALUES ('before_open', 'scheduled', '2026-06-18', "
            "'2026-06-18 08:50:00', 'completed', 'MARK_MORNING')")
        conn.execute(
            "INSERT INTO job_runs (job_id, trigger_type, run_date, started_at, "
            "status, summary) VALUES ('after_close', 'manual', '2026-06-18', "
            "'2026-06-18 18:30:00', 'failed', 'MARK_EVENING')")
        conn.commit()
        conn.close()
        client = TestClient(create_app(db_path=db_path, enable_scheduler=False))

        resp = client.get("/scheduler/runs")
        assert resp.status_code == 200
        assert "MARK_MORNING" in resp.text and "MARK_EVENING" in resp.text

        # 篩 job_id
        resp = client.get("/scheduler/runs", params={"job_id": "before_open"})
        assert "MARK_MORNING" in resp.text and "MARK_EVENING" not in resp.text

        # 篩 status
        resp = client.get("/scheduler/runs", params={"status": "failed"})
        assert "MARK_EVENING" in resp.text and "MARK_MORNING" not in resp.text

    def test_runs_page_status_shown_in_chinese(self, tmp_path):
        """執行狀態以中文顯示（completed→完成、failed→失敗），下拉選項亦中文。"""
        db_path = self._db(tmp_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO job_runs (job_id, trigger_type, run_date, started_at, "
            "status, summary) VALUES ('before_open', 'scheduled', '2026-06-18', "
            "'2026-06-18 08:50:00', 'completed', 'MARK_ZH')")
        conn.commit()
        conn.close()
        client = TestClient(create_app(db_path=db_path, enable_scheduler=False))

        resp = client.get("/scheduler/runs")
        assert "完成" in resp.text  # 表格狀態欄 + 下拉選項
        assert "失敗" in resp.text  # 下拉選項仍列出所有狀態的中文

    def _seed_runs(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.executemany(
            "INSERT INTO job_runs (job_id, trigger_type, run_date, started_at, "
            "status) VALUES (?, ?, ?, ?, ?)",
            [("before_open", "scheduled", "2026-06-17", "2026-06-17 08:50:00", "completed"),
             ("before_open", "scheduled", "2026-06-18", "2026-06-18 08:50:00", "completed"),
             ("after_close", "manual", "2026-06-18", "2026-06-18 18:30:00", "failed")],
        )
        conn.commit()
        ids = [r[0] for r in conn.execute("SELECT id FROM job_runs ORDER BY id").fetchall()]
        conn.close()
        return ids

    def _count(self, db_path):
        conn = sqlite3.connect(db_path)
        n = conn.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0]
        conn.close()
        return n

    def test_delete_one(self, tmp_path):
        db_path = self._db(tmp_path)
        ids = self._seed_runs(db_path)
        client = TestClient(create_app(db_path=db_path, enable_scheduler=False))

        resp = client.post("/scheduler/runs/delete",
                           data={"mode": "one", "run_id": ids[0]},
                           follow_redirects=False)
        assert resp.status_code == 303
        assert self._count(db_path) == 2

    def test_delete_filter_bounded(self, tmp_path):
        db_path = self._db(tmp_path)
        self._seed_runs(db_path)
        client = TestClient(create_app(db_path=db_path, enable_scheduler=False))

        # 只刪 before_open → 留下 after_close 1 筆
        resp = client.post("/scheduler/runs/delete",
                           data={"mode": "filter", "job_id": "before_open"},
                           follow_redirects=False)
        assert resp.status_code == 303
        assert self._count(db_path) == 1

    def test_delete_filter_refuses_unbounded(self, tmp_path):
        """filter 模式無任何條件 → 拒絕，不刪任何東西。"""
        db_path = self._db(tmp_path)
        self._seed_runs(db_path)
        client = TestClient(create_app(db_path=db_path, enable_scheduler=False))

        resp = client.post("/scheduler/runs/delete", data={"mode": "filter"},
                           follow_redirects=False)
        assert resp.status_code == 303
        assert "%E5%88%AA%E9%99%A4%E5%A4%B1%E6%95%97" in resp.headers["location"]  # 刪除失敗
        assert self._count(db_path) == 3  # 一筆都沒刪

    def test_delete_all(self, tmp_path):
        db_path = self._db(tmp_path)
        self._seed_runs(db_path)
        client = TestClient(create_app(db_path=db_path, enable_scheduler=False))

        resp = client.post("/scheduler/runs/delete", data={"mode": "all"},
                           follow_redirects=False)
        assert resp.status_code == 303
        assert self._count(db_path) == 0

    def test_delete_one_missing_id_refused(self, tmp_path):
        db_path = self._db(tmp_path)
        self._seed_runs(db_path)
        client = TestClient(create_app(db_path=db_path, enable_scheduler=False))

        resp = client.post("/scheduler/runs/delete", data={"mode": "one"},
                           follow_redirects=False)
        assert resp.status_code == 303
        assert self._count(db_path) == 3  # 無 id → 不刪

    def test_api_job_runs_filter(self, tmp_path):
        db_path = self._db(tmp_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO job_runs (job_id, trigger_type, run_date, started_at, "
            "status) VALUES ('before_open', 'scheduled', '2026-06-18', "
            "'2026-06-18 08:50:00', 'completed')")
        conn.commit()
        conn.close()
        client = TestClient(create_app(db_path=db_path, enable_scheduler=False))

        resp = client.get("/api/job-runs")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["rows"][0]["job_id"] == "before_open"


class TestJobConfig:
    """自訂名稱 + 通知開關（Slice 2）：job_config 覆寫，job_id 永遠不變。"""

    def _db(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        create_all_tables(conn)
        conn.close()
        return db_path

    def test_get_jobs_info_defaults_without_overrides(self, tmp_path):
        """無覆寫時 name=預設、notify_enabled=announce 預設。"""
        import server.scheduler as sched

        db_path = self._db(tmp_path)
        jobs = {j["id"]: j for j in sched.get_jobs_info(None, db_path=db_path)}
        bo = jobs["before_open"]
        assert bo["name"] == bo["default_name"]
        assert bo["notify_enabled"] is True  # before_open announce 預設開

    def test_overrides_reflected_in_jobs_info(self, tmp_path):
        import server.scheduler as sched

        db_path = self._db(tmp_path)
        assert sched.set_job_display_name("after_close", "盤後收尾", db_path=db_path)
        assert sched.set_job_notify("after_close", False, db_path=db_path)

        jobs = {j["id"]: j for j in sched.get_jobs_info(None, db_path=db_path)}
        ac = jobs["after_close"]
        assert ac["name"] == "盤後收尾"
        assert ac["default_name"] != "盤後收尾"   # 預設仍保留供標示
        assert ac["notify_enabled"] is False
        # 改名只動 display_name、不影響 notify（獨立 upsert）；反之亦然
        assert sched.set_job_display_name("after_close", "盤後X", db_path=db_path)
        jobs = {j["id"]: j for j in sched.get_jobs_info(None, db_path=db_path)}
        assert jobs["after_close"]["notify_enabled"] is False

    def test_set_rejects_invalid(self, tmp_path):
        import server.scheduler as sched

        db_path = self._db(tmp_path)
        assert sched.set_job_display_name("before_open", "  ", db_path=db_path) is False
        assert sched.set_job_display_name("before_open", "x" * 61, db_path=db_path) is False
        assert sched.set_job_display_name("nope", "x", db_path=db_path) is False  # 未知 id
        # 通知仍僅每日 job 可設（防盤中刷新被打開狂發通知）
        assert sched.set_job_notify("live_refresh", True, db_path=db_path) is False
        assert sched.set_job_notify("nope", True, db_path=db_path) is False

    def test_live_refresh_never_reads_job_config(self, monkeypatch):
        """盤中刷新（每 12 秒）絕不讀 job_config——鎖死熱路徑零 DB I/O。"""
        import server.scheduler as sched

        reads = []
        monkeypatch.setattr(sched, "_job_override",
                            lambda jid, db: reads.append(jid) or {})
        monkeypatch.setitem(sched.JOB_DEFS["live_refresh"], "func", lambda db: None)

        sched._make_runner("live_refresh")(":memory:")
        assert reads == []  # 非 schedulable → runner 不查 job_config

    def test_live_refresh_skip_tick_not_recorded(self, monkeypatch):
        """盤中刷新空轉（func 回 None）不更新上次執行；真的抓到（回 dict）才更新——
        否則畫面每 12 秒顯示一次「完成」，與假日/非盤中其實在略過的實況不符。"""
        import server.scheduler as sched

        sched._last_runs.pop("live_refresh", None)
        monkeypatch.setattr(sched, "notify", lambda *a: None)
        try:
            # 略過 tick → 不記
            monkeypatch.setitem(sched.JOB_DEFS["live_refresh"], "func", lambda db: None)
            sched._make_runner("live_refresh")(":memory:")
            assert "live_refresh" not in sched._last_runs
            # 真的抓到 → 記成 completed
            monkeypatch.setitem(sched.JOB_DEFS["live_refresh"], "func",
                                lambda db: {"status": "completed"})
            sched._make_runner("live_refresh")(":memory:")
            assert sched._last_runs["live_refresh"]["status"] == "completed"
        finally:
            sched._last_runs.pop("live_refresh", None)

    def test_runner_respects_notify_override_off(self, tmp_path, monkeypatch):
        """使用者關閉通知後，排程執行不發 TG。"""
        import server.scheduler as sched

        db_path = self._db(tmp_path)
        assert sched.set_job_notify("after_close", False, db_path=db_path)
        calls = []
        monkeypatch.setattr(sched, "notify", lambda *a: calls.append(1))
        monkeypatch.setitem(
            sched.JOB_DEFS["after_close"], "func",
            lambda db: {"date": "2026-06-19", "status": "completed", "results": {}})

        sched._make_runner("after_close")(db_path)
        assert calls == []

    def test_runner_logs_effective_display_name(self, tmp_path, monkeypatch):
        """執行紀錄的 job_name 記使用者改過的顯示名（非靜態預設）。"""
        import server.scheduler as sched

        db_path = self._db(tmp_path)
        assert sched.set_job_display_name("after_close", "我的盤後", db_path=db_path)
        monkeypatch.setattr(sched, "notify", lambda *a: None)
        monkeypatch.setitem(
            sched.JOB_DEFS["after_close"], "func",
            lambda db: {"date": "2026-06-19", "status": "completed", "results": {}})

        sched._make_runner("after_close")(db_path)

        conn = sqlite3.connect(db_path)
        name = conn.execute("SELECT job_name FROM job_runs").fetchone()[0]
        conn.close()
        assert name == "我的盤後"

    def test_set_job_desc_persists_and_reverts(self, tmp_path):
        """自訂說明：存覆寫、清空還原預設；get_jobs_info 帶 desc/default_desc/旗標。"""
        import server.scheduler as sched

        db_path = self._db(tmp_path)
        assert sched.set_job_desc("after_close", "自訂說明A", db_path=db_path)
        ac = {j["id"]: j for j in sched.get_jobs_info(None, db_path=db_path)}["after_close"]
        assert ac["desc"] == "自訂說明A"
        assert ac["desc_overridden"] is True
        assert ac["default_desc"] != "自訂說明A"
        # 清空（純空白）→ 還原預設、旗標歸 False
        assert sched.set_job_desc("after_close", "   ", db_path=db_path)
        ac = {j["id"]: j for j in sched.get_jobs_info(None, db_path=db_path)}["after_close"]
        assert ac["desc"] == ac["default_desc"]
        assert ac["desc_overridden"] is False

    def test_set_job_desc_rejects(self, tmp_path):
        import server.scheduler as sched

        db_path = self._db(tmp_path)
        assert sched.set_job_desc("after_close", "x" * 301, db_path=db_path) is False
        assert sched.set_job_desc("nope", "x", db_path=db_path) is False  # 未知 id

    def test_set_name_desc_works_for_infra_jobs(self, tmp_path):
        """基礎設施 job（盤中刷新／休市日曆刷新）可改名稱與說明，並反映於 get_jobs_info。"""
        import server.scheduler as sched

        db_path = self._db(tmp_path)
        assert sched.set_job_display_name("live_refresh", "盤中刷新（自訂）", db_path=db_path)
        assert sched.set_job_desc("refresh_holidays", "我的假日刷新說明", db_path=db_path)

        jobs = {j["id"]: j for j in sched.get_jobs_info(None, db_path=db_path)}
        assert jobs["live_refresh"]["name"] == "盤中刷新（自訂）"
        assert jobs["refresh_holidays"]["desc"] == "我的假日刷新說明"
        # 但通知仍不可設（防 12 秒高頻 job 被打開狂發）
        assert sched.set_job_notify("live_refresh", True, db_path=db_path) is False

    def test_default_descs_are_normalized(self):
        """預設說明須已去頭尾空白且 ≤上限——否則 /scheduler/save 的「還原預設」
        比對會誤判成變更、再被 set_job_desc 拒絕，跳出莫名錯誤。"""
        import server.scheduler as sched

        for jid, d in sched.JOB_DEFS.items():
            desc = d.get("desc", "")
            assert desc == desc.strip(), f"{jid} desc 有多餘空白"
            assert len(desc) <= sched._MAX_DESC_LEN, f"{jid} desc 超過上限"

    def test_save_route_updates_name(self, client):
        resp = client.post("/scheduler/save",
                           data={"name__before_open": "晨間情報"},
                           follow_redirects=False)
        assert resp.status_code == 303
        page = client.get("/scheduler")
        assert "晨間情報" in page.text

    def test_save_route_notify_off(self, client):
        # radio 送 "0" → 關閉。before_open 預設開、現被關 → 顯示「預設開」提示
        resp = client.post("/scheduler/save",
                           data={"notify__before_open": "0"},
                           follow_redirects=False)
        assert resp.status_code == 303
        page = client.get("/scheduler")
        assert "預設開" in page.text

    def test_save_route_notify_toggle_roundtrip(self, client):
        """toggle 開時送 hidden0+checkbox1（取最後一個 1）→ 開；只送 0 → 關。"""
        # after_close 預設開 → 關掉
        client.post("/scheduler/save", data={"notify__after_close": "0"})
        page = client.get("/scheduler")
        assert "預設開" in page.text  # 預設開、現被關 → 顯示提示
        # 再用瀏覽器實際送法（hidden "0" + checkbox "1"）開回來 → 取最後一個值
        client.post("/scheduler/save", data={"notify__after_close": ["0", "1"]})
        page = client.get("/scheduler")
        assert "預設開" not in page.text  # 回到預設開 → 不再顯示提示

    def test_save_route_batch_updates(self, client):
        resp = client.post("/scheduler/save", data={
            "name__after_close": "盤後收尾",
            "desc__after_close": "我的自訂說明",
            "time__after_close": "20:15",
            "notify__after_close": "0",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert "盤後收尾" in resp.text
        assert "我的自訂說明" in resp.text
        assert 'value="20:15"' in resp.text
        assert "已儲存 4 項" in resp.text

    def test_save_route_enables_chip_collect(self, client):
        # 籌碼分點收集出廠停用 → 透過 enabled toggle(hidden0+checkbox1)打開
        resp = client.post("/scheduler/save",
                           data={"enabled__chip_collect": ["0", "1"]},
                           follow_redirects=True)
        assert resp.status_code == 200
        assert "已儲存 1 項變更" in resp.text

    def test_scheduler_page_renders_enable_toggle(self, client):
        resp = client.get("/scheduler")
        assert 'name="enabled__chip_collect"' in resp.text   # 啟用開關
        assert "已停用" in resp.text                          # chip_collect 出廠停用

    def test_save_route_infra_name_editable_notify_ignored(self, client):
        # 基礎設施 job：名稱可改、通知不可改（硬塞 notify 也被忽略）→ 只算 1 項變更
        resp = client.post("/scheduler/save",
                           data={"name__live_refresh": "盤中刷新X",
                                 "notify__live_refresh": "1"},
                           follow_redirects=True)
        assert resp.status_code == 200
        assert "盤中刷新X" in resp.text       # 名稱已變更
        assert "已儲存 1 項變更" in resp.text  # 只有名稱，通知被忽略


class TestSchedulerRoutes:
    def test_scheduler_page_lists_jobs_when_disabled(self, client):
        """排程器未啟用仍顯示四個 job 的設定列。"""
        resp = client.get("/scheduler")
        assert resp.status_code == 200
        assert "after_close" in resp.text
        assert "排程器未啟用" in resp.text

    def test_save_time_updates_and_shows(self, client):
        resp = client.post("/scheduler/save",
                           data={"time__after_close": "19:00"},
                           follow_redirects=True)
        assert resp.status_code == 200
        assert "已儲存 1 項" in resp.text
        assert 'value="19:00"' in resp.text

    def test_save_invalid_time_reported(self, client):
        resp = client.post("/scheduler/save",
                           data={"time__after_close": "99:99"},
                           follow_redirects=True)
        assert "失敗" in resp.text

    def test_scheduler_page_renders_revamped_controls(self, client):
        """改版後頁面：批次保存表單、通知 toggle 開關、可編輯說明 textarea。"""
        resp = client.get("/scheduler")
        assert resp.status_code == 200
        assert 'action="/scheduler/save"' in resp.text
        # 通知＝單一 toggle（class="switch"）＋ hidden 伴隨欄，非兩顆 radio
        assert 'class="switch"' in resp.text
        assert 'type="radio"' not in resp.text
        assert 'name="desc__after_close"' in resp.text
        assert "<textarea" in resp.text
        # job_id 徽章已移除：不再以 <span class="tag">after_close</span> 顯示純 id
        assert '<span class="tag">after_close' not in resp.text
        # 基礎設施排程也要出現：名稱／說明可編輯，但時間固定、通知欄不可調
        assert "休市日曆刷新" in resp.text
        assert "盤中即時行情刷新" in resp.text
        assert 'name="name__refresh_holidays"' in resp.text   # 可改名
        assert 'name="desc__refresh_holidays"' in resp.text   # 可改說明
        assert 'name="time__refresh_holidays"' not in resp.text  # 時間固定
        assert "系統排程（時間固定）" in resp.text

    def test_post_run_without_scheduler_fails_gracefully(self, client):
        resp = client.post("/scheduler/run", data={"job_id": "after_close"},
                           follow_redirects=True)
        assert resp.status_code == 200
        assert "觸發失敗" in resp.text


class TestTableUX:
    def test_data_page_sortable_and_filter(self, client_with_data):
        """資料瀏覽表格有排序 class 與過濾輸入框。"""
        resp = client_with_data.get("/data?table=raw_index")
        # class 可含其他 class（手機 stack 卡片化），只要排序 class 仍套在 table 上
        assert 'class="sortable' in resp.text
        assert "data-filter" in resp.text

    def test_signals_page_sortable_and_filter(self, client_with_data):
        resp = client_with_data.get("/signals")
        assert 'class="sortable' in resp.text
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


class TestChipImport:
    @staticmethod
    def _client(tmp_path):
        db_path = str(tmp_path / "chip.db")
        conn = sqlite3.connect(db_path)
        create_all_tables(conn)
        conn.execute("INSERT INTO watchlist (stock_id, stock_name, added_date, reason) "
                     "VALUES ('2330', '台積電', '2026-04-08', 't')")
        conn.commit()
        conn.close()
        return db_path, TestClient(create_app(db_path=db_path, enable_scheduler=False))

    def test_get_page(self, tmp_path):
        _, client = self._client(tmp_path)
        resp = client.get("/chip-import")
        assert resp.status_code == 200
        assert "分點籌碼匯入" in resp.text

    def test_post_writes_raw_chip(self, tmp_path):
        db_path, client = self._client(tmp_path)
        resp = client.post("/chip-import", data={
            "date": "2026-06-16", "stock_id": "2330", "close_price": "1000",
            "broker_name": ["兆豐-嘉義", "", ""],
            "buy": ["5000", "", ""], "sell": ["1000", "", ""],
        }, follow_redirects=True)
        assert resp.status_code == 200
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT buy_volume, sell_volume, net_volume FROM raw_chip "
            "WHERE date='2026-06-16' AND stock_id='2330' AND broker_name='兆豐-嘉義'"
        ).fetchone()
        conn.close()
        assert row == (5000, 1000, 4000)

    def test_post_empty_rows(self, tmp_path):
        _, client = self._client(tmp_path)
        resp = client.post("/chip-import", data={
            "date": "2026-06-16", "stock_id": "2330", "close_price": "",
            "broker_name": ["", ""], "buy": ["", ""], "sell": ["", ""],
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert "沒有有效資料列" in resp.text

    def test_ocr_disabled_without_key(self, tmp_path):
        """未設 API key → OCR endpoint 回 enabled:False，不報錯。"""
        _, client = self._client(tmp_path)
        resp = client.post("/chip-import/ocr",
                           files={"image": ("x.png", b"fake", "image/png")})
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is False


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
