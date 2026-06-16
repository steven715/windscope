# 資料來源文件（Data Sources）

本文件記錄 Phase 1 所有資料來源的 URL、格式、可靠性、驗證狀態。
每次新增或修改來源時，同步更新此文件。

---

## 狀態定義

| 狀態 | 意義 |
|------|------|
| ✅ VERIFIED | 已實測可用，parser 已實作 |
| 🔧 PARTIAL | URL 可達，但 parser 尚未完成或有已知問題 |
| ❓ UNVERIFIED | 尚未實測，URL 和格式來自文件推測 |
| 🚫 BLOCKED | 已確認無法使用（被擋、需付費、格式不符） |
| 📌 STUB | 程式中已留 stub，等待替換 |

---

## 一、證交所（TWSE）

### 1.1 三大法人買賣超（大盤）

| 項目 | 內容 |
|------|------|
| 用途 | 取得外資/投信/自營商每日買賣超金額 |
| URL | `https://www.twse.com.tw/rwd/zh/fund/BFI82U?date={YYYYMMDD}&response=json` |
| 方法 | GET |
| 回傳格式 | JSON |
| 更新時間 | 每個交易日 ~15:00 |
| 狀態 | ✅ VERIFIED（2026-04-12 fixture 驗證） |
| 備註 | `stat` 欄位為 `"OK"` 表示有資料；非交易日回傳 `"很抱歉，沒有符合條件的資料"`。Parser 已處理逗號數字、括號負數、日期比對。|
| ⚠️ 限制 | **不支援歷史查詢**（2026-06-12 backfill 實測）：date 參數被忽略，永遠回傳最新交易日。Parser 的日期比對防呆會擋下錯置資料，因此歷史回補時此來源一律失敗，法人資料只能逐日累積。|

---

### 1.2 每月加權指數行情

| 項目 | 內容 |
|------|------|
| 用途 | 取得加權指數每日收盤價（`spot_close`） |
| URL | `https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?date={YYYYMMDD}&response=json` |
| 方法 | GET |
| 回傳格式 | JSON |
| 更新時間 | 每個交易日收盤後 |
| 狀態 | ✅ VERIFIED（fixture 驗證） |
| 備註 | 回傳整個月的資料，需從 `data` 陣列中找到目標日期那一列。日期格式為民國年（如 `115/04/08`）。收盤價在第 5 欄（index 4）。Parser 已實作民國年轉換。|

---

### 1.2b 加權指數每日開高低收（OHLC）

| 項目 | 內容 |
|------|------|
| 用途 | 取得加權指數當日開盤/最高/最低/收盤，供 Layer 4 驗證引擎判定開盤跳空與當日漲跌 |
| URL | `https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?date={YYYYMMDD}&response=json` |
| 方法 | GET |
| 回傳格式 | JSON |
| 更新時間 | 每個交易日收盤後 |
| 狀態 | ✅ VERIFIED（2026-06-12 真實回應驗證，fixture: `mi_5mins_hist_202606.json`） |
| 備註 | 回傳整月每日 OHLC。欄位：日期(民國年)/開盤/最高/最低/收盤，逗號數字。存入 `raw_index` 表。|

---

### 1.2c 加權指數盤中即時（MIS）

| 項目 | 內容 |
|------|------|
| 用途 | 開盤後即時加權指數，供 `/live` 盤中即時驗證觀察（對早上訊號做雙基準三分類） |
| URL | `https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_t00.tw&json=1&delay=0` |
| 方法 | GET（需帶 `Referer: https://mis.twse.com.tw/stock/index.jsp` 與 UA） |
| 回傳格式 | JSON |
| 更新時間 | 盤中（09:00–13:30）約 5–20 秒一檔快照；非真串流，輪詢取得 |
| 狀態 | ✅ VERIFIED（2026-06-15 真實回應驗證，fixture: `mis/getstockinfo_t00_2330.json`） |
| 備註 | `tse_t00.tw` = 加權指數，個股用 `tse_<code>.tw`。`rtcode="0000"` 為 OK，資料在 `msgArray[]`。關鍵欄位：`y`=昨收（雙基準 prev_close）、`o`=開盤（開盤跳空）、`z`=即時成交（即時漲跌）、`h`/`l`=當日高低、`tlong`=毫秒時間戳。**無成交時 `z='-'`**，parser fallback 到 `o`。**唯讀，不寫入 premarket.db**（即時資料只進記憶體，避免污染日線模型）。collector：`collectors/mis.py`（不繼承 BaseCollector）。|

---

### 1.3 個股每日收盤價

