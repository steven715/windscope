"""分點截圖 OCR 解析測試（不打真實 API，只測 JSON 解析容錯）。"""

from integration.chip_ocr import is_ocr_enabled, parse_ocr_rows


def test_parse_valid_json():
    text = '{"rows":[{"broker_name":"兆豐-嘉義","buy":12000,"sell":3000}]}'
    rows = parse_ocr_rows(text)
    assert rows == [{"broker_name": "兆豐-嘉義", "buy": 12000, "sell": 3000}]


def test_parse_with_surrounding_prose():
    text = '好的，結果如下：\n{"rows":[{"broker_name":"凱基-台北","buy":"5,000","sell":"0"}]}\n以上。'
    rows = parse_ocr_rows(text)
    assert rows == [{"broker_name": "凱基-台北", "buy": 5000, "sell": 0}]


def test_parse_filters_empty_broker_and_cleans_numbers():
    text = '{"rows":[{"broker_name":"","buy":1,"sell":2},{"broker_name":"永豐金-萬盛","buy":"1,234","sell":"-"}]}'
    rows = parse_ocr_rows(text)
    assert rows == [{"broker_name": "永豐金-萬盛", "buy": 1234, "sell": 0}]


def test_parse_invalid_returns_none():
    assert parse_ocr_rows("沒有 JSON") is None
    assert parse_ocr_rows("") is None


def test_ocr_disabled_without_key(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    assert is_ocr_enabled() is False
