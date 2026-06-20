"""APScheduler 排程：取代 crontab，在 server 內按時間軸觸發各 job。

設計（重構後）：所有排程任務——4 個每日情報 job 與 2 個基礎設施 job（盤中即時刷新、
休市日曆刷新）——統一登錄於 JOB_DEFS，各自帶一個 trigger 規格（daily / interval /
monthly）。三件事各自獨立、不互相耦合：

- **執行什麼**：`func`，簽名統一為 `(db_path) -> result|None`，本身不發通知。
- **何時執行**：`trigger` 規格，由 `_build_trigger` 轉成 APScheduler trigger。
- **如何回報**：`notify` 每個 job 自帶的訊息格式化函式（result -> 文字），統一只在
  `_make_runner` 發一次通知（`announce=False` 的基礎設施 job 不發）。

每日 job 的排程時間預設在 config/settings.py（SCHEDULE_*），可由 Web 排程頁覆寫，
覆寫值存於 DB 的 schedule_config 表（job_id, time_hhmm），重啟後沿用。
時區跟隨系統（Docker 部署時以 TZ=Asia/Taipei 設定）。
"""

import json
import logging
import re
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import settings
from db.connection import get_connection
from utils.notify import notify

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


# ── job 執行函式 ─────────────────────────────────────────────────
# 簽名統一為 (db_path) -> result|None。通知由 _make_runner 統一處理，此處不發。


def _run_after_night(db_path: str | None) -> dict:
    from jobs.after_night import run_after_night

    result = run_after_night(_today(), db_path=db_path)
    logger.info("scheduled after_night: %s", result["status"])
    return result


def _run_before_open(db_path: str | None) -> dict:
    from jobs.before_open import run_before_open

    result = run_before_open(_today(), db_path=db_path)
    logger.info("scheduled before_open: %s", result["status"])
    return result


def _run_verify_close(db_path: str | None) -> dict:
    from jobs.verify_close import run_verify_close

    result = run_verify_close(_today(), db_path=db_path)
    logger.info("scheduled verify_close: %s", result["status"])
    return result


def _run_after_close(db_path: str | None) -> dict:
    from jobs.after_close import run_after_close

    result = run_after_close(_today(), db_path=db_path)
    logger.info("scheduled after_close: %s", result["status"])
    return result


def _run_afternoon_fx(db_path: str | None) -> dict:
    from jobs.afternoon_fx import run_afternoon_fx

    result = run_afternoon_fx(_today(), db_path=db_path)
    logger.info("scheduled afternoon_fx: %s", result["status"])
    return result


def _run_live_refresh(db_path: str | None) -> dict | None:
    """盤中即時行情背景刷新：抓 MIS 存記憶體快取。非刷新時段內部自動 no-op。

    有真的抓到 → 回 {"status": "completed"}；略過（非交易日/非時段/無回應）→ 回 None，
    讓排程器不要把這次空轉 tick 記成「上次執行」（否則畫面每 12 秒顯示一次完成）。
    """
    from integration.live_tracker import refresh_live_quote

    return {"status": "completed"} if refresh_live_quote() else None


def _run_refresh_holidays(db_path: str | None) -> dict:
    from jobs.refresh_holidays import run_refresh_holidays

    return run_refresh_holidays(db_path)


# ── 各 job 的通知文字（result -> 訊息；回 None 表示交給 generic）──────


def _notify_generic(result) -> str:
    """泛用格式：date status（X/Y 步驟成功）＋前幾條失敗訊息。"""
    if not isinstance(result, dict):
        return "完成"
    status = result.get("status", "unknown")
    date = result.get("date", "")
    steps = result.get("results", {})
    oks = sum(1 for ok in steps.values() if ok)
    msg = f"{date} status={status}（{oks}/{len(steps)} 步驟成功）"
    if result.get("errors"):
        msg += "\n失敗：" + "；".join(result["errors"][:3])
    return msg


def _notify_before_open(result) -> str | None:
    """開盤前：有完整情報摘要（含訊號）就直接發摘要，否則交給 generic。"""
    if isinstance(result, dict) and result.get("summary"):
        return result["summary"]
    return None


