"""盤前判斷測試：兩票合成、信心調整、夾限、各維度級別與效果、讀取與寫入。"""

import json
import sqlite3

from config import settings
from db.schema import create_all_tables
from integration.premarket_call import (
    SKIP_ALIGNED,
    SKIP_DIRECTION_NEUTRAL,
    SKIP_FUTURES_VOTE_NEUTRAL,
    SKIP_FX_VOTE_NEUTRAL,
    DailyMetrics,
    compute_premarket_call,
    evaluate_premarket_call,
    load_daily_metrics,
)

_KRW_DIVERGENCE = {"TWD": "bearish", "CNY": "bearish", "KRW": "bullish"}


def _call(fx_direction="neutral", fx_asia_sync=None, asia_detail=None,
          spread_adjusted=None, volume_ratio=None, oi_net=None):
    return evaluate_premarket_call(DailyMetrics(
        date="2026-06-12", fx_direction=fx_direction, fx_asia_sync=fx_asia_sync,
        fx_asia_detail=asia_detail, futures_spread_adjusted=spread_adjusted,
        futures_volume_ratio=volume_ratio, oi_net_foreign=oi_net,
    ))


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    return conn


class TestVoteSynthesis:
    def test_both_bullish(self):
        """兩票同向偏多 → bullish 信心 3。"""
        c = _call(fx_direction="bullish", spread_adjusted=150)
        assert (c.direction, c.confidence) == ("bullish", 3)

    def test_both_bearish(self):
        c = _call(fx_direction="bearish", spread_adjusted=-150)
        assert (c.direction, c.confidence) == ("bearish", 3)

    def test_conflict_forces_neutral(self):
        """兩票反向 → 強制中性，信心 1。"""
        c = _call(fx_direction="bullish", spread_adjusted=-150)
        assert (c.direction, c.confidence) == ("neutral", 1)
        assert any("分歧" in r for r in c.reasons)

    def test_one_directional_one_neutral(self):
        """一票方向一票中性 → 該方向，信心 2。"""
        c = _call(fx_direction="bullish", spread_adjusted=50)
        assert (c.direction, c.confidence) == ("bullish", 2)

    def test_both_neutral(self):
        c = _call(fx_direction="neutral", spread_adjusted=20)
        assert (c.direction, c.confidence) == ("neutral", 2)

    def test_spread_exactly_at_threshold(self):
        """價差剛好 +100 → 偏多票（含邊界）。"""
        assert _call(spread_adjusted=100).futures_vote == "bullish"

    def test_missing_fx_treated_neutral(self):
        """匯率資料缺失 → 匯率票中性，理由有記錄。"""
        c = _call(fx_direction=None, spread_adjusted=150)
        assert (c.direction, c.confidence) == ("bullish", 2)
        assert any("匯率資料不可用" in r for r in c.reasons)

    def test_rule_version(self):
        assert _call().rule_version == settings.SIGNAL_RULE_VERSION


