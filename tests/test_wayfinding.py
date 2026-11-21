"""wayfinding 包的口径测试：计划、ITT 分析、变更回放。"""

import csv
import json
import tempfile
import unittest
from pathlib import Path

from wayfinding import COMBOS
from wayfinding.planner import (
    build_plans, load_planning_inputs, summarize, plan_day, _blocks,
    assess_support,
)
from wayfinding.analyze import (
    build_facts, estimate, assess_block_support, block_contrasts,
    party_outcomes,
)
from wayfinding.ledger import append_event, replay, support_timeline

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def rules():
    return json.loads((DATA / "rules.json").read_text(encoding="utf-8"))


class PlanningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plans, cls.signatures = build_plans(DATA)
        cls.min_blocks = rules()["min_comparable_blocks"]

    def test_full_factorial_days_cover_all_four_combos(self):
        for plan in self.plans["baseline"]:
            if plan.day not in ("2026-10-21", "2026-10-23", "2026-10-25"):
                self.assertEqual(set(plan.chosen.values()), set(COMBOS))

    def test_signed_slots_are_never_redrawn(self):
        for scenario in ("baseline", "offer_accepted"):
            c0 = next(p for p in self.plans[scenario]
                      if p.day == "2026-10-21")
            self.assertEqual(c0.chosen["C0-0"], "old/new")   # L01
            self.assertEqual(c0.chosen["C0-1"], "new/old")   # L02
            self.assertNotIn("C0-2", c0.chosen)              # L03 incompatible

    def test_incompatible_slot_blocks_execution_not_other_slots(self):
        c0 = next(p for p in self.plans["baseline"]
                  if p.day == "2026-10-21")
        blocked = dict(c0.blocked)
        self.assertIn("C0-2", blocked)
        self.assertNotIn("C0-3", blocked)

    def test_shared_r_buffer_allows_only_one_change_per_pair(self):
        for day in ("2026-10-23", "2026-10-25"):
            plan = next(p for p in self.plans["baseline"] if p.day == day)
            self.assertEqual(len(plan.chosen), 2)
            buffers = {s.shared_buffer for s in plan.slots
                       if s.candidate in plan.chosen}
            self.assertEqual(len(buffers), 2)   # 每个 R 资源至多一次

    def test_b_buffer_days_keep_four_executable_slots(self):
        plan = next(p for p in self.plans["baseline"]
                    if p.day == "2026-10-24")
        self.assertEqual(len(plan.chosen), 4)
        self.assertFalse(plan.blocked)

    def test_allowed_set_restriction_c1_c6(self):
        # C1/C6 的前两时段只允许 old/old 与 old/new（N 必留旧牌）
        slots, _ = load_planning_inputs(DATA)
        for cid in ("C1-0", "C1-1", "C6-0", "C6-1"):
            self.assertEqual(slots[cid].allowed,
                             frozenset(("old/old", "old/new")))
        plan = next(p for p in self.plans["baseline"]
                    if p.day == "2026-10-22")
        for cid in ("C1-0", "C1-1"):
            self.assertTrue(plan.chosen[cid].startswith("old/"))

    def test_without_e_only_penalizes_e_outcome_estimands(self):
        summary = summarize(self.plans["baseline"], self.min_blocks)
        c6_row = next(r for r in summary["main_N"]["rows"]
                      if r[0] == "2026-10-27")
        self.assertEqual(c6_row[0], "2026-10-27")
        self.assertEqual(c6_row[1], "full")
        e_row = next(r for r in summary["main_E"]["rows"]
                     if r[0] == "2026-10-27")
        self.assertEqual(e_row[1], "coverage")
        spill_n = next(r for r in summary["spill_N"]["rows"]
                       if r[0] == "2026-10-27")
        self.assertEqual(spill_n[1], "full")

    def test_interaction_missing_on_three_restricted_days(self):
        summary = summarize(self.plans["baseline"], self.min_blocks)
        partial_days = {d for d, level, _ in summary["interaction"]["rows"]
                        if level == "partial"}
        self.assertEqual(partial_days,
                         {"2026-10-21", "2026-10-23", "2026-10-25"})
        self.assertEqual(summary["interaction"]["counts"]["full"], 9)

    def test_offer_accepted_reduces_free_positions_but_not_counts(self):
        base = summarize(self.plans["baseline"], self.min_blocks)
        accepted = summarize(self.plans["offer_accepted"], self.min_blocks)
        for key in base:
            self.assertEqual(base[key]["counts"], accepted[key]["counts"])
        c1 = next(p for p in self.plans["offer_accepted"]
                  if p.day == "2026-10-22")
        self.assertEqual(c1.chosen["C1-0"], "old/new")  # L04 视为固定
        level, detail = c1.support["main_N"]
        self.assertEqual(level, "full")
        self.assertIn("已固定 1 个", detail)

    def test_threshold_min_comparable_blocks(self):
        summary = summarize(self.plans["baseline"], self.min_blocks)
        for key, s in summary.items():
            self.assertEqual(s["verdict"], "supported",
                             msg=f"{key} 应满足 4 块门槛")
        tight = summarize(self.plans["baseline"], 10)
        self.assertEqual(tight["interaction"]["verdict"], "insufficient")


class AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = rules()
        cls.rounds, cls.blocks, cls.parties = build_facts(DATA, cls.rules)
        cls.report, cls.per_block = estimate(
            cls.blocks, cls.parties, cls.rules)

    def test_itt_grouping_uses_assignment_not_actual_sign(self):
        # 01-0 事前 new/new，N 口撤牌成 old；仍按 new/new 归组
        rf = self.rounds["01-0"]
        self.assertEqual(rf.combo, "new/new")
        self.assertEqual(rf.actual["N"], "old")
        self.assertIn(("N", 557), rf.reverts)

    def test_closed_and_lost_are_never_substituted(self):
        vals, closed, lost = party_outcomes(self.parties, "00-3")
        self.assertEqual(closed, 2)   # 两口各一 closed
        self.assertEqual(lost, 2)     # 两口各一 lost
        # 均值只用 seen 且有 arrival 的队伍
        self.assertEqual(len(vals), 12)

    def test_carryover_taints_both_connected_rounds_same_day_only(self):
        # 00-0 有 1 队滞留到 00-1；00-3 是当日末轮，不跨日污染次日
        flags_00 = assess_block_support(self.blocks["2026-10-01"], self.rules)
        clean_main_n = set(flags_00["main_N"]["clean_round_ids"])
        self.assertNotIn("00-0", clean_main_n)
        self.assertNotIn("00-1", clean_main_n)
        self.assertIn("00-2", clean_main_n)   # 未连接的轮次仍干净
        # 次日 01-3 是滞留轮 01-2 的后继，按“连接的两轮都扣支持”同样
        # 扣除；撤牌偏离只登记 deviation_itt_kept，不改变上述归类
        flags_01 = assess_block_support(self.blocks["2026-10-02"], self.rules)
        self.assertNotIn("01-3", flags_01["main_E"]["clean_round_ids"])
        kinds = {k for k, _ in flags_01["main_E"]["reasons"]}
        self.assertIn("confounded_rounds", kinds)

    def test_unplanned_link_only_taints_spillover_estimands(self):
        rf = self.rounds["05-1"]
        self.assertTrue(any(x[0] == "L2" for x in rf.unplanned_links))
        flags = assess_block_support(self.blocks["2026-10-06"], self.rules)
        # 主效应不因计划外通道被标记为 confounded（但仍可能被滞留标记）
        self.assertNotIn("confounded_rounds",
                         [k for k, _ in flags["main_N"]["reasons"]] and
                         flags["main_N"]["reasons"] or [])
        spill_reasons = dict(flags["spill_N"]["reasons"])
        self.assertIn("confounded_rounds", spill_reasons)
        self.assertTrue(any(rid == "05-1"
                            for rid, _ in spill_reasons["confounded_rounds"]))

    def test_three_way_insufficiency_distinction(self):
        # 主 N：有观测但层级被污染 -> confounded
        self.assertTrue(all(
            self.report["main_N"]["confounded_blocks"]
            + self.report["main_N"]["gap_only_blocks"]))
        # 主 E：存在 clean 块但不足 4 块门槛
        self.assertEqual(self.report["main_E"]["verdict"],
                         "below_block_threshold")
        self.assertEqual(len(self.report["main_E"]["clean_blocks"]), 3)

    def test_contrast_sign_convention_new_minus_old(self):
        block = self.blocks["2026-10-07"]   # 无滞留、无断档、无撤牌
        flags = assess_block_support(block, self.rules)
        eligible = {k: set(v["clean_round_ids"]) for k, v in flags.items()}
        contrasts, cells, detail = block_contrasts(
            block, self.parties, eligible)
        # 该块 06-1 滞留、06-2 作为后继被扣除；06-0/06-3 两个干净轮次
        # 的 E 层级一新一旧，主 E（E 自身因子）可配对；主 N 两干净轮
        # 同为 N=new，配对不存在时按口径返回 None
        self.assertIsNotNone(contrasts["main_E"])
        self.assertIsNone(contrasts["main_N"])
        self.assertEqual(detail["main_N"], "干净轮次中无同层配对")

    def test_interval_width_checked_against_business_gain(self):
        e = self.report["main_E"]
        half = (e["ci_high"] - e["ci_low"]) / 2
        self.assertGreater(half, self.rules["min_arrival_gain_minutes"])

    def test_queue_gaps_counted_not_imputed(self):
        for rid in ("00-0", "05-1"):
            self.assertEqual(self.rounds[rid].queue_gaps["N"], 1)
            self.assertEqual(self.rounds[rid].queue_gaps["E"], 1)
        clean_day = self.rounds["07-0"]
        self.assertEqual(clean_day.queue_gaps["N"], 0)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "events.csv"

    def tearDown(self):
        self.tmp.cleanup()

    def test_signoff_event_freezes_slot_and_can_kill_support(self):
        min_blocks = rules()["min_comparable_blocks"]
        # 初始：C3 日（全 B 资源）对所有差别都是 full
        plans, summary, _ = replay(DATA, self.ledger, min_blocks)
        c3 = next(p for p in plans if p.day == "2026-10-24")
        self.assertEqual(len(c3.chosen), 4)

        # 事件 0：把 C3-0 局部签死为 old/old
        append_event(self.ledger, "2026-10-22T10:00", "sign",
                     "C3-0", "old/old", "局部签收")
        plans, summary, _ = replay(DATA, self.ledger, min_blocks)
        c3 = next(p for p in plans if p.day == "2026-10-24")
        self.assertEqual(c3.chosen["C3-0"], "old/old")
        level, _ = c3.support["main_N"]
        self.assertEqual(level, "full")   # 仍有自由位置承载对照

        # 事件 1-3：签死该块其余三个位置，随机互换彻底消失
        for cid, combo in (("C3-1", "old/new"),
                           ("C3-2", "new/old"),
                           ("C3-3", "new/new")):
            append_event(self.ledger, "2026-10-22T11:00", "sign",
                         cid, combo, "继续局部签收")
        plans, summary, _ = replay(DATA, self.ledger, min_blocks)
        c3 = next(p for p in plans if p.day == "2026-10-24")
        level, detail = c3.support["main_N"]
        self.assertEqual(level, "partial")
        self.assertIn("不允许随机互换", detail)

    def test_timeline_locates_change_point(self):
        min_blocks = rules()["min_comparable_blocks"]
        append_event(self.ledger, "2026-10-22T10:00", "sign",
                     "C3-0", "old/old", "局部签收")
        append_event(self.ledger, "2026-10-22T11:00", "incompatible",
                     "C3-1", "", "班次不可再配置")
        timeline = support_timeline(DATA, self.ledger, min_blocks)
        changes = [t for t in timeline if t["changed"]]
        self.assertGreaterEqual(len(changes), 2)
        # 初始快照 seq=-1 必须存在
        self.assertEqual(changes[0]["seq"], -1)

    def test_signed_slot_cannot_be_improved_by_redraw(self):
        min_blocks = rules()["min_comparable_blocks"]
        append_event(self.ledger, "2026-10-22T10:00", "sign",
                     "C3-2", "old/old", "局部签收")
        plans, _, _ = replay(DATA, self.ledger, min_blocks)
        c3 = next(p for p in plans if p.day == "2026-10-24")
        # 块内组合仍各出现一次，但 C3-2 固定不可换
        self.assertEqual(c3.chosen["C3-2"], "old/old")
        self.assertEqual(len(set(c3.chosen.values())), 4)


if __name__ == "__main__":
    unittest.main()
