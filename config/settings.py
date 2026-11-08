import os

# 共用純函式庫（ADR-0007）：趨勢／還原／Playbook 的門檻單一真相在它那裡，這裡只取值
from strategylib.adjust import DEFAULT_GAP_PCT as _ADJUST_GAP_PCT
from strategylib.playbooks.pullback_hold import DEFAULT_PARAMS as _PULLBACK_PARAMS
from strategylib.playbooks.trend_pullback import DEFAULT_PARAMS as _TREND_PULLBACK_PARAMS
from strategylib.trend import DEFAULTS as _TREND_DEFAULTS
from strategylib.trend import RULE_VERSION as _TREND_RULE_VERSION

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
# 規則本體與門檻的**單一真相在共用庫 strategylib.trend**（windscope／tradewind 共用，ADR-0007）；
# 這裡只是取值供既有程式碼引用，不要在此另改數字——改門檻去 strategylib.trend.DEFAULTS 並 bump
# 其 RULE_VERSION（不同版本的績效不混算）。fundamental_gate 目前無基本面資料源 → 一律 UNKNOWN。
TREND_RULE_VERSION = _TREND_RULE_VERSION
TREND_MA_BIG = _TREND_DEFAULTS["ma_big"]
TREND_MA_CENTER = _TREND_DEFAULTS["ma_center"]
TREND_MA_FAST = _TREND_DEFAULTS["ma_fast"]
TREND_SLOPE_LOOKBACK = _TREND_DEFAULTS["slope_lookback"]
TREND_SLOPE_DEADBAND = _TREND_DEFAULTS["slope_deadband"]
TREND_CROSS_WINDOW = _TREND_DEFAULTS["cross_window"]
TREND_CROSS_THRESH = _TREND_DEFAULTS["cross_thresh"]
TREND_BOX_EPS = _TREND_DEFAULTS["box_eps"]
TREND_CONFIRM_BARS = _TREND_DEFAULTS["confirm_bars"]
TREND_OVERBOUGHT_BIAS = _TREND_DEFAULTS["overbought_bias"]
# 回補歷史 K 線月數：暖機需 MA_BIG(240)+SLOPE_LOOKBACK(20)=260 根（約 13 個月），
# 取 16 個月（~300+ 交易日）留安全邊際（月含假日、暖機門檻才穩）。
TREND_BACKFILL_MONTHS = 16
# 還原股價：單日收盤變動 > 此比例＝公司行為缺口（真相在 strategylib.adjust.DEFAULT_GAP_PCT）
TREND_ADJUST_GAP_PCT = _ADJUST_GAP_PCT

# === Verification (Layer 4) ===
VERIFY_FLAT_BAND_PCT = 0.3          # |漲跌幅| <= 0.3% 視為「平」

# === 盤前判斷有效性統計（Layer 4b，call_stats）===
# 用既有驗證資料回答「盤前判斷有沒有 edge」：基準線對照、信心校準、單因子表、判斷回測。
# 純離線唯讀，不改門檻、不寫 DB；調整這裡的常數不影響任何判斷與驗證結果，不必 bump 版本。
STATS_MIN_SAMPLE = 30     # 整體驗證樣本低於此數 → 結論標「樣本不足」（n=30 時 95% CI 仍寬達 ±18pp）
STATS_MIN_BUCKET = 10     # 分桶（信心/因子）樣本低於此數 → 該桶標「樣本不足」、不參與單調性判定
# 判斷回測每趟來回成本（bps）：以 0050 ETF 口徑＝手續費 14.25×2 + 證交稅 10（ETF 0.1%）。
# 改用台指期/微台請自行換算（期貨來回約 1–3 bps + 交易稅 2/10 萬，遠低於此）。
STATS_BT_COST_BPS = 38.5

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

