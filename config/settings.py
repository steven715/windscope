import os

# DB
DB_PATH = os.environ.get("PREMARKET_DB", "data/premarket.db")

# HTTP
HTTP_TIMEOUT = 30
HTTP_RETRIES = 3
HTTP_DELAY_MIN = 1.0
HTTP_DELAY_MAX = 3.0
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# === FX Thresholds ===
# TWD：離岸晨對晨 delta 門檻。實測晨對晨 |delta| 多落在 0.001–0.128，0.05 在明確
#   隔夜波動時觸發、雜訊 <0.05 維持中性。（v2 時為 0.1，但 TWD 走在岸故 delta≈0 永不觸發。）
FX_THRESHOLD_TWD = 0.05
FX_THRESHOLD_CNY = 0.005
FX_THRESHOLD_KRW = 5.0
# 盤前匯率節奏（原文第一件事①）：跳空＝08:45 vs 前日16:00 變動 ≥ 0.05（5分）；
# 急拉＝最近 5 分 K 單根變動 ≥ 0.03（3分）；取最近 FX_INTRADAY_BARS 根看形狀。
FX_GAP_THRESHOLD = 0.05
FX_INTRADAY_SURGE = 0.03
FX_INTRADAY_BARS = 12
# 避險情緒溫度計（USD/JPY，獨立維度，不進亞幣同步/訊號）：
# 日圓是避險/套利貨幣，急升(USD/JPY 下跌 ≥ 此值) → risk-off、對股市偏空警示。
JPY_RISKOFF_DELTA = 1.0

# === Futures ===
FUTURES_VOLUME_LOOKBACK = 5

# === Chip ===
CHIP_MA_PERIOD = 20
CHIP_MA_MIN_DAYS = 5
PRICE_ZONE_LOW = -20
PRICE_ZONE_CONSOLIDATION = 5
PRICE_ZONE_HIGH = 20

# === Signal Engine (Layer 3) ===
# 門檻來源：專案發想文章的經驗值。調整任何門檻時記得 bump SIGNAL_RULE_VERSION，
# 否則新舊規則的命中率會混在一起統計。
# v2 (2026-06-22): 修正前一交易日基準會抓到休市日空殼 row 的 bug（休市日次一交易日
#   的訊號原本因基準錯置而資料不可用→中性）。屬正確性修正。
#   註：命中率統計不依 rule_version 篩選，bump 僅標記新訊號版本；受影響日的命中率
#   由 recompute + 重新驗證覆蓋舊 row 來修正，非靠版本篩選。
# v3 (2026-06-30): TWD 隔夜 delta 改用離岸 USDTWD=X 晨對晨（取代在岸 08:45 牌價，後者
#   開盤前未更新＝前收、delta≈0 永遠中性，FX 維度等於沒在投票）。門檻 0.1→0.05。
SIGNAL_RULE_VERSION = "v3"
FUTURES_SPREAD_THRESHOLD = 100      # 調整後價差 ±100 點才算有方向
VOLUME_RATIO_HIGH = 1.5             # 夜盤量比 >= 1.5 → 大戶佈局，信心 +1
VOLUME_RATIO_LOW = 0.7              # 夜盤量比 <= 0.7 → 觀望，信心 -1
OI_BEARISH_THRESHOLD = -30000       # 外資淨空單超過 3 萬口，偏多訊號信心 -1
OI_BULLISH_THRESHOLD = 30000        # 外資淨多單超過 3 萬口，偏空訊號信心 -1
CONFIDENCE_MIN = 1
CONFIDENCE_MAX = 5

# === Stock Signals ===
STOCK_NET_AMOUNT_MIN = 5e7          # 買超金額門檻：5,000 萬
STOCK_CONSECUTIVE_MIN = 3           # 連買/連賣天數門檻
STOCK_ACCUMULATION_MIN = 5          # 盤整區吸籌的連買天數門檻

