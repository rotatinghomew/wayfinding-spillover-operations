"""下一轮轮换计划器。

输入：data/next_round_constraints.csv（场地方允许轮换的时段、必留旧牌、
共用隔离窗口、最低观察覆盖）与 data/next_round_signatures.csv
（志愿者班次的签认/出借/不可配置记录）。

设计口径（rules.json）：
- 同一天的四个时段是一整个分配块；四类组合各出现一次才是完整的 2x2
  交互块。估计单位是“独立轮换块”，入口样本量不能替代轮换块数。
- 已签认（signed）安排固定不重抽；incompatible 班次不可再配置；
  offered 是尚未签认的出借，计划器同时给出接受/拒绝两套口径。
- shared_buffer：R 前缀为同日两个时段共用、一次只能占用一次的隔离
  资源，引用同一 R 标识的两个时段至多一个执行换牌；B 前缀各时段独有。
- minimum_coverage=without_E 的时段 E 口不具备完整观察覆盖，E 侧结果
  的差别在该时段不可评估（安排仍可发出）。

对每一种要估计的差别，逐块报告：
  full      块内具备该差别所需的全部因子层级/组合，且相关位置允许随机互换；
  partial   只有部分层级（可观察但不足以识别该差别）或位置已被固定；
  absent    排期本身不提供该差别所需的层级；
  coverage  组合具备但结果观察覆盖不足（without_E 等）。
"""

from dataclasses import dataclass, field
import csv
from pathlib import Path

from . import COMBOS

DATA = Path(__file__).resolve().parent.parent / "data"

RANK = {"full": 3, "coverage": 2, "partial": 1, "absent": 0}


@dataclass
class Slot:
    candidate: str
    day: str
    shift: str
    start: int
    end: int
    allowed: frozenset
    shared_buffer: str
    guide_hours: int
    minimum_coverage: str
    fixed_combo: str | None = None          # signed 固定组合
    unavailable: bool = False               # incompatible
    offer_combo: str | None = None          # offered 待签组合
    note: str = ""

    @property
    def position(self):
        return self.candidate.split("-")[1]


@dataclass
class BlockPlan:
    day: str
    slots: list
    chosen: dict                            # candidate -> combo
    blocked: list                           # 无法执行换牌的时段及原因
    scenario: str                           # baseline | offer_accepted
    feasible: bool
    reasons: list = field(default_factory=list)
    support: dict = field(default_factory=dict)
    candidate_count: int = 0


@dataclass
class Estimand:
    key: str
    title: str
    factor: str                             # 'N' | 'E' | 'both'
    outcome_gate: str
    full_factorial: bool                    # 交互需要四类组合同块


ESTIMANDS = (
    Estimand("main_N", "N 口主效应（N 自己换牌的效果）", "N", "N", False),
    Estimand("main_E", "E 口主效应（E 自己换牌的效果）", "E", "E", False),
    Estimand("spill_N", "外溢到 N（E 换牌对 N 口队伍的影响）", "E", "N", False),
    Estimand("spill_E", "外溢到 E（N 换牌对 E 口队伍的影响）", "N", "E", False),
    Estimand("interaction", "交互（两口同时换牌与分别换牌的差别）",
             "both", "N", True),
)


def _parse_allowed(text):
    return frozenset(text.split("+")) if text else frozenset()


def _is_shared_buffer(ref):
    return ref.startswith("R")


def load_planning_inputs(data_dir=DATA):
    with (data_dir / "next_round_constraints.csv").open(
            encoding="utf-8", newline="") as f:
        constraint_rows = list(csv.DictReader(f))
    with (data_dir / "next_round_signatures.csv").open(
            encoding="utf-8", newline="") as f:
        signature_rows = list(csv.DictReader(f))

    slots = {}
    for row in constraint_rows:
        slots[row["candidate"]] = Slot(
            candidate=row["candidate"],
            day=row["day"],
            shift=row["shift"],
            start=int(row["start"]),
            end=int(row["end"]),
            allowed=_parse_allowed(row["allowed"]),
            shared_buffer=row["shared_buffer"],
            guide_hours=int(row["guide_hours"]),
            minimum_coverage=row["minimum_coverage"],
        )
    for row in signature_rows:
        slot = slots.get(row["candidate"])
        if slot is None:
            raise ValueError(f"签认记录引用了未知时段 {row['candidate']}")
        state = row["state"]
        if state == "signed":
            slot.fixed_combo = row["combo"]
            slot.note = f"signed {row['record']} ({row['effective']})"
        elif state == "incompatible":
            slot.unavailable = True
            slot.note = f"incompatible {row['record']} ({row['effective']})"
        elif state == "offered":
            slot.offer_combo = row["combo"]
            slot.note = f"offered {row['record']} ({row['effective']})"
        else:
            raise ValueError(f"未知签认状态 {state}")
    return slots, signature_rows


