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
    """回歸：模板裡不能再有連到下線頁的連結或表單。"""
    for t in (Path(__file__).resolve().parents[2] / "server" / "templates").glob("*.html"):
        text = t.read_text(encoding="utf-8")
        assert not re.search(r'(href|action)="/(signals|stats)\b', text), t.name


@pytest.mark.parametrize("raw, want", [
    (None, None), ("", None), ("  ", None), ("watch.example", None), ("javascript:alert(1)", None), ("//evil", None),
    ("https://", None), ("https://watch.example/", "https://watch.example"), (" http://127.0.0.1:5173 ", "http://127.0.0.1:5173"),
])
def test_frontend_url_only_accepts_http_urls(raw, want):
    assert settings.frontend_url_from(raw) == want
