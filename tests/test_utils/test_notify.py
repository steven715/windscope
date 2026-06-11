"""通知模組測試：dispatch、Telegram payload、缺設定 fallback、永不 raise。"""

from unittest.mock import MagicMock, patch

from config import settings
from utils import notify as notify_module
from utils.notify import notify


class TestDispatch:
    def test_log_provider_returns_true(self, monkeypatch):
        monkeypatch.setattr(settings, "NOTIFY_PROVIDER", "log")
        assert notify("測試", "內容") is True

    def test_unknown_provider_falls_back_to_log(self, monkeypatch):
        monkeypatch.setattr(settings, "NOTIFY_PROVIDER", "carrier_pigeon")
        assert notify("測試", "內容") is True

    def test_provider_exception_never_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "NOTIFY_PROVIDER", "telegram")
        monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "t")
        monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "c")
        with patch("utils.http_client.http_get", side_effect=Exception("網路爆炸")):
            assert notify("測試", "內容") is False


class TestTelegram:
    def test_missing_config_falls_back(self, monkeypatch):
        monkeypatch.setattr(settings, "NOTIFY_PROVIDER", "telegram")
        monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "")
        assert notify("測試", "內容") is False

    def test_sends_correct_payload(self, monkeypatch):
        monkeypatch.setattr(settings, "NOTIFY_PROVIDER", "telegram")
        monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "TOKEN123")
        monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "999")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("utils.http_client.http_get", return_value=mock_resp) as mock_get:
            result = notify("開盤前情報", "今日偏多")

        assert result is True
        url = mock_get.call_args[0][0]
        params = mock_get.call_args[1]["params"]
        assert "botTOKEN123" in url
        assert params["chat_id"] == "999"
        assert "開盤前情報" in params["text"]
        assert "今日偏多" in params["text"]

    def test_http_error_returns_false(self, monkeypatch):
        monkeypatch.setattr(settings, "NOTIFY_PROVIDER", "telegram")
        monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "TOKEN123")
        monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "999")

        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"
        with patch("utils.http_client.http_get", return_value=mock_resp):
            assert notify("t", "m") is False