def _blocks(slots):
    days = {}
    for slot in slots.values():
        days.setdefault(slot.day, []).append(slot)
    for day in days:
        days[day].sort(key=lambda s: s.position)
    return days


def _executable_subsets(day_slots, fixed_map, inactive):
    """在共用隔离窗口单占用约束下，枚举可执行换牌的时段子集。

    固定（已签认）时段必须保留；incompatible 时段一律排除。
    返回子集列表（按可执行时段数降序）。
    """
    shared = {}
    for s in day_slots:
        if _is_shared_buffer(s.shared_buffer):
            shared.setdefault(s.shared_buffer, []).append(s)

    must = {s.candidate for s in day_slots
            if s.candidate in fixed_map and s.candidate not in inactive}
    options = []
    # 每个共用资源二选一（或都不选）；独有 B 资源的时段总是可执行
    grouped = []
    free_ungrouped = []
    for s in day_slots:
        if s.candidate in inactive:
            continue
        if s.shared_buffer in shared:
            continue
        free_ungrouped.append(s.candidate)
    for ref, members in shared.items():
        choices = [s.candidate for s in members
                   if s.candidate not in inactive]
        grouped.append((ref, choices))

    def walk(i, picked):
        if i == len(grouped):
            options.append(set(picked) | set(free_ungrouped) | must)
            return
        ref, choices = grouped[i]
        forced = [c for c in choices if c in must]
        if forced:
            if len(forced) > 1:
                return  # 同一共用资源上两个固定时段，硬冲突
            walk(i + 1, picked | {forced[0]})
            return
        walk(i + 1, picked)                 # 该资源本轮不占用
        for c in choices:
            walk(i + 1, picked | {c})

    walk(0, set())
    # 去重并按规模降序，优先发出时段最多的整张单
    unique = sorted({frozenset(o) for o in options},
                    key=lambda o: -len(o))
    return [set(o) for o in unique]


def _assign_combos(day_slots, executable, fixed_map):
    """给可执行时段分配互不相同的组合，满足 allowed 与固定要求。"""
    active = [s for s in day_slots if s.candidate in executable]
    reasons = []
    for s in active:
        combo = fixed_map.get(s.candidate)
        if combo is not None and combo not in s.allowed:
            reasons.append(
                f"{s.candidate}: 已固定 {combo} 不在允许集合 "
                f"{'/'.join(sorted(s.allowed))}")
    if reasons:
        return None, reasons

    ordered = sorted(active, key=lambda s: (
        s.candidate not in fixed_map, s.candidate))  # 固定的先放
    results = []

    def walk(i, chosen, used):
        if i == len(ordered):
            results.append(dict(chosen))
            return
        slot = ordered[i]
        if slot.candidate in fixed_map:
            options = [fixed_map[slot.candidate]]
        else:
            options = sorted(slot.allowed - set(used))
        for combo in options:
            chosen[slot.candidate] = combo
            walk(i + 1, chosen, used + [combo])
            del chosen[slot.candidate]

    walk(0, {}, [])
    if not results:
        reasons.append("允许组合集合内无法为可执行时段分配互不相同的组合")
        return None, reasons
    # 优先覆盖四类组合、其次让自由位置尽量多承载不同层级
    results.sort(key=lambda m: (-len(set(m.values())),
                                tuple(sorted(m))))
    return results, reasons