# === 外資流向個股訊號（用 T86 每檔外資買賣超，單位：張）===
# 訊號於 08:50 產出時今日 T86 未收，故用「今日之前」最新（前一交易日）的外資資料。
FOREIGN_CONSECUTIVE_MIN = 2         # 外資連買/連賣天數門檻
FOREIGN_CUM_NET_MIN = 3000          # 連續期間累計張數門檻（過濾雜訊）
FOREIGN_BIG_NET = 10000             # 單日大買/大賣門檻（如「反手大買」）

# === 個股趨勢訊號層（Layer 3b，spec v2：三態趨勢＋順大勢逆小勢）===
# 純技術面，資料來源＝個股還原日K OHLC（raw_stock_daily）。與市場訊號的
# SIGNAL_RULE_VERSION 各自獨立版本，命中率統計才不會混版；調整任一門檻請 bump
# TREND_RULE_VERSION。門檻皆為起始值須回測（回測優先序：FLAT 三症狀 → confirm_bars
# → 超買 bias）。fundamental_gate 目前無基本面資料源 → 一律 UNKNOWN（保守：不放行建倉）。
# t1→t2 (2026-07-05): 趨勢改用還原股價（偵測分割/大除權缺口按比例回推 + 免費配息比例
#   細調），修正 0050 等分割/配息股在除權息期間趨勢失真。屬 spec §6.1「需還原股價」的
#   資料修正，非改判讀門檻。
TREND_RULE_VERSION = "t2"
TREND_MA_BIG = 240              # 年線（大勢方向）；台股慣例 240、A股 250
TREND_MA_CENTER = 20           # 月線＝價值中樞（源修正，非季線）
TREND_MA_FAST = 5              # 小勢回升偵測（5日）
TREND_SLOPE_LOOKBACK = 20      # 年線斜率量測窗
TREND_SLOPE_DEADBAND = 0.02    # 走平死區
TREND_CROSS_WINDOW = 20        # 穿越/箱型量測窗
TREND_CROSS_THRESH = 4         # 反復穿越月線次數門檻
TREND_BOX_EPS = 0.06           # 箱型寬門檻（不創新高低）
TREND_CONFIRM_BARS = 3         # 遲滯確認（連續同態幾根才切換 state）
TREND_OVERBOUGHT_BIAS = 0.15   # 超買乖離（源留白，須自訂+回測）
# 回補歷史 K 線月數：暖機需 MA_BIG(240)+SLOPE_LOOKBACK(20)=260 根（約 13 個月），
# 取 16 個月（~300+ 交易日）留安全邊際（月含假日、暖機門檻才穩）。
TREND_BACKFILL_MONTHS = 16
# 還原股價：單日收盤變動 > 此比例＝公司行為缺口（股票分割/大除權）。台股有 ±10%
# 漲跌幅限制，故單日 >11% 必非市場波動→按 close[i]/close[i-1] 比例回推歷史價。
# 配息（<10%，價格看不出）另用免費 FinMind TaiwanStockDividendResult 的 after/before 比例細調。
# 註：極少數無漲跌幅限制的槓桿/反向/外國 ETF 可能誤判（會在來源說明標註）。
TREND_ADJUST_GAP_PCT = 0.11

# === Verification (Layer 4) ===
VERIFY_FLAT_BAND_PCT = 0.3          # |漲跌幅| <= 0.3% 視為「平」