# === 契約 I/O（docs/contracts v0.2，ADR-0005／0006）===
# 盤前判斷 → 契約 target（Insight＋Trade Plan）。數值是 Playbook 參數的預設值，寫進每筆 target 的
# trade_plan；改任一條請 bump PREMARKET_PLAYBOOK["playbook_version"]（不同版本績效分開記）。
PREMARKET_TARGET_INSTRUMENT = "TAIFEX:TMF"   # 盤前判斷對應的可交易商品（微台）
PREMARKET_TARGET_VALID_FROM = "08:50"
PREMARKET_TARGET_VALID_UNTIL = "13:25"
TARGET_LOCK_DEADLINE = "08:55"               # 08:55 未鎖定的 target，tradewind 視為中性；之後才鎖只記 warning
# 失效條件：開盤 30 分內現貨跌破（偏多）／站上（偏空）前一交易日低點／高點
PREMARKET_INVALIDATION_UNTIL = "09:30"
PREMARKET_PLAYBOOK = {
    "playbook_id": "pullback_hold",
    "playbook_version": "p1",
    "params": dict(_PULLBACK_PARAMS),          # 預設全取自 strategylib（不另存一份）；要覆蓋就改這裡的鍵
    "max_entries_per_day": 1,
    "entry": {"window_from": "09:05", "window_to": "10:30",
              "trigger": "5分K 回測開盤區間下緣不破後收高", "chase_limit_pts": 40},
    "risk": {"r_pct": 1.0, "stop_pts": 30, "max_units": 10},
    "scaling": [{"at_r": 1.0, "exit_fraction": 0.5, "move_stop_to": "breakeven"}],
    "exit": {"runner_rule": "跌破前一根 5分K 低點出場", "force_flat_at": "13:25"},
    "no_trade": ["開盤跳空 > 1.5%", "09:00–09:05 不進", "11:30 後不新進"],
    "discretion_allowed": ["early_exit"],
}
# 個股趨勢訊號 → 契約 target（strategy_id=trend_signal，integration/trend_targets.py）。批次規則（幾批、注碼、
# 加碼冷卻、超買減碼、閘門假設）**與回測同一組 BACKTEST_***：回測跑的就是 targets 會發的動作；target 的
# rule_version＝"{趨勢 RULE_VERSION}-{BACKTEST_RULE_VERSION}"，改批次規則 bump BACKTEST_RULE_VERSION。
TREND_TARGET_VALID_FROM = "09:00"          # 訊號收盤後算，次一交易日此時起生效；valid_until 無（波段）
TREND_TARGET_INVALIDATION_PCT = 2.0        # 失效＝收盤跌破價值中樞（月線）此 % 以上；停損距離＝收盤到此價位
TREND_PLAYBOOK = {
    "playbook_id": "trend_pullback",
    "playbook_version": "p1",
    "params": dict(_TREND_PULLBACK_PARAMS),    # 預設全取自 strategylib；要覆蓋就改這裡的鍵
    "max_entries_per_day": 1,
    "entry": {"window_from": "09:00", "window_to": "13:00",
              "trigger": "回踩月線企穩（small_turn_up）後次日：進場窗口內第一根 1 分 K 收完以 close 成交", "chase_limit_pts": None},
    "risk": {"r_pct": 1.0, "stop_pts": None, "max_units": None},     # stop_pts／max_units 每筆由 builder 填
    "scaling": [{"at_r": 2.0, "exit_fraction": 0.33, "move_stop_to": "breakeven"}],
    "exit": {"runner_rule": "大勢離開 UP／失效 → 新 target weight=0（Playbook 不判趨勢）；停損跨日保留", "force_flat_at": None},
    "no_trade": ["內功閘門 FAIL", "收盤已在失效價位之下不開新倉"],
    "discretion_allowed": ["early_exit", "reduce"],
}
# === 即時行情串流（契約 README「串流與回放」；ADR-0005 ⑧）===
# MARKET_SOURCE：none（預設，/ws/bars 只送心跳）／replay（回放 JSONL，CLI `stream` 用）／shioaji（📌 STUB，本機驗證後才能用）
MARKET_SOURCE = os.environ.get("MARKET_SOURCE", "none")
STREAM_INSTRUMENTS = ["TAIFEX:TMF"]          # 訂閱哪些商品（instruments 主檔要有，時段歸屬用它的 sessions）
STREAM_HEARTBEAT_SECONDS = 5                 # 閒置多久送一筆 {"heartbeat": ts}（契約起始值 5）
STREAM_PERSIST_TICKS = False                 # tick 量大且有損（契約 ts 只到秒，同秒多筆互相覆蓋），預設只落地 1m
STREAM_FLUSH_SECONDS = 1.0                   # 時鐘驅動 flush 的間隔：沒有後續 tick 的分鐘也在結束時刻發出
STREAM_REPLAY_JSONL = os.environ.get("STREAM_REPLAY_JSONL", "")   # MARKET_SOURCE=replay 時 server 回放的 JSONL
STREAM_REPLAY_DELAY_SECONDS = float(os.environ.get("STREAM_REPLAY_DELAY", "0"))
# 串流演練（ReplaySource）落地的 DB：一律跟正式 DB 分開（bars 主鍵不含 source，寫進正式 DB 會蓋掉同一分鐘的真資料）。
# 要讓 /api/bars 讀演練資料：另開一個 server，PREMARKET_DB 指到這個檔。
REPLAY_DB_PATH = os.environ.get("REPLAY_DB", "data/replay.db")
SHIOAJI_API_KEY = os.environ.get("SHIOAJI_API_KEY", "")
SHIOAJI_SECRET = os.environ.get("SHIOAJI_SECRET", "")
# tradewind 回流的 inbox：{inbox}/events/{run_mode}/{date}.jsonl、{inbox}/account_snapshots/...
TRADEWIND_INBOX_DIR = os.environ.get("TRADEWIND_INBOX", "data/inbox")

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