| 項目 | 內容 |
|------|------|
| 用途 | 取得觀察名單個股的收盤價，用於籌碼金額計算和 MA20 |
| URL | `https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={YYYYMMDD}&stockNo={stock_id}&response=json` |
| 方法 | GET |
| 回傳格式 | JSON |
| 更新時間 | 每個交易日收盤後 |
| 狀態 | ✅ VERIFIED（fixture 驗證） |
| 備註 | 回傳整個月的日行情。收盤價在第 7 欄（index 6）。每次只能查一檔，watchlist 多檔時逐一查詢，透過 http_client 的 delay 控制禮貌爬蟲。|

---

### 1.4 外資個股買賣超

| 項目 | 內容 |
|------|------|
| 用途 | 取得外資對觀察名單個股的每日買賣超 |
| URL | `https://www.twse.com.tw/rwd/zh/fund/T86?date={YYYYMMDD}&selectType=ALL&response=json` |
| 方法 | GET |
| 回傳格式 | JSON |
| 更新時間 | 每個交易日 ~15:30 |
| 狀態 | ✅ VERIFIED（fixture 驗證） |
| 備註 | 回傳全市場外資買賣超。Parser 自動篩選 watchlist 中的 stock_id。注意資料單位為「股」需除以 1000 轉為「張」。stock_id 欄位可能帶空白需 strip。|

---

### 1.5 除息預估點數

| 項目 | 內容 |
|------|------|
| 用途 | 取得當日除息對加權指數的預估影響點數，用於調整期貨價差 |
| URL | `https://www.twse.com.tw/rwd/zh/exRight/TWT49U?date={YYYYMMDD}&response=json` |
| 方法 | GET |
| 回傳格式 | JSON（待實測確認） |
| 更新時間 | 交易日（僅有除息日才有資料） |
| 狀態 | 🔧 PARTIAL |
| 備註 | URL 為推測。Parser 嘗試從 `notes` 欄位解析「影響加權指數約 X 點」。請求失敗或非除息日均回傳 `{"ex_dividend_points": 0.0}`，不會導致系統中斷。需實測確認 URL 是否可用。|

---

## 二、期交所（TAIFEX）

### 2.1 期貨每日行情（含夜盤）

| 項目 | 內容 |
|------|------|
| 用途 | 取得台指期夜盤收盤價和成交量 |
| URL | `https://www.taifex.com.tw/cht/3/futDataDown` |
| 方法 | POST（表單查詢） |
| 回傳格式 | CSV |
| 更新時間 | 夜盤收盤後（次日 ~05:15） |
| 狀態 | ✅ VERIFIED（2026-06-12 真實回應驗證，fixture: `fut_data_20260611.csv`） |
| 備註 | POST 參數：`down_type=1`、`commodity_id=TX`、`queryStartDate={YYYY/MM/DD}`、`queryEndDate={YYYY/MM/DD}`。Parser 解析 CSV 找到近月 TX 的「盤後」交易時段。CSV 編碼為 Big5。**注意：** 原先用的 `futContractsDateDown` + `queryType/marketCode` 參數實測回傳 HTML 錯誤頁（該 URL 是三大法人下載端點），2026-06-12 修正。假日/查無資料時回傳 HTML 錯誤頁（非 CSV），parser 找不到 header 會回 None。|

---

### 2.2 三大法人期貨留倉（外資未平倉）

| 項目 | 內容 |
|------|------|
| 用途 | 取得外資台指期未平倉淨額（口數） |
| URL | `https://www.taifex.com.tw/cht/3/futContractsDateDown` |
| 方法 | POST |
| 回傳格式 | CSV（Big5） |
| 更新時間 | 每個交易日 ~15:00 |
| 狀態 | ✅ VERIFIED（2026-06-12 真實回應驗證，fixture: `oi_foreign_20260611.csv`） |
| 備註 | POST 參數：`queryStartDate={YYYY/MM/DD}`、`queryEndDate={YYYY/MM/DD}`、`commodityId=TXF`。實際欄位：身份別為「外資及陸資」、淨額欄位為「多空未平倉口數淨額」（與原推測的「外資」「多空淨額口數」不同，parser 已修正）。假日回傳 HTML 錯誤頁，parser 回 None。|

---

## 三、匯率（FX）

### 3.1 台灣銀行牌告匯率

| 項目 | 內容 |
|------|------|
| 用途 | 取得 USD/TWD 即期買入匯率 |
| URL（CSV） | `https://rate.bot.com.tw/xrt/flcsv/0/day` |
| 方法 | GET |
| 回傳格式 | CSV |
| 更新時間 | 營業日即時更新 |
| 狀態 | ✅ VERIFIED（2026-06-15 真實回應驗證，fixture: `bot_tw_csv.csv`） |
| 備註 | **2026-06-15 修正：** (1) CSV 帶 UTF-8 BOM，表頭第一格變 `﻿幣別`，`.strip()` 不去 BOM 導致 `幣別` 比對失敗 → parser 先 `lstrip("﻿")`。(2) 台銀已改 flcsv 格式：表頭為「幣別,匯率,現金,**即期**,遠期10天…」買入區塊後接相同欄位的賣出區塊，「即期」出現兩次；即期買入 = **第一個「即期」欄**（賣出在第二區塊）。原 parser 找的 `即期買入` 欄名已不存在。修正前 collector 對 live 資料 100% 失效（回 None）。|

