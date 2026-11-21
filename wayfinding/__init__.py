"""指路牌轮换：下一轮排期计划与历史证据分析。

本包只使用 Python 标准库；所有业务口径见 data/rules.json 与 README。
"""

GATES = ("N", "E")
LEVELS = ("old", "new")
# 四类 2x2 组合，顺序即（N 牌, E 牌）
COMBOS = ("old/old", "old/new", "new/old", "new/new")