def _notify_verify_close(result) -> str | None:
    """收盤驗證：預測 vs 實際、命中與否、開盤跳空。無驗證結果交給 generic。"""
    v = result.get("verification") if isinstance(result, dict) else None
    if not v:
        return None
    date = result.get("date", "")
    hit = "✓ 命中" if v["hit_day"] else "✗ 失誤"
    gap = "✓" if v["hit_open"] else "✗"
    return (f"{date} 收盤驗證\n預測 {v['predicted_direction']}（信心 {v['confidence']}）"
            f" vs 實際 {v['day_change_class']} {v['day_change_pct']:+.2f}% → {hit}\n"
            f"開盤跳空 {v['open_gap_pct']:+.2f}% {gap}")


# job 定義：id → 中繼資料 + trigger 規格 + 通知格式。dict 順序即頁面顯示順序。
#
# 欄位：
#   name/desc    顯示用名稱與說明
#   func         執行函式 (db_path) -> result|None
#   trigger      排程規格，kind 為 daily/interval/monthly（見 _build_trigger）
#   notify       result -> 訊息文字的格式化函式（回 None 則用 _notify_generic）
#   schedulable  True 才出現在 Web 排程頁、才能調整時間（每日情報 job）
#   announce     True 才在每次執行後發 TG 通知（基礎設施 job 設 False）
JOB_DEFS: dict[str, dict] = {
    "after_night": {
        "name": "夜盤後收集 (週二~六)",
        "desc": "夜盤台指期收盤/量＋S&P 500 → 算價差、除息調整、夜盤量比。"
                "夜盤歸次一營業日，故週二~六。",
        "func": _run_after_night,
        "trigger": {"kind": "daily", "days": "tue-sat",
                    "default": settings.SCHEDULE_AFTER_NIGHT},
        "notify": _notify_generic,
        "schedulable": True,
        "announce": True,
    },
    "before_open": {
        "name": "開盤前收集+訊號判斷 (週一~五)",
        "desc": "即時匯率(台銀/Yahoo) → 升貶＋亞幣同步 → 合成市場訊號(偏多/偏空＋信心)"
                "與個股觀察訊號，產出開盤前情報。休市日只更新匯率統計、不產生訊號。",
        "func": _run_before_open,
        "trigger": {"kind": "daily", "days": "mon-fri",
                    "default": settings.SCHEDULE_BEFORE_OPEN},
        "notify": _notify_before_open,
        "schedulable": True,
        "announce": True,
    },
    "verify_close": {
        "name": "收盤驗證 (週一~五)",
        "desc": "收當日加權指數，雙基準三分類(收盤漲跌＋開盤跳空，±0.3%)比對早上訊號"
                "是否命中，累積命中率。",
        "func": _run_verify_close,
        "trigger": {"kind": "daily", "days": "mon-fri",
                    "default": settings.SCHEDULE_VERIFY_CLOSE},
        "notify": _notify_verify_close,
        "schedulable": True,
        "announce": True,
    },
    "after_close": {
        "name": "收盤後收集 (週一~五)",
        "desc": "三大法人／外資個股／個股收盤／除息／外資未平倉／USD與CNY/KRW/JPY收盤／分點"
                "→ 算籌碼指標(隔日基準)。",
        "func": _run_after_close,
        "trigger": {"kind": "daily", "days": "mon-fri",
                    "default": settings.SCHEDULE_AFTER_CLOSE},
        "notify": _notify_generic,
        "schedulable": True,
        "announce": True,
    },
    "afternoon_fx": {
        "name": "午盤匯率收集 (週一~五)",
        "desc": "午後再抓一次 USD/TWD 與 CNY/KRW/JPY 匯率(quote_pm 槽)，供盤中觀察。"
                "不產生訊號、不影響早盤升貶基準。預設不發通知，可於上方開關開啟。",
        "func": _run_afternoon_fx,
        "trigger": {"kind": "daily", "days": "mon-fri",
                    "default": settings.SCHEDULE_AFTERNOON_FX},
        "notify": _notify_generic,
        "schedulable": True,
        "announce": False,
    },
    # ── 基礎設施 job：不在排程頁顯示、不發通知 ──
    "live_refresh": {
        "name": "盤中即時行情刷新",
        "desc": "定時抓台股加權指數即時行情（證交所 MIS 即時資訊系統 mis.twse.com.tw）"
                "存入記憶體快取，供 /live 盤中驗證頁即時顯示。僅交易日盤中時段實際連網，"
                "週末／國定假日與非盤中時段自動略過、不打網路。",
        "func": _run_live_refresh,
        "trigger": {"kind": "interval", "seconds": settings.LIVE_REFRESH_SECONDS,
                    "run_at_start": True},
        "notify": None,
        "schedulable": False,
        "announce": False,
        # 每 ~12 秒一次，一天上千筆——絕不寫入 job_runs 執行紀錄，否則灌爆 DB。
        "skip_logging": True,
    },
    "refresh_holidays": {
        "name": "休市日曆刷新（每月）",
        "desc": "抓 TWSE 假日表寫入 DB 並刷新交易日曆快取。啟動時抓一次，之後每月 1 日。",
        "func": _run_refresh_holidays,
        "trigger": {"kind": "monthly", "day": 1, "hour": 6, "minute": 0,
                    "run_at_start": True},
        "notify": None,
        "schedulable": False,
        "announce": False,
    },
}


