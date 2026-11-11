"""對外認證（ADR-0008，server/auth.py）：設了 token 後，API／頁面／表單 POST／WebSocket 沒帶都擋；Bearer 與 Basic（密碼＝token）都過；
Basic 的會改資料請求與 WS 握手要同源；公開路徑不擋；沒設 token 照舊開放；錯的 token、壞掉的 header、log 不洩漏 token。"""

import base64
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from config import settings
from db.schema import create_all_tables
from server.app import create_app

TOKEN = "tok-Abc123-very-secret"
HOST = "testserver"


def _basic(pw: str, user: str = "me") -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


BEARER = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def db(tmp_path: Path) -> str:
    p = str(tmp_path / "t.db")
    conn = sqlite3.connect(p)
    create_all_tables(conn)
    conn.close()
    return p


@pytest.fixture
def client(db) -> TestClient:
    return TestClient(create_app(db_path=db, enable_scheduler=False, api_token=TOKEN))


def test_api_requires_the_token(client):
    r = client.get("/api/instruments")
    assert r.status_code == 401 and r.json() == {"detail": "unauthorized"} and "Basic" in r.headers["www-authenticate"]
    assert client.get("/api/instruments", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/instruments", headers={"Authorization": "Basic !!notb64"}).status_code == 401
    assert client.get("/api/instruments", headers=_basic("wrong")).status_code == 401
    assert client.get("/api/instruments", headers=BEARER).status_code == 200
    assert client.get("/api/instruments", headers={"Authorization": f"bearer   {TOKEN}  "}).status_code == 200


def test_pages_accept_browser_basic_auth(client):
    assert client.get("/").status_code == 401
    assert client.get("/", headers=_basic(TOKEN)).status_code == 200
    assert client.get("/watchlist", headers=_basic(TOKEN, user="")).status_code == 200    # 帳號隨意（可空）


def test_mutating_posts_are_blocked_without_the_token(client, db):
    """盤點出來會改資料的 POST：沒帶 token 一律 401，handler 根本不會跑。"""
    for path in ("/api/targets/x/lock", "/api/scheduler/before_open/run", "/api/events", "/watchlist/add", "/watchlist/remove", "/scheduler/run",
                 "/scheduler/save", "/scheduler/runs/delete", "/backtest/run", "/stock-import/run", "/chip-import",
                 "/chip-import/ocr"):
        assert client.post(path, data={"stock_id": "2330"}).status_code == 401, path


def test_browser_posts_must_be_same_origin(client):
    """Basic 帳密瀏覽器會自動帶：會改資料的請求要 Origin（或 Referer）＝Host，擋跨站表單。Bearer 不受限。"""
    ok = {**_basic(TOKEN), "Origin": f"http://{HOST}"}
    assert client.post("/scheduler/runs/delete", data={"mode": "one", "run_id": "0"}, headers=ok,
                       follow_redirects=False).status_code in (200, 303)
    evil = {**_basic(TOKEN), "Origin": "https://evil.example"}
    r = client.post("/scheduler/runs/delete", data={"mode": "all"}, headers=evil)
    assert r.status_code == 403 and r.json()["detail"] == "cross-origin request refused"
    assert client.post("/scheduler/runs/delete", data={"mode": "all"}, headers=_basic(TOKEN)).status_code == 403   # 沒 Origin／Referer
    ref = {**_basic(TOKEN), "Referer": f"http://{HOST}/scheduler/runs"}
    assert client.post("/scheduler/runs/delete", data={"mode": "one", "run_id": "0"}, headers=ref,
                       follow_redirects=False).status_code in (200, 303)
    assert client.post("/api/targets/nope/lock", headers=BEARER, json={}).status_code == 404          # Bearer：不看 Origin
    assert client.get("/api/instruments", headers={**_basic(TOKEN), "Origin": "https://evil.example"}).status_code == 200  # 讀取不限


def test_public_paths_stay_open(client):
    assert client.get("/healthz").json() == {"ok": True, "auth": True}
    assert client.get("/manifest.webmanifest").status_code == 200 and client.get("/sw.js").status_code == 200
    static = next(p for p in (Path(__file__).resolve().parents[2] / "server" / "static").rglob("*") if p.is_file())
    rel = static.relative_to(Path(__file__).resolve().parents[2] / "server" / "static").as_posix()
    assert client.get(f"/static/{rel}").status_code == 200


def test_websocket_requires_the_token_and_same_origin_for_browsers(db):
    app = create_app(db_path=db, api_token=TOKEN)
    with TestClient(app) as c:
        with pytest.raises(WebSocketDenialResponse) as denied:                             # 握手直接回 401（tradewind 看得到狀態碼）
            with c.websocket_connect("/ws/bars"):
                pass
        assert denied.value.status_code == 401
        with c.websocket_connect("/ws/bars", headers=BEARER) as ws:                        # tradewind 的 Bearer
            assert "heartbeat" in ws.receive_json()
        with pytest.raises(WebSocketDenialResponse) as cross:                              # 跨站 WebSocket 劫持
            with c.websocket_connect("/ws/bars", headers={**_basic(TOKEN), "Origin": "https://evil.example"}):
                pass
        assert cross.value.status_code == 403
        with c.websocket_connect("/ws/bars", headers={**_basic(TOKEN), "Origin": f"http://{HOST}"}) as ws:
            assert "heartbeat" in ws.receive_json()


def test_without_a_token_everything_stays_open(db, monkeypatch):
    monkeypatch.setattr(settings, "WINDSCOPE_API_TOKEN", "")
    c = TestClient(create_app(db_path=db))
    assert c.get("/api/instruments").status_code == 200 and c.get("/healthz").json() == {"ok": True, "auth": False}


def test_token_comes_from_settings_and_denials_never_log_it(db, monkeypatch, caplog):
    monkeypatch.setattr(settings, "WINDSCOPE_API_TOKEN", TOKEN)
    c = TestClient(create_app(db_path=db))
    assert c.get("/api/instruments").status_code == 401 and c.get("/api/instruments", headers=BEARER).status_code == 200
    c.get("/api/instruments", headers={"Authorization": f"Bearer {TOKEN}x"})
    assert "auth denied" in caplog.text and TOKEN not in caplog.text
    assert caplog.text.count("auth denied") == 1                                         # 同一 IP 60 秒內只記一次


# ── 審查加固 ─────────────────────────────────────────────────────
def test_every_response_refuses_to_be_framed_even_without_auth(client, db):
    for c, headers in ((client, BEARER), (TestClient(create_app(db_path=db, api_token="")), {})):
        for path in ("/healthz", "/api/instruments"):
            r = c.get(path, headers=headers)
            assert r.headers["x-frame-options"] == "DENY" and r.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert client.get("/api/instruments").headers["x-frame-options"] == "DENY"              # 401 也有


def test_denial_log_cannot_be_forged_and_reports_suppressed_counts(client, caplog):
    client.get("/api/x%0aWARNING%20server.auth%20auth%20OK%20for%20admin")             # 伺服器解碼後是換行
    for _ in range(3):
        client.get("/api/instruments")
    assert "\nWARNING server.auth auth OK" not in caplog.text and "\\n" in caplog.text     # 路徑以 repr 記
    assert caplog.text.count("auth denied") == 1
    import server.auth as mod
    mw = client.app.middleware_stack
    while not isinstance(mw, mod.TokenAuth):
        mw = mw.app
    mw._last_log.clear()                                                                   # 下一分鐘
    client.get("/api/instruments", headers={"X-Forwarded-For": "203.0.113.9"})
    assert "forwarded '203.0.113.9'" in caplog.text


def test_whitespace_token_means_auth_off_and_healthz_says_so(db):
    c = TestClient(create_app(db_path=db, api_token="   "))
    assert c.get("/api/instruments").status_code == 200 and c.get("/healthz").json()["auth"] is False


def test_validate_token_rules():
    from server.auth import validate_token
    good = "A" * 32
    assert validate_token(good) is None and validate_token("", disabled=True) is None
    assert "not set" in validate_token("") and "not set" in validate_token("   ")
    assert "too short" in validate_token("short-token")
    assert "printable ASCII" in validate_token("A" * 31 + "é") and "printable ASCII" in validate_token("A" * 16 + " " + "A" * 16)


def test_serve_refuses_to_start_without_a_token_or_with_a_weak_one(monkeypatch, capsys):
    import argparse

    import uvicorn

    import main as cli
    started = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: started.append(kw))
    args = argparse.Namespace(no_scheduler=True, port=None)
    monkeypatch.delenv("WINDSCOPE_AUTH_DISABLED", raising=False)
    for tok in ("", "weak"):
        monkeypatch.setattr(settings, "WINDSCOPE_API_TOKEN", tok)
        with pytest.raises(SystemExit) as e:
            cli.cmd_serve(args)
        assert e.value.code == 2 and "refusing to start" in capsys.readouterr().err
    monkeypatch.setenv("WINDSCOPE_AUTH_DISABLED", "1")                                       # 本機開發：明講才准
    monkeypatch.setattr(settings, "WINDSCOPE_API_TOKEN", "")
    cli.cmd_serve(args)
    monkeypatch.delenv("WINDSCOPE_AUTH_DISABLED")
    monkeypatch.setattr(settings, "WINDSCOPE_API_TOKEN", TOKEN + "x" * 20)
    cli.cmd_serve(args)
    assert len(started) == 2


def test_service_worker_caches_nothing_and_clears_old_caches(client):
    sw = client.get("/sw.js").text
    assert "caches.delete" in sw and "addAll" not in sw and ".put(" not in sw and "respondWith" not in sw
