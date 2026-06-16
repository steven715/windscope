"""分點截圖 OCR：用 Claude vision 從看盤軟體截圖抽取分點買賣超。

需 ANTHROPIC_API_KEY（未設定則停用，手動表單照常可用）。辨識結果僅用來『預填』
表單，使用者核對後才寫入——不盲信 OCR。
"""

import base64
import json
import logging

from config import settings

logger = logging.getLogger(__name__)

_PROMPT = (
    "這是台股看盤軟體的『分點明細／券商買賣超』截圖。抽取每個券商分點的：分點名稱、"
    "買超張數、賣超張數。只回 JSON，格式："
    '{"rows":[{"broker_name":"兆豐-嘉義","buy":12000,"sell":3000}]}。'
    "單位是『張』（整數、去掉逗號）。某格看不清楚就填 0。不要加任何解說文字。"
)


def is_ocr_enabled() -> bool:
    """是否已設定 API key（截圖 OCR 可用）。"""
    return bool(settings.ANTHROPIC_API_KEY)


def extract_chip_from_image(image_bytes: bytes, media_type: str) -> list[dict] | None:
    """從截圖抽分點買賣超。回傳 [{broker_name, buy, sell}] 或 None（未啟用/失敗）。"""
    if not settings.ANTHROPIC_API_KEY:
        logger.warning("chip_ocr: ANTHROPIC_API_KEY 未設定，OCR 停用")
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        b64 = base64.standard_b64encode(image_bytes).decode()
        msg = client.messages.create(
            model=settings.OCR_MODEL,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": _PROMPT},
                ],
            }],
        )
        text = "".join(b.text for b in msg.content
                       if getattr(b, "type", None) == "text")
        return parse_ocr_rows(text)
    except Exception as e:
        logger.error("chip_ocr 辨識失敗: %s", e)
        return None


def parse_ocr_rows(text: str) -> list[dict] | None:
    """從模型回應文字解析出 rows（容錯抓 JSON 區塊）。"""
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None

    rows = []
    for r in data.get("rows", []):
        name = str(r.get("broker_name", "")).strip()
        if not name:
            continue
        rows.append({"broker_name": name,
                     "buy": _to_int(r.get("buy")), "sell": _to_int(r.get("sell"))})
    return rows


def _to_int(v) -> int:
    try:
        return int(float(str(v).replace(",", "").strip() or 0))
    except (ValueError, TypeError):
        return 0
