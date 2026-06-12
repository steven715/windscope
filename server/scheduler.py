"""APScheduler 排程：取代 crontab，在 server 內按時間軸觸發各 job。

排程時間：預設值在 config/settings.py（SCHEDULE_*），可由 Web 排程頁覆寫，
覆寫值存於 DB 的 schedule_config 表（job_id, time_hhmm），重啟後沿用。
時區跟隨系統（Docker 部署時以 TZ=Asia/Taipei 設定）。
"""

import logging
import re
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config import settings
from db.connection import get_connection

logger = logging.getLogger(__name__)

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# 各 job 的最近一次執行結果（手動或排程觸發都記），供排程頁顯示。
# 只存在記憶體：重啟後清空，歷史結果以 log 為準。
_last_runs: dict[str, dict] = {}


def _today() -> str:
    """執行當下的日期（YYYY-MM-DD）。"""
    return datetime.now().strftime("%Y-%m-%d")


def _parse_hhmm(hhmm: str) -> tuple[int, int]:
    """'08:50' -> (8, 50)。"""
    h, m = hhmm.split(":")
    return int(h), int(m)


def _run_after_night(db_path: str | None) -> dict:
    from jobs.after_night import run_after_night

    result = run_after_night(_today(), db_path=db_path)
    logger.info("scheduled after_night: %s", result["status"])
    return result


def _run_before_open(db_path: str | None) -> dict:
    from jobs.before_open import run_before_open
    from utils.notify import notify

    result = run_before_open(_today(), db_path=db_path)
    logger.info("scheduled before_open: %s", result["status"])
    if result.get("summary"):
        notify("開盤前情報", result["summary"])
    return result


def _run_verify_close(db_path: str | None) -> dict:
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
    return result


def _run_after_close(db_path: str | None) -> dict:
    from jobs.after_close import run_after_close

    result = run_after_close(_today(), db_path=db_path)
    logger.info("scheduled after_close: %s", result["status"])
    return result


# job 定義：id → 名稱、執行函式、cron 星期、預設時間。dict 順序即頁面顯示順序。
JOB_DEFS: dict[str, dict] = {
    "after_night": {
        "name": "夜盤後收集 (週二~六)",
        "func": _run_after_night,
        "days": "tue-sat",
        "default": settings.SCHEDULE_AFTER_NIGHT,
    },
    "before_open": {
        "name": "開盤前收集+訊號判斷 (週一~五)",
        "func": _run_before_open,
        "days": "mon-fri",
        "default": settings.SCHEDULE_BEFORE_OPEN,
    },
    "verify_close": {
        "name": "收盤驗證 (週一~五)",
        "func": _run_verify_close,
        "days": "mon-fri",
        "default": settings.SCHEDULE_VERIFY_CLOSE,
    },
    "after_close": {
        "name": "收盤後收集 (週一~五)",
        "func": _run_after_close,
        "days": "mon-fri",
        "default": settings.SCHEDULE_AFTER_CLOSE,
    },
}


def _make_runner(job_id: str):
    """包一層執行器：跑 job 並把結果記到 _last_runs。"""
    def runner(db_path: str | None) -> None:
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            result = JOB_DEFS[job_id]["func"](db_path)
            status = result.get("status", "unknown") if isinstance(result, dict) else "done"
        except Exception as e:
            logger.error("job %s crashed: %s", job_id, e)
            status = f"error: {str(e)[:80]}"
        _last_runs[job_id] = {"time": started, "status": status}
    return runner


def get_schedule_times(db_path: str | None = None) -> dict[str, str]:
    """回傳各 job 的排程時間（HH:MM）：DB 覆寫值優先，否則用 settings 預設。"""
    times = {job_id: d["default"] for job_id, d in JOB_DEFS.items()}
    try:
        with get_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT job_id, time_hhmm FROM schedule_config"
            ).fetchall()
        for job_id, hhmm in rows:
            if job_id in times and _HHMM_RE.match(hhmm or ""):
                times[job_id] = hhmm
    except Exception as e:
        # 表不存在或 DB 不可讀時退回預設，不阻擋 server 啟動
        logger.warning("get_schedule_times fallback to defaults: %s", e)
    return times


def set_schedule_time(job_id: str, hhmm: str,
                      scheduler: BackgroundScheduler | None = None,
                      db_path: str | None = None) -> bool:
    """更新 job 的排程時間：寫入 DB，scheduler 運行中時立即 reschedule。"""
    if job_id not in JOB_DEFS or not _HHMM_RE.match(hhmm or ""):
        logger.error("set_schedule_time rejected: job_id=%s time=%s", job_id, hhmm)
        return False

    now = datetime.now().isoformat()
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO schedule_config (job_id, time_hhmm, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET "
            " time_hhmm = excluded.time_hhmm, updated_at = excluded.updated_at",
            (job_id, hhmm, now),
        )

    if scheduler is not None:
        h, m = _parse_hhmm(hhmm)
        scheduler.reschedule_job(
            job_id,
            trigger=CronTrigger(day_of_week=JOB_DEFS[job_id]["days"], hour=h, minute=m),
        )
    logger.info("schedule time updated: %s -> %s", job_id, hhmm)
    return True


def run_job_now(scheduler: BackgroundScheduler | None, job_id: str,
                db_path: str | None = None) -> bool:
    """立即在背景觸發一次 job（不影響原排程）。scheduler 未啟用或 job 不存在回 False。"""
    if scheduler is None or job_id not in JOB_DEFS:
        return False
    scheduler.add_job(
        _make_runner(job_id), args=[db_path],
        id=f"{job_id}__manual", name=f"{JOB_DEFS[job_id]['name']}（手動）",
        replace_existing=True,
    )
    logger.info("manual run triggered: %s", job_id)
    return True


def create_scheduler(db_path: str | None = None) -> BackgroundScheduler:
    """建立並設定四個每日 job 的 scheduler（未啟動）。時間取 DB 覆寫值或預設。"""
    scheduler = BackgroundScheduler()
    times = get_schedule_times(db_path)

    for job_id, d in JOB_DEFS.items():
        h, m = _parse_hhmm(times[job_id])
        scheduler.add_job(
            _make_runner(job_id),
            CronTrigger(day_of_week=d["days"], hour=h, minute=m),
            args=[db_path], id=job_id, name=d["name"],
        )

    logger.info("Scheduler created with %d jobs", len(scheduler.get_jobs()))
    return scheduler


def get_jobs_info(scheduler: BackgroundScheduler | None,
                  db_path: str | None = None) -> list[dict]:
    """回傳排程狀態供頁面顯示：時間、下次執行、上次結果。scheduler 未啟用時仍列出設定。"""
    times = get_schedule_times(db_path)
    jobs = []
    for job_id, d in JOB_DEFS.items():
        next_run = None
        if scheduler is not None:
            job = scheduler.get_job(job_id)
            nrt = getattr(job, "next_run_time", None) if job else None
            next_run = nrt.strftime("%Y-%m-%d %H:%M:%S") if nrt else None
        jobs.append({
            "id": job_id,
            "name": d["name"],
            "time_hhmm": times[job_id],
            "default_time": d["default"],
            "next_run": next_run,
            "last_run": _last_runs.get(job_id),
        })
    return jobs
