---
name: telegram-setup
description: 設定或修復本專案的 Telegram 通知：取得 chat id、寫入 .env、啟用 docker-compose 的通知環境變數、重啟容器、發送測試訊息。當使用者想設定 Telegram 通知、換 bot token、換接收對象、或通知收不到要排查時使用。
---

# Telegram 通知設定

目標：讓 premarket 容器的 08:50 訊號摘要與 13:40 驗證結果推送到使用者的 Telegram。

## 前置知識

- 通知設定讀三個環境變數（見 `config/settings.py`）：`PREMARKET_NOTIFY=telegram`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`
- 專案慣例：secrets 放專案根目錄 `.env`（已在 .gitignore），`docker-compose.yml` 以 `${VAR}` 引用
- 輔助腳本：`.claude/skills/telegram-setup/scripts/tg.py`（純標準庫）

## 步驟

1. **取得 token**：若使用者尚未提供 bot token，請他先到 @BotFather 建 bot 拿 token，並提醒他**先在 Telegram 傳一句話給該 bot**（bot 無法主動發起對話，沒這步查不到 chat id）。
2. **查 chat id**：
   ```
   python .claude/skills/telegram-setup/scripts/tg.py chat-id <TOKEN>
   ```
   - 列出多個 chat 時，請使用者選一個（個人為正數、群組為負數）
   - exit code 2 = 使用者還沒傳訊息給 bot，請他傳了再重跑
3. **寫入 `.env`**（專案根目錄，存在則更新對應 key、保留其他行）：
   ```
   TELEGRAM_BOT_TOKEN=<token>
   TELEGRAM_CHAT_ID=<chat_id>
   ```
   注意：不要把 token 原文重複貼回對話，引用時遮蔽中段。
4. **啟用 compose 設定**：確認 `docker-compose.yml` 的 environment 區塊有以下三行（在範本中是註解，解開即可）：
   ```yaml
   PREMARKET_NOTIFY: telegram
   TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN}
   TELEGRAM_CHAT_ID: ${TELEGRAM_CHAT_ID}
   ```
5. **重啟容器**：`docker compose up -d`（compose 會自動讀 `.env`）
6. **驗證**：
   ```
   python .claude/skills/telegram-setup/scripts/tg.py test <TOKEN> <CHAT_ID>
   ```
   請使用者確認 Telegram 收到「✅ 開盤前情報系統：通知測試成功」。
7. （可選）端到端驗證走容器內的 notify 路徑：
   ```
   docker exec premarket python -c "from utils.notify import notify; print(notify('測試', '容器內通知路徑 OK'))"
   ```
   印出 `True` 且 Telegram 收到訊息即全通。

## 排查

- 測試訊息 OK 但排程通知沒來 → `docker exec premarket printenv | grep -E "PREMARKET_NOTIFY|TELEGRAM"` 確認環境變數有進容器（漏掉第 4/5 步最常見）
- API 回 401 → token 錯；回 400 chat not found → chat id 錯或使用者沒先跟 bot 對話過
- 通知失敗不會弄掛 job（`utils/notify.py` 設計如此），所以收不到通知時 job 本身可能仍正常，去 `logs/` 看 `[NOTIFY]` 或 ERROR
