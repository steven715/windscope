"""午盤匯率 job：午後再抓一次匯率（quote_pm 槽），供盤中觀察。

休市無關維度：平日（含國定假日）照收國際匯率統計；週末整步略過。
不產生訊號、不影響早盤升貶基準——compute_fx_metrics 仍錨定 quote_0845 vs 前日 close_16，
quote_pm 為附加觀察欄，現有下游查詢一律不受影響。
"""

import logging
from datetime import datetime

from jobs.helpers import determine_status, run_step

logger = logging.getLogger(__name__)

# 午盤收集的幣別；USD/TWD 走台銀、其餘走 Yahoo（由 FXCollector.collect_pair 路由）
AFTERNOON_FX_PAIRS = ["USD/TWD", "USD/CNY", "USD/KRW", "USD/JPY"]


def run_afternoon_fx(date: str, db_path: str | None = None) -> dict:
    """午盤匯率 job：收 quote_pm 槽。週末整步略過，平日（含休市）照收。"""
    logger.info("run_afternoon_fx: starting for %s", date)

    if datetime.strptime(date, "%Y-%m-%d").weekday() >= 5:
        logger.info("run_afternoon_fx: %s 為週末，skipping", date)
        return {"date": date, "status": "skipped", "results": {}, "errors": []}

    from collectors.fx import FXCollector

    c = FXCollector(db_path=db_path)
    results = {}
    errors = []
    for pair in AFTERNOON_FX_PAIRS:
        step = "fx_pm_" + pair.split("/")[1].lower()
        ok, err = run_step(step,
                           lambda p=pair: c.collect_and_save_pair(date, p, "quote_pm"))
        results[step] = ok
        if err:
            errors.append(err)

    status = determine_status(results)
    logger.info("run_afternoon_fx: %s status=%s", date, status)
    return {"date": date, "status": status, "results": results, "errors": errors}