# === 回測層（Layer 5，backtest）===
# 拿個股趨勢訊號當「動作」重放歷史、算績效，用來提煉/驗證策略（非只看單日命中率）。
# 純離線 f(raw, config)，唯讀不影響既有訊號/排程。調整任一回測參數請 bump
# BACKTEST_RULE_VERSION，不同參數的回測結果才不會混在一起比較。分批進出場參數的
# 實務依據見 docs/roadmap.md（海龜金字塔加碼：最多 4 批、遞減注碼 ½⅓⅙）。
BACKTEST_RULE_VERSION = "b1"
BACKTEST_INITIAL_CAPITAL = 1_000_000   # 名目起始資金（僅供報酬/權益曲線換算；單檔全額配置）
BACKTEST_ENTRY_MODE = "scaled"         # "single"=全進全出 / "scaled"=分批進出
BACKTEST_MAX_UNITS = 3                 # 分批最多幾批（single 模式強制 1）
BACKTEST_TRANCHE_SIZING = "equal"      # "equal"=均分 / "decreasing"=遞減 (n,n-1,…,1)/Σ = ½⅓⅙…
BACKTEST_ADD_COOLDOWN_DAYS = 3         # 兩次加碼最小間隔交易日，避免連日加碼
BACKTEST_FEE_BPS = 14.25               # 手續費：單邊 0.1425%（買賣各收一次）
BACKTEST_TAX_BPS = 30.0                # 證交稅：賣出 0.3%（個股）；ETF 為 0.1%，回測 ETF 另設
BACKTEST_ANNUALIZATION_DAYS = 252      # 年化交易日數（Sharpe / CAGR）
# 回測假設內功閘門一律放行（PASS）：fundamental_gate 無資料源恆 UNKNOWN 會使「關注建倉區」
# 永不觸發＝永不進場。此為方法論「先拋棄基本面、只測技術面」的明確建模假設（見 roadmap）。
BACKTEST_ASSUME_GATE_PASS = True

# === Chip 自動來源（FinMind）===
# 分點明細唯一可自動化的合法來源是 FinMind TaiwanStockTradingDailyReport，
# 需要 Sponsor 等級的 token（https://finmindtrade.com）。未設定時自動來源停用，
# 改用手動 CSV 匯入（python main.py import-chip）。
FINMIND_API_URL = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")

# === 分點截圖 OCR（階段二，視覺 LLM）===
# 未設定 ANTHROPIC_API_KEY 時，/chip-import 的截圖上傳功能停用，手動表單照常可用。
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OCR_MODEL = os.environ.get("OCR_MODEL", "claude-sonnet-4-6")

# === Notify ===
# provider: "log"（預設，寫進 log）或 "telegram"（需設定 token 與 chat_id）
NOTIFY_PROVIDER = os.environ.get("PREMARKET_NOTIFY", "log")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# === Server ===
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8000
# 盤中即時行情背景刷新間隔（秒）：背景排程每隔此秒數抓一次 MIS 存入記憶體快取，
# 頁面/API 只讀快取不阻塞。
LIVE_REFRESH_SECONDS = 12
# 資料瀏覽頁每頁筆數（server-side 分頁，不一次拉全部）
DATA_PAGE_SIZE = 50
SCHEDULE_AFTER_NIGHT = "05:30"
SCHEDULE_BEFORE_OPEN = "08:50"
SCHEDULE_AFTERNOON_FX = "16:00"   # 收盤匯率：16:00 FX 收盤(close_16)，USD/TWD + CNY/KRW/JPY
SCHEDULE_VERIFY_CLOSE = "14:30"   # 13:30 收盤後，證交所指數OHLC約需~1小時才發布，故排14:30
# 18:30：三大法人(T86)/外資個股/除息/期貨未平倉等盤後資料分批發布，~傍晚才齊，故留安全邊際
SCHEDULE_AFTER_CLOSE = "18:30"
SCHEDULE_CHIP_COLLECT = "18:00"   # 籌碼分點收集(個股收盤+分點+算指標)；預設停用，串好來源再開
# APScheduler misfire 容忍秒數：executor 偶有 ~1s 抖動，預設 grace 只有 1s 會讓排程到點
# 時若慢超過 1 秒就整個跳過（曾導致 08:50 before_open 整天不產訊號）。放寬到 30 分鐘：
# 遲到 30 分內仍補跑（每日訊號晚一點勝過沒有），超過才跳過（避免拿盤中資料冒充盤前）。
SCHEDULE_MISFIRE_GRACE_SEC = 1800
