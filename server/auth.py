"""單一 token 認證（ADR-0008）：除了公開路徑，所有 HTTP 與 WebSocket 請求都要帶 `WINDSCOPE_API_TOKEN`。

- **API／程式**（tradewind、之後的前端）：`Authorization: Bearer <token>`。
- **瀏覽器**（過渡期的 Jinja 頁）：HTTP Basic，帳號隨意、密碼＝token。瀏覽器第一次彈出對話框，之後同源的頁面、表單、fetch 都自動帶；
  不新增登入頁（CLAUDE.md 規則 1：不再新增 Jinja 頁面）、不發 cookie。
- **跨站偽造**：Basic 的帳密瀏覽器會自動帶，所以用 Basic 認證的「會改資料」請求（POST／PUT／PATCH／DELETE）與 WebSocket 握手，
  `Origin`（沒有就看 `Referer`）的 host 必須等於 `Host`，否則 403。Bearer 要程式自己加 header，不受這條限制。
- **公開路徑**：`/healthz`、`/static/*`、`/manifest.webmanifest`、`/sw.js`（PWA 殼，不含資料）。
- **沒設 token**：認證關閉（本機開發與測試照舊），開機記 ERROR；正式部署由 docker-compose 的 `${WINDSCOPE_API_TOKEN:?…}` 擋：沒設起不來。
- 比對用 `hmac.compare_digest`；失敗記 WARNING（來源 IP＋X-Forwarded-For、路徑以 repr 記——不能用換行偽造 log；同一來源每 60 秒
  一筆、期間被壓下的筆數附在下一筆），token 不進 log。ngrok／docker 後面所有人的來源 IP 都一樣，所以壓制是「每個來源」不是每個人。
- 每個 HTTP 回應都加 `X-Frame-Options: DENY` 與 `Content-Security-Policy: frame-ancestors 'none'`（不准被別的網站框起來：
  同源檢查擋不住「把頁面框起來騙你按」）。
- `validate_token`：`main.py serve` 開機時檢查（至少 32 字、只准可見 ASCII）；沒設要明講 `WINDSCOPE_AUTH_DISABLED=1` 才准起
  （本機開發）——docker-compose 另外用 `:?` 擋。
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
_LOG_SOURCES_MAX = 1000
MIN_TOKEN_LEN = 32
SECURITY_HEADERS = [(b"x-frame-options", b"DENY"), (b"content-security-policy", b"frame-ancestors 'none'")]


def validate_token(token: str | None, disabled: bool = False) -> str | None:
    """開機檢查：回錯誤訊息（不准起）或 None。沒設 token 只有 disabled（WINDSCOPE_AUTH_DISABLED=1）才准。"""
    t = (token or "").strip()
    if not t:
        return None if disabled else ("WINDSCOPE_API_TOKEN is not set; set it in .env, or WINDSCOPE_AUTH_DISABLED=1 for a "
                                      "local-only server (never expose it)")
    if len(t) < MIN_TOKEN_LEN:
        return f"WINDSCOPE_API_TOKEN is too short ({len(t)} chars, need >= {MIN_TOKEN_LEN}); use secrets.token_urlsafe(32)"
    if not all(33 <= ord(c) <= 126 for c in t):
        return "WINDSCOPE_API_TOKEN must be printable ASCII without spaces"
    return None


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


def _with_security_headers(send):
    async def wrapped(message):
        if message["type"] == "http.response.start":
            names = {k.lower() for k, _ in message.get("headers") or []}
            message = {**message, "headers": list(message.get("headers") or [])
                       + [(k, v) for k, v in SECURITY_HEADERS if k not in names]}
        await send(message)
    return wrapped


class TokenAuth:
    """ASGI middleware（HTTP＋WebSocket）。token 為空＝不檢查。"""

    def __init__(self, app, token: str | None) -> None:
        self.app = app
        self._token = (token or "").strip().encode("utf-8")
        self._last_log: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}

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
        fwd = _header(scope, b"x-forwarded-for")
        src = f"{ip} (forwarded {fwd!r})" if fwd else str(ip)
        now = time.monotonic()
        if now - self._last_log.get(src, -_LOG_EVERY_SEC) < _LOG_EVERY_SEC:
            self._suppressed[src] = self._suppressed.get(src, 0) + 1
            return
        if len(self._last_log) >= _LOG_SOURCES_MAX:                      # 來源太多：重來（只影響壓制，不影響擋）
            self._last_log.clear()
            self._suppressed.clear()
        self._last_log[src] = now
        more = self._suppressed.pop(src, 0)
        logger.warning("auth denied (%s): %s %r from %s%s", why, scope.get("method", "WS"), scope.get("path"), src,
                       f" (+{more} more in the last minute)" if more else "")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            send = _with_security_headers(send)
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
