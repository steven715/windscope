# ADR-0008：單一 token 認證——API 用 Bearer、過渡期 Jinja 頁用 HTTP Basic

- 狀態：接受（2026-11-08 Steven 確認「照建議」）
- 日期：2026-11-08
- 相關：ADR-0005（與 tradewind 的邊界）、ADR-0006（API-only、Jinja 過渡期）、tradewind 13-integrations「windscope／認證」

## 背景

server 對外（ngrok）時完全沒有認證：11 個 POST（10 個會改資料——鎖定 target、寫 events、一次清空排程紀錄、觸發回補與回測、
改觀察名單；1 個會花 OCR 費用）、所有讀取端點（含帳戶快照與部位）、`/ws/bars` 都開著。ngrok 進來的連線在 server 看來是本機或
docker 閘道，所以「信任區網」這種依來源 IP 的做法擋不住。使用者只有一個人；tradewind 只用 GET 與 `/ws/bars`，回流走檔案收件匣。

## 決定

1. **一把 token**（`WINDSCOPE_API_TOKEN`，只放 `.env`）保護所有 HTTP 與 WebSocket 請求；公開的只有 `/healthz`、`/static/*`、
   `/manifest.webmanifest`、`/sw.js`。實作是一個 ASGI middleware（`server/auth.py`），route 不用各自加。
2. **程式用 Bearer**：`Authorization: Bearer <token>`（tradewind 從同名環境變數讀，REST 與串流握手都帶）。
3. **瀏覽器用 HTTP Basic**（帳號隨意、密碼＝token），不做登入頁、不發 cookie：CLAUDE.md 規則 1 不准新增 Jinja 頁面，而 Basic
   讓瀏覽器自己記住、同源的頁面、表單、fetch 都會自動帶。缺點是沒有登出（關掉瀏覽器或清除帳密），單人使用可接受。
4. **跨站偽造**：瀏覽器會自動帶 Basic 帳密，所以「用 Basic 認證的會改資料請求（POST／PUT／PATCH／DELETE）與 WebSocket 握手」
   要 `Origin`（沒有就 `Referer`）的 host 等於 `Host`，否則 403。Bearer 需要程式自己加 header，不受這條限制。
5. **沒設 token＝不認證**（本機開發、測試），開機記 ERROR；正式部署由 docker-compose 的 `${WINDSCOPE_API_TOKEN:?…}` 擋，沒設起不來。
6. 比對用 `hmac.compare_digest`；失敗記 WARNING（來源 IP 與路徑、同一 IP 每 60 秒一筆），token 不進 log。WebSocket 被拒時握手直接回
   401／403（client 看得到狀態碼）。
7. **同源反向代理先不做**：等前端 repo 建好，再用它把前端與 `/api` 收在同一個 origin（順便不需要 CORS）。

## 後果

- ngrok 對外的每個端點都要 token；tradewind 要先上「帶 token」的版本（沒設不帶，先上不影響），windscope 才能設 token。
- 換 token：改兩邊的 `.env`、重啟；瀏覽器會重新彈出對話框。
- 前端 repo 上線時：同源部署可以沿用 Basic，或改成 Bearer（前端自己存 token）；屆時再寫 ADR。
