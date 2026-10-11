"""Job 共用工具：步驟紀錄器 JobRun、單步執行、狀態判定。"""

import logging
import time
from collections.abc import Callable
from typing import TypeVar

from utils.logger import log_event

logger = logging.getLogger(__name__)

T = TypeVar("T")


def run_step_value(step_name: str, fn: Callable[[], T]) -> tuple[T | None, str | None]:
    """執行單一步驟並帶回結果，回傳 (結果, 錯誤訊息)；fn 回 None/False 或拋例外時結果為 None。"""
    t0 = time.perf_counter()
    try:
        result = fn()
        duration_ms = int((time.perf_counter() - t0) * 1000)
        if result is None or result is False:
            log_event("step_run", level=logging.WARNING, step=step_name,
                      outcome="failed", duration_ms=duration_ms,
                      error=f"{step_name}: no data")
            return None, f"{step_name}: no data"
        log_event("step_run", step=step_name, outcome="ok",
                  duration_ms=duration_ms)
        return result, None
    except Exception as e:
        duration_ms = int((time.perf_counter() - t0) * 1000)
        logger.error("%s failed: %s", step_name, e)
        # 例外＝真正的錯誤 → ERROR（與 logger.error 一致）；
        # no-data 降級維持 WARNING。
        log_event("step_run", level=logging.ERROR, step=step_name,
                  outcome="failed", duration_ms=duration_ms,
                  error=f"{step_name}: {str(e)}")
        return None, f"{step_name}: {str(e)}"


def determine_status(results: dict[str, bool]) -> str:
    """根據各步驟結果判定 job 整體狀態。"""
    values = list(results.values())
    if all(values):
        return "completed"
    elif any(values):
        return "partial"
    else:
        return "failed"


class JobRun:
    """一次 job 執行的步驟紀錄器：step／collect 記結果與錯誤，finish 判定狀態、發 job_completed。

    每一步都經 run_step_value 執行（例外與 no-data 不中斷 job），這也是 job 測試的唯一 seam。
    """

    def __init__(self, job: str, date: str) -> None:
        self.job = job
        self.date = date
        self.results: dict[str, bool] = {}
        self.errors: list[str] = []
        self._t0 = time.perf_counter()
        logger.info("%s: starting for %s", job, date)

    def step(self, name: str, fn: Callable[[], T]) -> T | None:
        """執行一步並記錄；回傳 fn 的結果，失敗（例外／None／False）為 None。"""
        value, err = run_step_value(name, fn)
        ok = value is not None and value is not False
        self.results[name] = ok
        if err:
            self.errors.append(err)
        return value if ok else None

    def collect(self, name: str, fetch: Callable[[], object],
                save: Callable[[object], None]) -> bool:
        """collect＋save 一步：fetch 回 None 視為無資料（失敗），否則交給 save。"""
        def _do() -> bool | None:
            data = fetch()
            if data is None:
                return None
            save(data)
            return True
        return self.step(name, _do) is not None

    def finish(self, level: int = logging.INFO, **extra: object) -> dict:
        """判定狀態、發 job_completed 事件（extra 併入事件），回傳 {date, status, results, errors}。"""
        status = determine_status(self.results)
        logger.info("%s: %s status=%s", self.job, self.date, status)
        log_event(
            "job_completed", level=level, job=self.job, date=self.date, status=status,
            steps_ok=sum(1 for v in self.results.values() if v),
            steps_failed=sum(1 for v in self.results.values() if not v),
            duration_ms=int((time.perf_counter() - self._t0) * 1000), **extra,
        )
        return {"date": self.date, "status": status,
                "results": self.results, "errors": self.errors}

    @staticmethod
    def skipped(job: str, date: str, reason: str, **extra: object) -> dict:
        """非交易日等整個 job 略過：記 log（不發事件，沿用既有行為），回傳標準略過結果。"""
        logger.info("%s: %s skipped（%s）", job, date, reason)
        return {"date": date, "status": "skipped", "results": {}, "errors": [], **extra}
