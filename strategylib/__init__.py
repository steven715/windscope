"""strategylib：windscope 與 tradewind 共用的純函式策略庫（ADR-0005 §3、ADR-0007）。

規則：本套件只能 import 標準庫與自己（不碰 settings、db、網路），所有函式是
`f(資料, 參數) -> 結果` 的純函式。回測（windscope）與交易引擎（tradewind）import 同一份，
「回測跑 production 邏輯」才成立。版本見 __version__；門檻改了就 bump 對應模組的 RULE_VERSION。
"""

__version__ = "0.1.0"
