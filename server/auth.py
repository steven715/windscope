"""單一 token 認證（ADR-0008）：除了公開路徑，所有 HTTP 與 WebSocket 請求都要帶 `WINDSCOPE_API_TOKEN`。

- **API／程式**（tradewind、之後的前端）：`Authorization: Bearer <token>`。
- **瀏覽器**（過渡期的 Jinja 頁）：HTTP Basic，帳號隨意、密碼＝token。瀏覽器第一次彈出對話框，之後同源的頁面、表單、fetch 都自動帶；
  不新增登入頁（CLAUDE.md 規則 1：不再新增 Jinja 頁面）、不發 cookie。
- **跨站偽造**：Basic 的帳密瀏覽器會自動帶，所以用 Basic 認證的「會改資料」請求（POST／PUT／PATCH／DELETE）與 WebSocket 握手，
  `Origin`（沒有就看 `Referer`）的 host 必須等於 `Host`，否則 403。Bearer 要程式自己加 header，不受這條限制。
- **公開路徑**：`/healthz`、`/static/*`、`/manifest.webmanifest`、`/sw.js`（PWA 殼，不含資料）。
- **沒設 token**：認證關閉（本機開發與測試照舊），開機記 ERROR；正式部署由 docker-compose 的 `${WINDSCOPE_API_TOKEN:?…}` 擋：沒設起不來。
- 比對用 `hmac.compare_digest`；失敗記 WARNING（來源 IP、路徑；同一 IP 每 60 秒最多一筆），token 不進 log。
"""

import base64
import binascii
import hmac
import json
import logging
import time
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({"/healthz", "/manifest.webmanifest", "/sw.js"})
PUBLIC_PREFIXES = ("/static/",)
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
REALM = "windscope"
_LOG_EVERY_SEC = 60.0


def _header(scope: dict, name: bytes) -> str | None:
    for k, v in scope.get("headers") or []:
        if k.lower() == name:
            return v.decode("latin-1")
    return None


def _same_origin(scope: dict) -> bool:
    """Origin（沒有就 Referer）的 host:port 等於 Host header。兩者都沒有 → 不算同源。"""
    host = (_header(scope, b"host") or "").strip().lower()
    src = _header(scope, b"origin") or _header(scope, b"referer")
    if not host or not src or src == "null":
        return False
    return urlsplit(src.strip()).netloc.lower() == host


class TokenAuth:
    """ASGI middleware（HTTP＋WebSocket）。token 為空＝不檢查。"""

    def __init__(self, app, token: str | None) -> None:
        self.app = app
        self._token = (token or "").strip().encode("utf-8")
        self._last_log: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        """有沒有開認證。"""
        return bool(self._token)

    def _check(self, scope: dict) -> str | None:
        """回傳認證方式（bearer／basic）或 None（沒過）。"""
        auth = (_header(scope, b"authorization") or "").strip()
        scheme, _, cred = auth.partition(" ")
        scheme, cred = scheme.lower(), cred.strip()
        if scheme == "bearer" and cred and hmac.compare_digest(cred.encode("utf-8"), self._token):
            return "bearer"
        if scheme == "basic" and cred:
            try:
                _, _, pw = base64.b64decode(cred, validate=True).decode("utf-8").partition(":")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                return None
            if hmac.compare_digest(pw.encode("utf-8"), self._token):
                return "basic"
        return None

    def _log_denied(self, scope: dict, why: str) -> None:
        ip = (scope.get("client") or ("?", 0))[0]
        now = time.monotonic()
        if now - self._last_log.get(ip, -_LOG_EVERY_SEC) >= _LOG_EVERY_SEC:
            self._last_log[ip] = now
            logger.warning("auth denied (%s): %s %s from %s", why, scope.get("method", "WS"), scope.get("path"), ip)

    async def __call__(self, scope, receive, send):
        if not self._token or scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path = scope.get("path") or ""
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await self.app(scope, receive, send)
        how = self._check(scope)
        if how is None:
            self._log_denied(scope, "no or wrong token")
            return await self._deny(scope, receive, send, 401)
        unsafe = scope["type"] == "websocket" or scope.get("method", "GET").upper() in UNSAFE_METHODS
        if how == "basic" and unsafe and not _same_origin(scope):
            self._log_denied(scope, "cross-origin request with browser credentials")
            return await self._deny(scope, receive, send, 403)
        return await self.app(scope, receive, send)

    async def _deny(self, scope: dict, receive, send, status: int) -> None:
        body = json.dumps({"detail": "unauthorized" if status == 401 else "cross-origin request refused"}).encode("utf-8")
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        if status == 401:
            headers.append((b"www-authenticate", f'Basic realm="{REALM}", charset="UTF-8"'.encode()))
        if scope["type"] == "websocket":
            await receive()                                          # 先收 websocket.connect，再回拒絕
            if "websocket.http.response" in (scope.get("extensions") or {}):   # 握手直接回 401／403（client 看得到狀態碼）
                await send({"type": "websocket.http.response.start", "status": status, "headers": headers})
                await send({"type": "websocket.http.response.body", "body": body})
            else:
                await send({"type": "websocket.close", "code": 1008})
            return
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
