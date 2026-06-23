"""排程器設定測試：misfire grace 必須套到 job，避免到點慢一拍被整個跳過。"""

import sqlite3

from config import settings
from db.schema import create_all_tables
from server.scheduler import create_scheduler


def test_jobs_have_misfire_grace(tmp_path):
    """每日 job（before_open）與 interval job（live_refresh）都要帶 misfire_grace_time。

    回歸 bug：預設 grace 1s，executor 抖動 1s 即跳過 → 08:50 整天不產訊號。
    """
    db = str(tmp_path / "sched.db")
    conn = sqlite3.connect(db)
    create_all_tables(conn)
    conn.close()

    scheduler = create_scheduler(db_path=db)
    scheduler.start(paused=True)  # 物化 job 但不執行（paused，不打網路）
    try:
        before = scheduler.get_job("before_open")
        assert before is not None
        assert before.misfire_grace_time == settings.SCHEDULE_MISFIRE_GRACE_SEC

        live = scheduler.get_job("live_refresh")
        assert live is not None
        assert live.misfire_grace_time == settings.SCHEDULE_MISFIRE_GRACE_SEC
    finally:
        scheduler.shutdown(wait=False)
