"""匯率收盤收集 job：16:00 收 FX 收盤（close_16 槽）。

收 USD/TWD（台銀）＋ CNY/KRW/JPY（Yahoo）的 16:00 收盤，寫入 close_16——這是隔日
compute_fx_metrics 升貶基準用的「前一日收盤」（今日 quote_0845 vs 前日 close_16）。
僅交易日收（close_16 是交易日收盤）；非交易日整步略過。

（原為午盤 quote_pm 觀察 job，盤中匯率無意義已移除；job_id 沿用 afternoon_fx。）
"""

import logging

from jobs.helpers import determine_status, run_step
from utils.trading_calendar import is_trading_day

logger = logging.getLogger(__name__)

# 收盤匯率幣別；USD/TWD 走台銀、其餘走 Yahoo（由 FXCollector.collect_pair 路由）
CLOSE_FX_PAIRS = ["USD/TWD", "USD/CNY", "USD/KRW", "USD/JPY"]


def run_afternoon_fx(date: str, db_path: str | None = None) -> dict:
    """匯率收盤收集 job：收 close_16 槽。非交易日整步略過。"""
    logger.info("run_afternoon_fx: starting for %s", date)

    if not is_trading_day(date):
        logger.info("run_afternoon_fx: %s 非交易日，skipping", date)
        return {"date": date, "status": "skipped", "results": {}, "errors": []}

    from collectors.fx import FXCollector

    c = FXCollector(db_path=db_path)
    results = {}
    errors = []
    for pair in CLOSE_FX_PAIRS:
        step = "fx_close_" + pair.split("/")[1].lower()
        ok, err = run_step(step,
                           lambda p=pair: c.collect_and_save_pair(date, p, "close_16"))
        results[step] = ok
        if err:
            errors.append(err)

    status = determine_status(results)
    logger.info("run_afternoon_fx: %s status=%s", date, status)
    return {"date": date, "status": status, "results": results, "errors": errors}