def _schedulable_ids() -> list[str]:
    """可由使用者調整時間、顯示於排程頁的 job id（每日情報 job）。"""
    return [jid for jid, d in JOB_DEFS.items() if d.get("schedulable")]


def _format_job_notify(job_id: str, result) -> str:
    """組出該 job 完成後要發的 TG 訊息內容：走 job 自帶 notify，回 None 則用 generic。"""
    spec = JOB_DEFS.get(job_id)
    notifier = spec.get("notify") if spec else None
    msg = notifier(result) if notifier else None
    return msg if msg is not None else _notify_generic(result)


def _build_trigger(trigger_spec: dict, hhmm: str | None = None):
    """把 trigger 規格轉成 (APScheduler trigger, add_job 額外 kwargs)。

    kind:
      daily   —— 每週 days 指定的星期、HH:MM（hhmm 覆寫值優先，否則用 default）
      interval—— 每 seconds 秒；run_at_start 則啟動時先跑一次
      monthly —— 每月 day 日 hour:minute；run_at_start 則啟動時先跑一次
    """
    kind = trigger_spec["kind"]
    if kind == "daily":
        h, m = _parse_hhmm(hhmm or trigger_spec["default"])
        return CronTrigger(day_of_week=trigger_spec["days"], hour=h, minute=m), {}

    if kind in ("interval", "monthly"):
        kwargs = {"max_instances": 1, "coalesce": True, "replace_existing": True}
        if trigger_spec.get("run_at_start"):
            kwargs["next_run_time"] = datetime.now()
        if kind == "interval":
            return IntervalTrigger(seconds=trigger_spec["seconds"]), kwargs
        return CronTrigger(day=trigger_spec["day"], hour=trigger_spec["hour"],
                           minute=trigger_spec["minute"]), kwargs

    raise ValueError(f"unknown trigger kind: {kind}")


# 顯示名稱長度上限（避免破版／TG 標題過長）
_MAX_NAME_LEN = 60
# 自訂說明長度上限（避免破版）
_MAX_DESC_LEN = 300


def get_job_overrides(db_path: str | None = None) -> dict[str, dict]:
    """回傳 {job_id: {display_name, display_desc, notify_enabled}}（讀 job_config）。

    供 get_jobs_info 一次取全部覆寫。表不存在或讀取失敗回 {}，退回 JOB_DEFS 預設。
    """
    out: dict[str, dict] = {}
    try:
        with get_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT job_id, display_name, display_desc, notify_enabled "
                "FROM job_config"
            ).fetchall()
        for jid, name, desc, notify_en in rows:
            out[jid] = {"display_name": name, "display_desc": desc,
                        "notify_enabled": notify_en}
    except Exception as e:
        logger.warning("get_job_overrides fallback to empty: %s", e)
    return out


def _job_override(job_id: str, db_path: str | None) -> dict:
    """讀單一 job 的覆寫設定 {display_name, display_desc, notify_enabled}。無列回 {}。"""
    try:
        with get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT display_name, display_desc, notify_enabled "
                "FROM job_config WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return {}
        return {"display_name": row[0], "display_desc": row[1],
                "notify_enabled": row[2]}
    except Exception as e:
        logger.warning("job_config read failed for %s: %s", job_id, e)
        return {}


