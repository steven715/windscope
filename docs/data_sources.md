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
| URL | `https://www.taifex.com.tw/cht/3/futContractsDateDown` |
| 方法 | POST（表單查詢） |
| 回傳格式 | CSV |
| 更新時間 | 夜盤收盤後（次日 ~05:15） |
| 狀態 | ✅ VERIFIED（fixture 驗證） |
| 備註 | POST 參數：`queryType=1`、`marketCode=0`、`commodity_id=TX`、`queryDate={YYYY/MM/DD}`。Parser 解析 CSV 找到近月 TX 的「盤後」交易時段。CSV 編碼為 Big5。|

---

### 2.2 三大法人期貨留倉（外資未平倉）

| 項目 | 內容 |
|------|------|
| 用途 | 取得外資台指期未平倉淨額（口數） |
| URL | `https://www.taifex.com.tw/cht/3/totalTableDate` |
| 方法 | POST |
| 回傳格式 | HTML 或 CSV |
| 更新時間 | 每個交易日 ~15:00 |
| 狀態 | 📌 STUB |
| 備註 | `collect_oi_foreign()` 目前為 stub 回傳 None。CSV 解析邏輯已在 `collect_oi_foreign_from_csv()` 實作並通過測試。待實測確認真實 API 的 POST 參數和回傳格式後接通。|

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
| 狀態 | ✅ VERIFIED（fixture 驗證） |
| 備註 | Parser 尋找「幣別=USD」列的「即期買入」欄位值。CSV 格式穩定。|

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
| URL | `https://www.twse.com.tw/rwd/zh/fund/TWT43U?date={YYYYMMDD}&stockNo={stock_id}&response=json` |
| 方法 | GET |
| 回傳格式 | JSON（待確認） |
| 更新時間 | 每個交易日 ~18:00 |
| 狀態 | 📌 STUB |
| 備註 | `collect_broker_trading()` 目前為 stub 回傳 None。需實測確認此 URL 是否回傳個股的券商買賣明細。|

---

### 4.2 手動 CSV 匯入（保底方案）

| 項目 | 內容 |
|------|------|
| 用途 | 當所有自動來源不可用時，由使用者手動匯入 |
| 格式 | CSV，欄位：`date,stock_id,stock_name,broker_name,buy_volume,sell_volume,net_volume` |
| 狀態 | ✅ VERIFIED |
| 備註 | `import_from_csv()` 已實作並通過測試。支援冪等匯入（重複匯入不報錯）、格式錯誤行自動跳過。CLI 指令：`python main.py import-chip path/to/chip.csv`。|

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
| 用途 | 美股收盤對照 |
| URL | `https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?interval=1d&range=5d` |
| 狀態 | ❓ UNVERIFIED |
| 備註 | 與 FX 共用 Yahoo Finance 來源。代碼 `^GSPC`。|

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
| 外資期貨未平倉 | 期交所 CSV 📌 | — | — |
| USD/TWD 匯率 | 台灣銀行 CSV ✅ | 鉅亨網 | 手動輸入 |
| USD/CNY 匯率 | Yahoo Finance ✅ | 鉅亨網 | 手動輸入 |
| USD/KRW 匯率 | Yahoo Finance ✅ | 鉅亨網 | 手動輸入 |
| 分點籌碼 | 證交所券商日報 📌 | — | CSV 手動匯入 ✅ |
| S&P 500 | Yahoo Finance ❓ | — | — |
| 富台指 | — | — | Phase 1 略過 |
| 紐約盤匯率 | — | — | Phase 1 略過 |

---

## 變更紀錄

| 日期 | 變更內容 |
|------|----------|
| 2026-04-12 | 初版建立，所有來源標記為 UNVERIFIED |
| 2026-04-13 | Round 2 完成：BFI82U/FMTQIK/STOCK_DAY/T86 標記 VERIFIED；期交所 CSV 標記 VERIFIED；台銀 CSV/Yahoo Finance 標記 VERIFIED；TWT49U 標記 PARTIAL；OI/Chip 標記 STUB；CSV import 標記 VERIFIED |
