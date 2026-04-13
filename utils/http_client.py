import logging
import random
import time

import requests

from config import settings

logger = logging.getLogger(__name__)


def http_get(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int | None = None,
    encoding: str | None = None,
) -> requests.Response:
    """統一 GET 請求，內建 User-Agent、timeout、指數退避 retry、random delay。"""
    timeout = timeout or settings.HTTP_TIMEOUT
    request_headers = {"User-Agent": settings.USER_AGENT}
    if headers:
        request_headers.update(headers)

    last_exception: Exception | None = None

    for attempt in range(1, settings.HTTP_RETRIES + 1):
        # 禮貌延遲
        delay = random.uniform(settings.HTTP_DELAY_MIN, settings.HTTP_DELAY_MAX)
        time.sleep(delay)

        try:
            resp = requests.get(
                url, params=params, headers=request_headers, timeout=timeout
            )
            if encoding:
                resp.encoding = encoding
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_exception = e
            body_preview = ""
            if hasattr(e, "response") and e.response is not None:
                body_preview = e.response.text[:200]
            logger.warning(
                "HTTP GET attempt %d/%d failed: %s | URL: %s | body: %s",
                attempt,
                settings.HTTP_RETRIES,
                e,
                url,
                body_preview,
            )
            if attempt < settings.HTTP_RETRIES:
                backoff = delay * (2 ** (attempt - 1))
                time.sleep(backoff)

    logger.error("HTTP GET failed after %d retries: %s", settings.HTTP_RETRIES, url)
    raise last_exception


def http_post(
    url: str,
    data: dict | None = None,
    headers: dict | None = None,
    timeout: int | None = None,
    encoding: str | None = None,
) -> requests.Response:
    """統一 POST 請求，內建 User-Agent、timeout、指數退避 retry、random delay。"""
    timeout = timeout or settings.HTTP_TIMEOUT
    request_headers = {"User-Agent": settings.USER_AGENT}
    if headers:
        request_headers.update(headers)

    last_exception: Exception | None = None

    for attempt in range(1, settings.HTTP_RETRIES + 1):
        delay = random.uniform(settings.HTTP_DELAY_MIN, settings.HTTP_DELAY_MAX)
        time.sleep(delay)

        try:
            resp = requests.post(
                url, data=data, headers=request_headers, timeout=timeout
            )
            if encoding:
                resp.encoding = encoding
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_exception = e
            body_preview = ""
            if hasattr(e, "response") and e.response is not None:
                body_preview = e.response.text[:200]
            logger.warning(
                "HTTP POST attempt %d/%d failed: %s | URL: %s | body: %s",
                attempt,
                settings.HTTP_RETRIES,
                e,
                url,
                body_preview,
            )
            if attempt < settings.HTTP_RETRIES:
                backoff = delay * (2 ** (attempt - 1))
                time.sleep(backoff)

    logger.error("HTTP POST failed after %d retries: %s", settings.HTTP_RETRIES, url)
    raise last_exception