def set_job_display_name(job_id: str, name: str,
                         scheduler: BackgroundScheduler | None = None,
                         db_path: str | None = None) -> bool:
    """設定 job 的顯示名稱（覆寫 JOB_DEFS 預設）。job_id 本身永遠不變。

    任何已知 job（含基礎設施 job）皆可改名；名稱去頭尾空白後不可為空、不可超過上限。
    """
    spec = JOB_DEFS.get(job_id)
    if spec is None:
        logger.error("set_job_display_name rejected: job_id=%s", job_id)
        return False
    name = (name or "").strip()
    if not name or len(name) > _MAX_NAME_LEN:
        logger.error("set_job_display_name rejected name: %r", name)
        return False

    now = datetime.now().isoformat()
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO job_config (job_id, display_name, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET "
            " display_name = excluded.display_name, updated_at = excluded.updated_at",
            (job_id, name, now),
        )
    # APScheduler job.name 是 cosmetic（手動執行標籤用）；id 永不變
    if scheduler is not None:
        try:
            scheduler.modify_job(job_id, name=name)
        except Exception as e:
            logger.warning("modify_job name failed for %s: %s", job_id, e)
    logger.info("job display name updated: %s -> %s", job_id, name)
    return True


def set_job_desc(job_id: str, desc: str,
                 scheduler: BackgroundScheduler | None = None,
                 db_path: str | None = None) -> bool:
    """設定 job 的顯示說明（覆寫 JOB_DEFS 預設）。

    任何已知 job（含基礎設施 job）皆可設；去頭尾空白後為空＝清除覆寫、還原預設說明；
    非空則存為覆寫，長度不可超過上限。scheduler 參數僅為簽名一致，不使用。
    """
    spec = JOB_DEFS.get(job_id)
    if spec is None:
        logger.error("set_job_desc rejected: job_id=%s", job_id)
        return False
    desc = (desc or "").strip()
    if len(desc) > _MAX_DESC_LEN:
        logger.error("set_job_desc rejected desc len=%d", len(desc))
        return False

    now = datetime.now().isoformat()
    # 空白 → 存 NULL（還原預設）；非空 → 存覆寫
    value = desc or None
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO job_config (job_id, display_desc, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET "
            " display_desc = excluded.display_desc, updated_at = excluded.updated_at",
            (job_id, value, now),
        )
    logger.info("job desc updated: %s -> %s", job_id, "(default)" if value is None else value)
    return True


def set_job_notify(job_id: str, enabled: bool,
                   scheduler: BackgroundScheduler | None = None,
                   db_path: str | None = None) -> bool:
    """設定每日 job 是否發 TG 通知（覆寫 announce 預設）。僅每日 job 可設。"""
    spec = JOB_DEFS.get(job_id)
    if spec is None or not spec.get("schedulable"):
        logger.error("set_job_notify rejected: job_id=%s", job_id)
        return False

    now = datetime.now().isoformat()
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO job_config (job_id, notify_enabled, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET "
            " notify_enabled = excluded.notify_enabled, updated_at = excluded.updated_at",
            (job_id, 1 if enabled else 0, now),
        )
    logger.info("job notify updated: %s -> %s", job_id, enabled)
    return True


