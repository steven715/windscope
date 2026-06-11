"""通知推送：log（預設）與 Telegram 兩種 provider。

設定走環境變數（見 config/settings.py）：
    PREMARKET_NOTIFY=telegram
    TELEGRAM_BOT_TOKEN=<bot token>
    TELEGRAM_CHAT_ID=<chat id>

notify() 永不 raise——通知失敗不能讓 job 掛掉。
"""

import logging

from config import settings

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def _send_log(title: str, message: str) -> bool:
    """預設 provider：把通知內容寫進 log。"""
    logger.info("[NOTIFY] %s\n%s", title, message)
    return True


def _send_telegram(title: str, message: str) -> bool:
    """Telegram provider。缺 token/chat_id 時 fallback 到 log 並回傳 False。"""
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        logger.warning("Telegram notify configured but token/chat_id missing, "
                       "falling back to log")
        _send_log(title, message)
        return False

    from utils.http_client import http_get

    url = TELEGRAM_API_URL.format(token=settings.TELEGRAM_BOT_TOKEN)
    resp = http_get(url, params={
        "chat_id": settings.TELEGRAM_CHAT_ID,
        "text": f"{title}\n\n{message}",
    })
    ok = resp.status_code == 200
    if not ok:
        logger.error("Telegram notify failed: status=%d body=%s",
                     resp.status_code, resp.text[:200])
    return ok


_PROVIDERS = {
    "log": _send_log,
    "telegram": _send_telegram,
}


def notify(title: str, message: str) -> bool:
    """依 settings.NOTIFY_PROVIDER 發送通知。任何失敗記 log 回傳 False，不 raise。"""
    provider_name = settings.NOTIFY_PROVIDER
    provider = _PROVIDERS.get(provider_name)
    if provider is None:
        logger.error("Unknown notify provider: %s, falling back to log",
                     provider_name)
        return _send_log(title, message)

    try:
        return provider(title, message)
    except Exception as e:
        logger.error("notify via %s failed: %s", provider_name, e)
        return False
