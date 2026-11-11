"""下線的 Jinja 頁（ADR-0006）：/signals、/stats 搬到前端 windwatch。有設前端網址 → 302 導過去；沒設 → 410 說明；
其他頁不再連到它們（有設前端網址時改連前端）。"""

import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config import settings
from db.schema import create_all_tables
from server.app import create_app

FE = "https://watch.example.test"


@pytest.fixture
def client(tmp_path) -> TestClient:
    db_path = str(tmp_path / "t.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    conn.execute("INSERT INTO verifications (date, predicted_direction, confidence, day_change_pct, day_change_class, hit_day, "
                 "hit_open, rule_version) VALUES ('2026-06-11', 'bullish', 3, 0.5, 'up', 1, 1, ?)", (settings.SIGNAL_RULE_VERSION,))
    conn.execute("INSERT INTO signals (date, direction, confidence, reasons, rule_version) VALUES ('2026-06-12', 'bullish', 3, '[]', ?)",
                 (settings.SIGNAL_RULE_VERSION,))
    conn.commit()
    conn.close()
    return TestClient(create_app(db_path=db_path, enable_scheduler=False))


@pytest.mark.parametrize("path, words", [("/signals", "情報"), ("/stats", "交易")])
def test_without_frontend_url_old_pages_say_where_they_went(client, monkeypatch, path, words):
    monkeypatch.setattr(settings, "FRONTEND_URL", None)
    r = client.get(path + "?date_from=2026-06-01", follow_redirects=False)
    assert r.status_code == 410
    assert "windwatch" in r.text and words in r.text and "WINDSCOPE_FRONTEND_URL" in r.text


@pytest.mark.parametrize("path", ["/signals", "/stats"])
def test_with_frontend_url_old_pages_redirect_to_the_intel_area(client, monkeypatch, path):
    monkeypatch.setattr(settings, "FRONTEND_URL", FE)
    r = client.get(path + "?rule_version=v3", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"{FE}/#intel"


def test_pages_link_to_the_frontend_only_when_it_is_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_URL", None)
    for page in ("/", "/more"):
        html = client.get(page).text
        assert not re.search(r'href="/(signals|stats)\b', html), page
        assert "/#intel" not in html, page                                  # 沒設：連 None/#intel 都不能出現
    monkeypatch.setattr(settings, "FRONTEND_URL", FE)
    for page in ("/", "/more"):
        html = client.get(page).text
        assert not re.search(r'href="/(signals|stats)\b', html), page
        assert f'href="{FE}/#intel"' in html, page


def test_no_template_links_to_retired_pages():
    """回歸：模板（含裡面的 JS）不能再連到下線頁：href／action、單雙引號、location／fetch 字串都算。"""
    pat = re.compile(r"""["'`]/(signals|stats)(?=["'`?#/])""")
    for t in (Path(__file__).resolve().parents[2] / "server" / "templates").glob("*.html"):
        assert not pat.search(t.read_text(encoding="utf-8")), t.name


def test_trust_strip_is_not_a_dead_link_without_frontend(client, monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_URL", None)
    html = client.get("/").text
    assert '<div class="trust-strip">' in html and 'class="trust-strip" href' not in html


def test_retired_pages_sit_behind_auth_and_do_not_leak_the_frontend_url(tmp_path, monkeypatch):
    """ADR-0008：沒帶認證 → 401（不導向、不洩漏前端網址）；Basic 認證後才 302。"""
    db_path = str(tmp_path / "a.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    conn.close()
    monkeypatch.setattr(settings, "FRONTEND_URL", FE)
    c = TestClient(create_app(db_path=db_path, enable_scheduler=False, api_token="tok-123"))
    r = c.get("/signals", follow_redirects=False)
    assert r.status_code == 401 and "location" not in r.headers and FE not in r.text
    r = c.get("/signals", auth=("u", "tok-123"), follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"{FE}/#intel"


@pytest.mark.parametrize("raw, want", [
    (None, None), ("", None), ("  ", None), ("watch.example", None), ("javascript:alert(1)", None), ("//evil", None),
    ("https://", None), ("http://", None), ("https://a b", None), ("https://a@evil.example", None), ("https:///evil.example", None),
    ("https://x/#", None), ("https://x/?a=1", None), ("https://x\r\nSet-Cookie: a=b", None),
    ("https://watch.example/", "https://watch.example"), (" http://127.0.0.1:5173 ", "http://127.0.0.1:5173"),
    ("HTTPS://Watch.example/app", "HTTPS://Watch.example/app"),
])
def test_frontend_url_only_accepts_http_urls(raw, want):
    assert settings.frontend_url_from(raw) == want