---

### 3.2 Yahoo Finance 匯率

| 項目 | 內容 |
|------|------|
| 用途 | USD/CNY、USD/KRW 即時報價 |
| URL | `https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=2d` |
| 幣對代碼 | `USDCNY=X`, `USDKRW=X` |
| 方法 | GET |
| 回傳格式 | JSON |
| 狀態 | ✅ VERIFIED（fixture 驗證） |
| 備註 | Parser 從 `chart.result[0].indicators.quote[0].close` 取最後一筆非 None 收盤價。Yahoo Finance 有時會擋非瀏覽器請求，可能需要 cookie/crumb 機制。實際上線時需驗證穩定性。|

---

### 3.3 鉅亨網匯率（備選）

| 項目 | 內容 |
|------|------|
| 用途 | USD/TWD、USD/CNY、USD/KRW 即時匯率（方案 B） |
| URL | `https://www.cnyes.com/forex/detail/USD-TWD/overview` |
| 狀態 | ❓ UNVERIFIED |
| 備註 | SPA 架構，直接 requests.get 可能拿不到匯率數值。Phase 1 不使用。|

---

## 四、分點籌碼

### 4.1 證交所券商買賣日報

| 項目 | 內容 |
|------|------|
| 用途 | 取得個股分點進出明細 |
| URL | `https://www.twse.com.tw/rwd/zh/fund/TWT43U` / `https://bsr.twse.com.tw/bshtm/` |
| 狀態 | 🚫 BLOCKED（2026-06-12 實測確認） |
| 備註 | TWT43U 實測回傳「自營商買賣超彙總表」（全市場彙總，`stockNo` 參數被忽略），**不是**分點明細。官方券商買賣日報表（bsr.twse.com.tw）有 CAPTCHA，無法（也不應）自動化。TWSE OpenAPI 亦無分點資料集（已查 swagger）。自動來源改用 FinMind（見 4.1b）。|

---

### 4.1b FinMind 券商分點（自動來源）

| 項目 | 內容 |
|------|------|
| 用途 | 取得 watchlist 個股的券商分點買賣明細 |
| URL | `https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockTradingDailyReport&data_id={stock_id}&start_date={date}&end_date={date}&token={token}` |
| 方法 | GET |
| 回傳格式 | JSON |
| 更新時間 | 週一至五 21:00（FinMind 文件） |
| 狀態 | 🔧 PARTIAL（程式已接通；需使用者取得 FinMind **Sponsor 等級** token 才會啟用） |
| 備註 | 設定環境變數 `FINMIND_TOKEN` 後自動啟用，未設定時 `collect_broker_trading()` 回 None（after_close job 該步驟顯示失敗但不中斷）。回應每列為「同券商不同成交價位」且單位是「股」，collector 會加總成每券商一筆 buy/sell/net 並換算為「張」（÷1000，與 raw_chip 其他來源及 chip_metrics 的金額計算一致）。Schema 依官方文件（finmind.github.io/tutor/TaiwanMarket/Chip/）構造 fixture，2026-06-12 無付費 token 無法實抓驗證；等級不足時 API 回 `{"status": 400, "msg": "Your level is free..."}`，已處理。免費等級實測確認不含此 dataset。|

---

### 4.2 手動 CSV 匯入（保底方案）

| 項目 | 內容 |
|------|------|
| 用途 | 當所有自動來源不可用時，由使用者手動匯入 |
| 格式 | CSV，欄位：`date,stock_id,stock_name,broker_name,buy_volume,sell_volume,net_volume` |
| 狀態 | ✅ VERIFIED |
| 備註 | `import_from_csv()` 已實作並通過測試。支援冪等匯入（重複匯入不報錯）、格式錯誤行自動跳過。CLI 指令：`python main.py import-chip path/to/chip.csv`。|

---

### 4.3 籌碼K線等第三方網站（評估，2026-06-16）

| 項目 | 內容 |
|------|------|
| 用途 | 想用觸發腳本爬「籌碼K線」免費版的分點明細回填 raw_chip |
| 狀態 | 🚫 不採用（建議手動或付費 FinMind） |
| 備註 | 籌碼K線（與卡方、神秘金字塔等）為商業產品：網頁版多為登入後的 SPA、API 非公開、有反爬蟲機制，且 ToS 通常禁止自動擷取。寫爬蟲既脆弱又有合規疑慮，不建議。**結論**：分點自動化唯一合法途徑仍是 FinMind Sponsor token（4.1b，付費）；免費路徑＝使用者手動看籌碼K線後，用 `import-chip` CSV 匯入（4.2）。原文工作流本身即為手動（免費版看近 5 天、週末截圖留存）。|

