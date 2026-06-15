# Spec — 盤中即時驗證觀察（Live Signal Verification Overlay）

狀態：草案 v1（2026-06-15）

## 1. 目標

把現有 Layer 4 收盤驗證（13:40 一次性）延伸成**盤中連續觀察**：開盤後即時抓加權指數，
用同一套三分類門檻，持續顯示「早上 08:50 那筆訊號目前走對還是背離」，並在收盤時自然
收斂到 `verify_close` 的結果。

**核心定位：唯讀觀察，綁在既有驗證目的上。不產生新訊號、不觸發任何動作。**

## 2. 範圍

### In
- 盤中（交易日 09:00–13:30）即時抓**加權指數**（必要），watchlist 個股即時報價（選配）。
- 用 `verification.py` 既有三分類門檻，即時算：當日漲跌%、開盤跳空、目前命中/背離、偏離幅度。
- Web 頁 `/live` + SSE 推送，server-rendered + vanilla JS（不引入前端框架）。
- 交易日曆 + 時段判斷：非盤中/假日不連線，自動啟停。

### Out（明確不做）
- 盤中產生新訊號或調整早上訊號（signal_engine 完全不碰）。
- 個股級的即時下單/警示/動作。
- 把即時 tick 寫進 `premarket.db` 的日線資料模型（即時資料原則上只進記憶體）。
- 真 tick 級串流（券商 API）；本期用證交所 MIS 輪詢即可。

### 範圍文件影響
CLAUDE.md「仍然不做：…盤中即時監控」需改寫為「盤中即時監控（**除訊號驗證觀察外**）」，
保持範圍誠實。

## 3. 資料來源（已實測 2026-06-15）

證交所 MIS 即時行情 JSON：
```
GET https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_t00.tw&json=1&delay=0
Headers: User-Agent, Referer: https://mis.twse.com.tw/stock/index.jsp
```
- `tse_t00.tw` = 發行量加權股價指數（TAIEX）。個股用 `tse_<code>.tw`（如 `tse_2330.tw`）。
- 回應 `rtcode="0000"`、`msgArray[]`，每筆關鍵欄位：

  | 欄位 | 意義 | 用途 |
  |---|---|---|
  | `y` | 昨收 | 漲跌%/跳空 的基準（prev_close） |
  | `o` | 開盤 | 開盤跳空 |
  | `z` | 即時成交 | 即時漲跌% |
  | `h`/`l` | 當日高/低 | 顯示用 |
  | `tlong` | 毫秒時間戳 | 報價時間 |

- **注意**：盤前/無成交時 `z` 可能是 `'-'`，需 fallback 到 `o`（或上一筆有效 z）。
- 輪詢非真串流；約 5–10 秒一個快照。MIS 有 rate limit，需帶 Referer/UA。
- `http_client.http_get` 內建 1–3s random delay + retry；輪詢間隔需 ≥ 該延遲（建議 10s）。

## 4. 元件（全部旁掛，純加法，不動既有閉環）

1. **`collectors/mis.py`** — `MISCollector`（**不繼承 BaseCollector**，因無 DB save，paradigm 不同）。
   `collect_index(symbol="t00") -> dict | None`，解析 MIS JSON 回 `{symbol,name,price,prev_close,open,high,low,ts}`。
2. **`integration/verification.py` 重構** — 抽出純函式
   `classify_against_benchmarks(predicted_direction, prev_close, open, current) -> dict`
   （回 day_change_pct/open_gap_pct/各 class/hit_day/hit_open），讓 `verify_signal` 與 `/live`
   共用同一份門檻邏輯，保證盤中觀察與 13:40 結果一致。
3. **`server/live_tracker.py`** — asyncio 常駐 task。交易日 09:00–13:30 才輪詢 MIS，最新報價
   存記憶體（dataclass / module-level state），收盤或假日停。
4. **`server/routes/live.py` + `templates/live.html`** — `/live` 頁顯示早上訊號（方向/信心/理由）
   ＋即時指數＋即時漲跌%＋即時跳空＋「目前 ✓符合 / ✗背離」＋偏離幅度；`/live/stream` SSE endpoint
   持續 push，頁面用 `EventSource` 接收。

## 5. 驗收標準

- AC1：`MISCollector.collect_index("t00")` 對 fixture 正確解析出 price/prev_close/open；`z='-'` 時 fallback。
- AC2：MIS 回 rtcode≠0000 或網路失敗時回 `None`，不丟例外。
- AC3：`classify_against_benchmarks` 重構後，既有 `verify_signal` 行為不變（既有測試全綠）。
- AC4：`/live` 在盤中回 200 並含早上訊號 + 即時命中狀態；非交易日/盤前顯示空狀態。
- AC5：live_tracker 在非盤中時段不發 HTTP（時段 gating 有測試）。
- AC6：即時資料不寫入 `premarket.db`（grep 確認無 raw_* 寫入）。

## 6. 任務切片（incremental）

- **Slice 1（本次 thin slice）**：`collectors/mis.py` + fixture + 測試。盤中能抓到即時指數即可。
- Slice 2：重構 `verification.py` 抽出 `classify_against_benchmarks`，既有測試保持綠。
- Slice 3：`live_tracker` 記憶體狀態 + 時段 gating + 測試。
- Slice 4：`/live` 頁 + SSE + vanilla JS。
- Slice 5：CLAUDE.md 範圍文字更新 + docs/data_sources.md 補 MIS 來源。

## 7. 待決問題

- Q1：個股即時報價要不要一起做（對應個股觀察訊號），或本期只做大盤？
- Q2：live_tracker 放 FastAPI 同進程 asyncio task，還是獨立 container？（同進程較簡單，但 server 重啟會中斷）
- Q3：即時資料要不要做「盤中快照」落盤（如每 5 分鐘一筆）供事後回看，還是純記憶體即看即丟？