def _record_run(job_id: str, job_name: str | None, trigger_type: str,
                started_dt: datetime, finished_dt: datetime, status: str,
                result, error: str | None, db_path: str | None) -> None:
    """把一次執行寫入 job_runs 持久化紀錄。

    自帶 try/except——寫入失敗只記 log，絕不讓 job 掛掉或擋下通知。
    skip_logging 的 job（盤中刷新）直接跳過，避免高頻寫入灌爆 DB。
    """
    spec = JOB_DEFS.get(job_id, {})
    if spec.get("skip_logging"):
        return
    try:
        # status 正規化：例外時存成乾淨的 'error'（error 欄存訊息）方便篩選
        clean_status = "error" if error is not None else status
        is_dict = isinstance(result, dict)
        # summary 格式化失敗不該讓整筆紀錄消失，獨立包一層、壞了就留空
        try:
            summary = _format_job_notify(job_id, result) if is_dict else None
        except Exception:
            summary = None
        result_json = (json.dumps(result, ensure_ascii=False, default=str)
                       if is_dict else None)
        run_date = (result.get("date") if is_dict else None) or _today()
        duration_ms = int((finished_dt - started_dt).total_seconds() * 1000)
        with get_connection(db_path) as conn:
            conn.execute(
                "INSERT INTO job_runs (job_id, job_name, trigger_type, run_date, "
                "started_at, finished_at, duration_ms, status, summary, error, "
                "result_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, job_name or spec.get("name"), trigger_type, run_date,
                 started_dt.strftime("%Y-%m-%d %H:%M:%S"),
                 finished_dt.strftime("%Y-%m-%d %H:%M:%S"),
                 duration_ms, clean_status, summary, error, result_json),
            )
    except Exception as e:
        logger.warning("job_runs persist failed for %s: %s", job_id, e)


def _make_runner(job_id: str, trigger_type: str = "scheduled"):
    """包一層執行器：跑 job、記結果（記憶體＋持久化）、視 announce 發一次 TG 通知。"""
    def runner(db_path: str | None) -> None:
        started_dt = datetime.now()
        started = started_dt.strftime("%Y-%m-%d %H:%M:%S")
        spec = JOB_DEFS[job_id]

        # 解析使用者覆寫：顯示名稱與通知開關。只有每日 job 查 DB（避免盤中刷新
        # 每 12 秒讀一次）；基礎設施 job 直接用 announce 預設、不查。
        display_name = spec["name"]
        announce = spec.get("announce", True)
        if spec.get("schedulable"):
            ov = _job_override(job_id, db_path)
            if ov.get("display_name"):
                display_name = ov["display_name"]
            if ov.get("notify_enabled") is not None:
                announce = bool(ov["notify_enabled"])

        result = None
        error = None
        try:
            result = spec["func"](db_path)
            status = result.get("status", "unknown") if isinstance(result, dict) else "done"
        except Exception as e:
            logger.error("job %s crashed: %s", job_id, e)
            status = f"error: {str(e)[:80]}"
            error = str(e)
        finished_dt = datetime.now()

        # 記憶體即時結果（排程頁顯示）＋持久化紀錄（排程紀錄頁查詢，記有效顯示名）。
        # skip_logging 的高頻 job（盤中刷新）只在「真的有做事」（result 非 None）時才更新
        # 上次執行——否則畫面會每 12 秒顯示一次「完成」，與假日/非盤中其實在略過的實況不符。
        if not (spec.get("skip_logging") and result is None):
            _last_runs[job_id] = {"time": started, "status": status}
        _record_run(job_id, display_name, trigger_type, started_dt, finished_dt,
                    status, result, error, db_path)

        # 發 TG。通知關閉（announce=False 或使用者關閉）則不發。notify 本身不 raise，
        # 但 _format_job_notify 可能因 result 結構異常拋錯——整段包起來，確保排程
        # 執行緒不會死在通知格式化上（job 已執行完、紀錄已寫入）。
        if announce:
            try:
                if result is not None:
                    msg = _format_job_notify(job_id, result)
                else:
                    msg = f"執行發生例外：{status}"
                notify(f"📡 {display_name}", msg)
            except Exception as e:
                logger.warning("notify failed for %s: %s", job_id, e)
    return runner


def get_schedule_times(db_path: str | None = None) -> dict[str, str]:
    """回傳各「每日 job」的排程時間（HH:MM）：DB 覆寫值優先，否則用 settings 預設。"""
    times = {
        jid: d["trigger"]["default"]
        for jid, d in JOB_DEFS.items()
        if d["trigger"]["kind"] == "daily"
    }
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
    """更新每日 job 的排程時間：寫入 DB，scheduler 運行中時立即 reschedule。

    僅每日 job 可調整時間；基礎設施 job 或未知 id、格式錯誤一律回 False。
    """
    spec = JOB_DEFS.get(job_id)
    if (spec is None or spec["trigger"]["kind"] != "daily"
            or not _HHMM_RE.match(hhmm or "")):
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
            trigger=CronTrigger(day_of_week=spec["trigger"]["days"], hour=h, minute=m),
        )
    logger.info("schedule time updated: %s -> %s", job_id, hhmm)
    return True