---

## 五、進階來源（Phase 1 非必要）

### 5.1 富台指（FTSE TWSE Taiwan 50 Futures）

| 項目 | 內容 |
|------|------|
| 用途 | 台指期 vs 富台指的價差比對 |
| 狀態 | ❓ UNVERIFIED |
| 備註 | Phase 1 先留 `NULL`。`raw_futures.ftse_tw_close` 欄位預留但不強制。|

### 5.2 紐約盤 USD/TWD 收盤價

| 項目 | 內容 |
|------|------|
| 用途 | 對照紐約盤 vs 台北盤的匯率差異 |
| 狀態 | ❓ UNVERIFIED |
| 備註 | Phase 1 先留 `NULL`。`raw_fx.ny_close` 欄位預留但不強制。|

### 5.3 S&P 500 收盤

| 項目 | 內容 |
|------|------|
| 用途 | 美股收盤對照（存入 `raw_futures.sp500_close`） |
| URL | `https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?interval=1d&range=2d` |
| 狀態 | ✅ VERIFIED（2026-06-12 真實回應驗證，fixture: `yahoo_gspc_20260612.json`） |
| 備註 | 與 FX 共用 Yahoo Finance chart API 與 parser（`FXCollector.collect_sp500()`）。代碼 `^GSPC`。after_night job 收集。|

---

## 來源優先順序總結

| 資料 | 首選 | 備選 | 保底 |
|------|------|------|------|
| 三大法人 | 證交所 JSON API ✅ | — | — |
| 加權指數收盤 | 證交所 JSON API ✅ | — | — |
| 個股收盤價 | 證交所 JSON API ✅ | — | — |
| 外資個股買賣超 | 證交所 JSON API ✅ | — | — |
| 除息點數 | 證交所 JSON API 🔧 | — | 回傳 0（安全降級） |
| 期貨夜盤 | 期交所 CSV ✅ | 期交所 HTML | — |
| 外資期貨未平倉 | 期交所 CSV ✅ | — | — |
| USD/TWD 匯率 | 台灣銀行 CSV ✅ | 鉅亨網 | 手動輸入 |
| USD/CNY 匯率 | Yahoo Finance ✅ | 鉅亨網 | 手動輸入 |
| USD/KRW 匯率 | Yahoo Finance ✅ | 鉅亨網 | 手動輸入 |
| 分點籌碼 | FinMind API 🔧（需 Sponsor token） | — | CSV 手動匯入 ✅ |
| S&P 500 | Yahoo Finance ✅ | — | — |
| 富台指 | — | — | Phase 1 略過 |
| 紐約盤匯率 | — | — | Phase 1 略過 |

---

## 變更紀錄

| 日期 | 變更內容 |
|------|----------|
| 2026-04-12 | 初版建立，所有來源標記為 UNVERIFIED |
| 2026-04-13 | Round 2 完成：BFI82U/FMTQIK/STOCK_DAY/T86 標記 VERIFIED；期交所 CSV 標記 VERIFIED；台銀 CSV/Yahoo Finance 標記 VERIFIED；TWT49U 標記 PARTIAL；OI/Chip 標記 STUB；CSV import 標記 VERIFIED |
| 2026-06-12 | v2 R1：新增 MI_5MINS_HIST（加權指數 OHLC）標記 VERIFIED |
| 2026-06-12 | backfill 實測歷史回補能力：**可回補** = FMTQIK、STOCK_DAY、T86、MI_5MINS_HIST（月查詢含歷史）；**不可回補** = BFI82U（date 參數被忽略）、台銀匯率 CSV（只有即時牌價）、期交所夜盤（僅當日 CSV）。不可回補的來源只能逐日累積。|
| 2026-06-12 | 補齊三個 STUB：(1) 外資 OI 實測接通（futContractsDateDown，VERIFIED），順帶發現並修正夜盤端點錯誤（futContractsDateDown→futDataDown）；(2) 分點：TWT43U 實測為自營商彙總表非分點、bsr 有 CAPTCHA → 標 BLOCKED，自動來源改接 FinMind（PARTIAL，需 Sponsor token，環境變數 `FINMIND_TOKEN`）；(3) S&P 500 ^GSPC 實測接通（VERIFIED）。|
| 2026-06-15 | 台銀匯率 CSV 修正：UTF-8 BOM + 台銀改 flcsv 格式（即期買入＝第一個「即期」欄），修正前對 live 資料 100% 失效（見 3.1）。新增 MIS 盤中即時加權指數（1.2c，VERIFIED），供 `/live` 盤中即時驗證觀察。|
