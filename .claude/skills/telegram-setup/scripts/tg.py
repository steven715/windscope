"""Telegram 通知設定小工具（只用標準庫，不需安裝任何套件）。

用法：
    python tg.py chat-id <bot_token>           # 列出最近跟 bot 互動過的 chat
    python tg.py test <bot_token> <chat_id>    # 發送測試訊息
"""

import json
import sys
import urllib.parse
import urllib.request


def api(token: str, method: str, params: dict | None = None) -> dict:
    """呼叫 Telegram Bot API，回傳解析後的 JSON。"""
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.load(resp)


def cmd_chat_id(token: str) -> None:
    """從 getUpdates 找出所有互動過的 chat，列出 id 與名稱。"""
    data = api(token, "getUpdates")
    if not data.get("ok"):
        print(f"API 錯誤：{data}")
        sys.exit(1)

    chats: dict[int, dict] = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat

    if not chats:
        print("找不到任何對話。請先在 Telegram 裡傳一句話給你的 bot（按 Start 也行），再執行一次。")
        sys.exit(2)

    for cid, chat in chats.items():
        name = (
            chat.get("title")
            or f"{chat.get('first_name', '')} {chat.get('last_name', '')}".strip()
            or chat.get("username", "")
        )
        print(f"chat_id={cid}\ttype={chat['type']}\tname={name}")


def cmd_test(token: str, chat_id: str) -> None:
    """發送測試訊息。"""
    data = api(token, "sendMessage", {
        "chat_id": chat_id,
        "text": "✅ 開盤前情報系統：通知測試成功",
    })
    if data.get("ok"):
        print("OK，測試訊息已送出")
    else:
        print(f"失敗：{data}")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "chat-id":
        cmd_chat_id(sys.argv[2])
    elif len(sys.argv) >= 4 and sys.argv[1] == "test":
        cmd_test(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
        sys.exit(1)