class TestAdjustments:
    def test_asia_sync_aligned_plus_one(self):
        """亞幣同步且與方向同向 → +1。"""
        c = _call(fx_direction="bullish", fx_asia_sync=1, spread_adjusted=150)
        assert c.confidence == 4
        assert (c.asia_sync.level, c.asia_sync.effect) == ("sync", 1)

    def test_twd_only_minus_one(self):
        """只有台幣動（不同步）→ -1。"""
        c = _call(fx_direction="bullish", fx_asia_sync=0, spread_adjusted=150)
        assert c.confidence == 2
        assert (c.asia_sync.level, c.asia_sync.effect) == ("diverge", -1)

    def test_asia_diverge_with_neutral_fx_not_counted(self):
        """亞幣分歧但匯率票中性 → 不調整，原因：匯率票中性。"""
        c = _call(fx_direction="neutral", fx_asia_sync=0, spread_adjusted=150)
        assert c.confidence == 2
        assert c.asia_sync.effect == 0
        assert c.asia_sync.skip == SKIP_FX_VOTE_NEUTRAL

    def test_asia_sync_with_conflict_not_counted(self):
        """亞幣同步但兩票分歧（方向中性）→ 不調整，原因：方向中性。"""
        c = _call(fx_direction="bullish", fx_asia_sync=1, spread_adjusted=-150)
        assert c.asia_sync.skip == SKIP_DIRECTION_NEUTRAL

    def test_volume_high_plus_one(self):
        """量比 >= 1.5 且期貨有方向 → +1。"""
        c = _call(fx_direction="bullish", spread_adjusted=150, volume_ratio=1.6)
        assert c.confidence == 4
        assert (c.night_volume.level, c.night_volume.effect) == ("high", 1)

    def test_volume_high_without_futures_direction_no_bonus(self):
        """量比高但期貨票中性 → 不加分，級別仍是爆量。"""
        c = _call(fx_direction="bullish", spread_adjusted=50, volume_ratio=1.6)
        assert c.confidence == 2
        assert c.night_volume.level == "high"
        assert c.night_volume.skip == SKIP_FUTURES_VOTE_NEUTRAL

    def test_volume_low_minus_one(self):
        """量比 <= 0.7 → -1。"""
        c = _call(fx_direction="bullish", spread_adjusted=150, volume_ratio=0.6)
        assert c.confidence == 2
        assert (c.night_volume.level, c.night_volume.effect) == ("low", -1)

    def test_volume_normal(self):
        c = _call(volume_ratio=1.0)
        assert (c.night_volume.level, c.night_volume.effect) == ("normal", 0)

    def test_oi_against_bullish_minus_one(self):
        """偏多但外資淨空單超過 3 萬口 → -1。"""
        c = _call(fx_direction="bullish", spread_adjusted=150, oi_net=-34800)
        assert c.confidence == 2
        assert (c.foreign_oi.level, c.foreign_oi.effect) == ("net_short", -1)
        assert "外資淨空單 -34,800 口仍偏空 -1" in c.reasons

    def test_oi_against_bearish_minus_one(self):
        c = _call(fx_direction="bearish", spread_adjusted=-150, oi_net=35000)
        assert c.confidence == 2
        assert "外資淨多單 35,000 口仍偏多 -1" in c.reasons

    def test_oi_aligned_not_counted(self):
        """外資淨空與偏空同向 → 不調整（籌碼沒有票），原因：同向。"""
        c = _call(fx_direction="bearish", spread_adjusted=-150, oi_net=-40000)
        assert c.confidence == 3
        assert c.foreign_oi.level == "net_short"
        assert c.foreign_oi.skip == SKIP_ALIGNED

    def test_oi_with_neutral_direction_not_counted(self):
        c = _call(oi_net=-40000)
        assert c.foreign_oi.skip == SKIP_DIRECTION_NEUTRAL

    def test_oi_small_no_penalty(self):
        """空單未達 3 萬口 → 部位中性、不扣分。"""
        c = _call(fx_direction="bullish", spread_adjusted=150, oi_net=-20000)
        assert c.confidence == 3
        assert c.foreign_oi.level == "neutral"

    def test_krw_divergence_minus_one(self):
        """台幣貶人民幣貶但韓元升 → -1 + 警示理由。"""
        c = _call(fx_direction="bearish", spread_adjusted=-150,
                  asia_detail=_KRW_DIVERGENCE)
        assert c.confidence == 2
        assert (c.krw_divergence.level, c.krw_divergence.effect) == ("divergence", -1)
        assert any("賣台買韓" in r for r in c.reasons)

    def test_missing_data_has_no_level(self):
        c = _call()
        for adj in (c.asia_sync, c.night_volume, c.foreign_oi, c.krw_divergence):
            assert adj.level is None and adj.effect == 0

    def test_confidence_clamped_to_max(self):
        """所有加分項同時成立也不超過 5。"""
        c = _call(fx_direction="bullish", fx_asia_sync=1, spread_adjusted=200,
                  volume_ratio=2.0, oi_net=10000)
        assert c.confidence == 5

    def test_confidence_clamped_to_min(self):
        """扣到底也不低於 1。"""
        c = _call(fx_direction="bullish", fx_asia_sync=0, spread_adjusted=150,
                  volume_ratio=0.5, oi_net=-40000)
        assert c.confidence == 1


def _insert_metrics(conn, date="2026-06-12", **cols):
    cols = {"fx_direction": "bullish", "futures_spread_adjusted": 150, **cols}
    names = ", ".join(["date", *cols])
    marks = ", ".join("?" * (len(cols) + 1))
    conn.execute(f"INSERT INTO daily_metrics ({names}) VALUES ({marks})",
                 (date, *cols.values()))
    conn.commit()


class TestLoadAndPersist:
    def test_load_parses_asia_detail(self):
        conn = _make_db()
        _insert_metrics(conn, fx_asia_detail=json.dumps(_KRW_DIVERGENCE), oi_delta=-500)
        m = load_daily_metrics("2026-06-12", conn)
        assert m.fx_asia_detail == _KRW_DIVERGENCE
        assert m.oi_delta == -500

    def test_load_missing_returns_none(self):
        assert load_daily_metrics("2026-06-12", _make_db()) is None

    def test_no_metrics_returns_none(self):
        assert compute_premarket_call("2026-06-12", _make_db()) is None

    def test_written_to_signals_table(self):
        conn = _make_db()
        _insert_metrics(conn)
        compute_premarket_call("2026-06-12", conn)
        row = conn.execute(
            "SELECT direction, confidence, rule_version, reasons "
            "FROM signals WHERE date = '2026-06-12'"
        ).fetchone()
        assert row[0] == "bullish"
        assert row[1] == 3
        assert row[2] == settings.SIGNAL_RULE_VERSION
        assert json.loads(row[3]) == ["匯率與期貨同向"]

    def test_idempotent_recompute(self):
        """重算同一天覆蓋舊值。"""
        conn = _make_db()
        _insert_metrics(conn)
        compute_premarket_call("2026-06-12", conn)
        conn.execute("UPDATE daily_metrics SET fx_direction = 'neutral'")
        compute_premarket_call("2026-06-12", conn)
        rows = conn.execute("SELECT direction, confidence FROM signals").fetchall()
        assert rows == [("bullish", 2)]  # 期貨票仍偏多，僅匯率轉中性