def plan_day(day, day_slots, scenario):
    """生成单日 BlockPlan。scenario: baseline | offer_accepted。"""
    fixed_map = {s.candidate: s.fixed_combo for s in day_slots
                 if s.fixed_combo is not None}
    inactive = {s.candidate for s in day_slots if s.unavailable}
    if scenario == "offer_accepted":
        for s in day_slots:
            if s.offer_combo is not None:
                fixed_map[s.candidate] = s.offer_combo

    subsets = _executable_subsets(day_slots, fixed_map, inactive)
    best = None
    all_reasons = []
    blocked = []
    for subset in subsets:
        sheets, reasons = _assign_combos(day_slots, subset, fixed_map)
        if sheets:
            best = (subset, sheets)
            break
        all_reasons.extend(reasons)
    if best is None:
        plan = BlockPlan(day=day, slots=day_slots, chosen={},
                         blocked=[(s.candidate, s.note or "无可用隔离资源")
                                  for s in day_slots],
                         scenario=scenario, feasible=False,
                         reasons=all_reasons or ["无可行排法"])
        plan.support = assess_support(plan, fixed_map, inactive)
        return plan

    subset, sheets = best
    chosen = sheets[0]
    for s in day_slots:
        if s.candidate in inactive:
            blocked.append((s.candidate, s.note or "班次 incompatible"))
        elif s.candidate not in subset:
            blocked.append((s.candidate,
                            f"共用隔离窗口 {s.shared_buffer} 单占用，本时段不换牌"))
    plan = BlockPlan(day=day, slots=day_slots, chosen=chosen,
                     blocked=blocked, scenario=scenario, feasible=True,
                     candidate_count=len(sheets))
    plan.support = assess_support(plan, fixed_map, inactive)
    return plan


def _levels_present(combos, factor):
    """已排组合在指定因子（N/E/both）上覆盖到的层级。"""
    if factor == "both":
        return frozenset(combos)
    idx = 0 if factor == "N" else 1
    return frozenset(c.split("/")[idx] for c in combos)


def assess_support(plan, fixed_map, inactive):
    """逐差别判断该块是否仍有足够且允许随机互换的组合。"""
    out = {}
    chosen = plan.chosen
    placed = frozenset(chosen.values())
    free_positions = [c for c in chosen if c not in fixed_map]
    fixed_positions = [c for c in chosen if c in fixed_map]
    slot_by_id = {s.candidate: s for s in plan.slots}
    no_e_active = {s.candidate for s in plan.slots
                   if s.minimum_coverage == "without_E"
                   and s.candidate in chosen}

    for estimand in ESTIMANDS:
        key = estimand.key
        if not chosen:
            out[key] = ("absent", "该日无可行轮换单")
            continue
        if estimand.full_factorial:
            missing = frozenset(COMBOS) - placed
            if missing:
                out[key] = ("partial",
                            f"交互需四类组合同块，缺少 "
                            f"{'/'.join(sorted(missing))}（本块仅排 "
                            f"{len(placed)} 个可执行时段）")
                continue
        else:
            present = _levels_present(placed, estimand.factor)
            if len(present) < 2:
                out[key] = ("absent",
                            f"因子 {estimand.factor} 只有层级 "
                            f"{'/'.join(sorted(present))}，无对照层级")
                continue
        # 层级齐备：再看随机互换与观察覆盖
        if not free_positions:
            out[key] = ("partial",
                        "所需层级齐备但位置全部已固定，不允许随机互换")
            continue
        coverage_gap = (estimand.outcome_gate == "E" and bool(no_e_active))
        if coverage_gap:
            full_cov = [c for c in chosen if c not in no_e_active]
            out[key] = ("coverage",
                        "层级齐备但 without_E 时段 E 口结果无完整观察覆盖"
                        + (f"；full 覆盖时段: {'/'.join(sorted(full_cov))}"
                           if full_cov else "；本块无 full 覆盖时段"))
            continue
        detail = f"所需层级齐备，自由位置 {len(free_positions)} 个"
        if fixed_positions:
            detail += f"，已固定 {len(fixed_positions)} 个（不重抽）"
        out[key] = ("full", detail)
    return out


def build_plans(data_dir=DATA):
    """两个场景各生成一张完整轮换单及逐差别支持结论。"""
    slots, signatures = load_planning_inputs(data_dir)
    days = _blocks(slots)
    result = {}
    for scenario in ("baseline", "offer_accepted"):
        result[scenario] = [plan_day(day, days[day], scenario)
                            for day in sorted(days)]
    return result, signatures


def summarize(plans, min_comparable_blocks):
    """按差别汇总块级支持，并对照事先业务门槛给出结论。"""
    summary = {}
    for estimand in ESTIMANDS:
        rows = []
        for plan in plans:
            level, detail = plan.support[estimand.key]
            rows.append((plan.day, level, detail))
        counts = {k: 0 for k in RANK}
        for _, level, _ in rows:
            counts[level] += 1
        full = counts["full"]
        summary[estimand.key] = {
            "title": estimand.title,
            "counts": counts,
            "full_blocks": full,
            "required": min_comparable_blocks,
            "verdict": "supported" if full >= min_comparable_blocks
                       else "insufficient",
            "rows": rows,
        }
    return summary
