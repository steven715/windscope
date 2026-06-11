"""APScheduler 排程：取代 crontab，在 server 內按時間軸觸發各 job。

排程時間集中在 config/settings.py（SCHEDULE_*）。
時區跟隨系統（Docker 部署時以 TZ=Asia/Taipei 設定）。
"""

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config import settings

logger = logging.getLogger(__name__)


def _today() -> str:
    """執行當下的日期（YYYY-MM-DD）。"""
    return datetime.now().strftime("%Y-%m-%d")


def _parse_hhmm(hhmm: str) -> tuple[int, int]:
    """'08:50' -> (8, 50)。"""
    h, m = hhmm.split(":")
    return int(h), int(m)


def _run_after_night(db_path: str | None) -> None:
    from jobs.after_night import run_after_night

    result = run_after_night(_today(), db_path=db_path)
    logger.info("scheduled after_night: %s", result["status"])


def _run_before_open(db_path: str | None) -> None:
    from jobs.before_open import run_before_open
    from utils.notify import notify

    result = run_before_open(_today(), db_path=db_path)
    logger.info("scheduled before_open: %s", result["status"])
    if result.get("summary"):
        notify("開盤前情報", result["summary"])


def _run_verify_close(db_path: str | None) -> None:
    from jobs.verify_close import run_verify_close
    from utils.notify import notify

    result = run_verify_close(_today(), db_path=db_path)
    logger.info("scheduled verify_close: %s", result["status"])
    v = result.get("verification")
    if v:
        hit_label = "✓ 命中" if v["hit_day"] else "✗ 失誤"
        notify(
            "收盤驗證",
            f"{v['date']} 預測 {v['predicted_direction']} (信心 {v['confidence']}) "
            f"vs 實際 {v['day_change_class']} ({v['day_change_pct']:+.2f}%) → {hit_label}",
        )


def _run_after_close(db_path: str | None) -> None:
    from jobs.after_close import run_after_close

    result = run_after_close(_today(), db_path=db_path)
    logger.info("scheduled after_close: %s", result["status"])


def create_scheduler(db_path: str | None = None) -> BackgroundScheduler:
    """建立並設定四個每日 job 的 scheduler（未啟動）。"""
    scheduler = BackgroundScheduler()

    h, m = _parse_hhmm(settings.SCHEDULE_AFTER_NIGHT)
    scheduler.add_job(
        _run_after_night, CronTrigger(day_of_week="tue-sat", hour=h, minute=m),
        args=[db_path], id="after_night", name="夜盤後收集 (週二~六)",
    )

    h, m = _parse_hhmm(settings.SCHEDULE_BEFORE_OPEN)
    scheduler.add_job(
        _run_before_open, CronTrigger(day_of_week="mon-fri", hour=h, minute=m),
        args=[db_path], id="before_open", name="開盤前收集+訊號判斷 (週一~五)",
    )

    h, m = _parse_hhmm(settings.SCHEDULE_VERIFY_CLOSE)
    scheduler.add_job(
        _run_verify_close, CronTrigger(day_of_week="mon-fri", hour=h, minute=m),
        args=[db_path], id="verify_close", name="收盤驗證 (週一~五)",
    )

    h, m = _parse_hhmm(settings.SCHEDULE_AFTER_CLOSE)
    scheduler.add_job(
        _run_after_close, CronTrigger(day_of_week="mon-fri", hour=h, minute=m),
        args=[db_path], id="after_close", name="收盤後收集 (週一~五)",
    )

    logger.info("Scheduler created with %d jobs", len(scheduler.get_jobs()))
    return scheduler


def get_jobs_info(scheduler: BackgroundScheduler | None) -> list[dict]:
    """回傳排程狀態供頁面顯示。scheduler 未啟用時回傳空清單。"""
    if scheduler is None:
        return []
    jobs = []
    for job in scheduler.get_jobs():
        next_run = getattr(job, "next_run_time", None)
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run else None,
        })
    return jobs