def run_job_now(scheduler: BackgroundScheduler | None, job_id: str,
                db_path: str | None = None) -> bool:
    """立即在背景觸發一次 job（不影響原排程）。scheduler 未啟用或 job 不存在回 False。"""
    if scheduler is None or job_id not in JOB_DEFS:
        return False
    scheduler.add_job(
        _make_runner(job_id, trigger_type="manual"), args=[db_path],
        id=f"{job_id}__manual", name=f"{JOB_DEFS[job_id]['name']}（手動）",
        replace_existing=True,
    )
    logger.info("manual run triggered: %s", job_id)
    return True


def create_scheduler(db_path: str | None = None) -> BackgroundScheduler:
    """建立並設定所有 job 的 scheduler（未啟動）。每日 job 時間取 DB 覆寫值或預設。"""
    scheduler = BackgroundScheduler()
    times = get_schedule_times(db_path)

    for job_id, d in JOB_DEFS.items():
        trigger, kwargs = _build_trigger(d["trigger"], times.get(job_id))
        scheduler.add_job(
            _make_runner(job_id), trigger,
            args=[db_path], id=job_id, name=d["name"], **kwargs,
        )

    logger.info("Scheduler created with %d jobs", len(scheduler.get_jobs()))
    return scheduler


def _schedule_label(trigger_spec: dict, hhmm: str | None = None) -> str:
    """人類可讀的排程描述（畫面顯示用，尤其是無法調整的基礎設施 job）。"""
    kind = trigger_spec["kind"]
    if kind == "daily":
        return f"每日 {hhmm or trigger_spec['default']}"
    if kind == "interval":
        return f"每 {trigger_spec['seconds']} 秒"
    if kind == "monthly":
        return (f"每月 {trigger_spec['day']} 日 "
                f"{trigger_spec['hour']:02d}:{trigger_spec['minute']:02d}")
    return "—"


def get_jobs_info(scheduler: BackgroundScheduler | None,
                  db_path: str | None = None) -> list[dict]:
    """回傳所有 job 的排程狀態供頁面顯示：時間、下次執行、上次結果。

    每日（schedulable）job 帶 editable=True、可調名稱/說明/時間/通知；基礎設施 job
    （盤中刷新、休市日曆刷新）editable=False、唯讀顯示。scheduler 未啟用時仍列設定。
    name/notify_enabled 取使用者覆寫（無則回 JOB_DEFS 預設），並附 default_* 供頁面標示。
    """
    times = get_schedule_times(db_path)
    overrides = get_job_overrides(db_path)
    jobs = []
    for job_id, d in JOB_DEFS.items():
        editable = bool(d.get("schedulable"))
        ov = overrides.get(job_id, {})
        eff_name = ov.get("display_name") or d["name"]
        ov_desc = ov.get("display_desc")
        eff_desc = ov_desc if ov_desc else d.get("desc", "")
        notify_en = ov.get("notify_enabled")
        eff_notify = bool(notify_en) if notify_en is not None else d.get("announce", True)

        time_hhmm = times.get(job_id)  # 基礎設施 job 不在 times → None
        next_run = None
        if scheduler is not None:
            job = scheduler.get_job(job_id)
            nrt = getattr(job, "next_run_time", None) if job else None
            next_run = nrt.strftime("%Y-%m-%d %H:%M:%S") if nrt else None
        jobs.append({
            "id": job_id,
            "editable": editable,
            "name": eff_name,
            "default_name": d["name"],
            "desc": eff_desc,
            "default_desc": d.get("desc", ""),
            "desc_overridden": bool(ov_desc),
            "time_hhmm": time_hhmm,
            "default_time": d["trigger"].get("default"),
            "schedule_label": _schedule_label(d["trigger"], time_hhmm),
            "notify_enabled": eff_notify,
            "notify_default": d.get("announce", True),
            "next_run": next_run,
            "last_run": _last_runs.get(job_id),
        })
    return jobs
