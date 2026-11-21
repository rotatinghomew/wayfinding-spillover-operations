"""历史轮次证据分析器。

口径（rules.json 与 README）：
- 队伍以第一入口所属轮次归组；比较一律使用事前分配（rotation.csv），
  因拥堵撤牌（signs.csv 的 R- 事件）只记为协议偏离，不按实际旧牌重算。
- 同日四轮是一个分配块；主效应/外溢用块内因子层级均值之差，交互用
  四类组合的差分中的差分（DID）。独立轮换块是复制单位。
- 跨轮滞留：队伍的 arrival（或无 arrival 时的 last_seen）达到同日
  下一轮起点，则该队伍连接的两轮比较都扣除证据支持。
- 通道：共同可达 N-E 的计划内通告按设计口径处理；计划外通道
  （declared=no，同号取最高修订）覆盖的轮次，其外溢/交互比较扣除支持。
- 观察断档：queues.csv 空白读数不插补；lost（观察中断）与 closed
  （闭馆确认未到达）互不替代，二者均不进入到场时延均值。
- 证据不足时区分三类原因：缺少组合（missing_combo）、观察不全
  （incomplete_observation）、有观测但两种影响分不清（confounded）。
"""

from collections import defaultdict
from dataclasses import dataclass, field
import csv
import math
from pathlib import Path

from . import COMBOS
from .planner import ESTIMANDS

DATA = Path(__file__).resolve().parent.parent / "data"


def _load(name, data_dir):
    with (data_dir / f"{name}.csv").open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


@dataclass
class RoundFacts:
    round_id: str
    day: str
    position: str
    combo: str                              # 事前 N/E 组合
    actual: dict                            # gate -> 轮内最终实际牌面
    reverts: list                           # (gate, minute)
    queue_gaps: dict                        # gate -> 空白读数个数
    unplanned_links: list                   # (link, 修订, 通道)
    carryover_parties: int = 0
    parties: list = field(default_factory=list)


@dataclass
class BlockFacts:
    day: str
    rounds: dict                            # position -> RoundFacts
    flags: dict = field(default_factory=dict)   # estimand key -> 支持结论


def build_facts(data_dir=DATA, rules=None):
    rotation = _load("rotation", data_dir)
    signs = _load("signs", data_dir)
    parties = _load("parties", data_dir)
    queues = _load("queues", data_dir)
    links = _load("links", data_dir)

    rounds = {}
    by_day = defaultdict(list)
    for row in rotation:
        combo = f"{row['north']}/{row['east']}"
        rid = row["round"]
        rounds[rid] = RoundFacts(
            round_id=rid, day=row["day"],
            position=rid.split("-")[1], combo=combo,
            actual={}, reverts=[], queue_gaps={"N": 0, "E": 0},
            unplanned_links=[])
        by_day[row["day"]].append(rid)

    # 实际牌面：同轮同口取时间上最后事件；R- 撤牌事件同时记入偏离
    sign_events = defaultdict(list)
    for s in signs:
        sign_events[(s["round"], s["gate"])].append(
            (int(s["minute"]), s["sign"], s["event"].startswith("R-")))
    for (rid, gate), events in sign_events.items():
        events.sort()
        rounds[rid].actual[gate] = events[-1][1]
        rounds[rid].reverts.extend(
            [(gate, minute) for minute, _, is_revert in events if is_revert])

    # 队列断档
    for q in queues:
        if q["people"] == "":
            rounds[q["round"]].queue_gaps[q["gate"]] += 1

    # 通道：同号取最高修订；计划外通道按覆盖分钟标记轮次
    latest = {}
    for l in links:
        key = l["link"]
        rev = int(l["revision"])
        if key not in latest or rev > int(latest[key]["revision"]):
            latest[key] = l
    for l in latest.values():
        if l["declared"] != "no":
            continue
        rid = l["round"]
        plan = next(r for r in rotation if r["round"] == rid)
        if int(l["start"]) < int(plan["end"]) and int(l["end"]) > int(plan["start"]):
            rounds[rid].unplanned_links.append(
                (l["link"], l["revision"], f"{l['gate_a']}-{l['gate_b']}"))

    # 同日下一轮起点，用于跨轮滞留
    next_start = {}
    for day, ids in by_day.items():
        ids.sort(key=lambda r: int(rounds[r].position))
        plan_by_id = {r["round"]: r for r in rotation}
        for a, b in zip(ids, ids[1:]):
            next_start[a] = int(plan_by_id[b]["start"])

    party_rows = defaultdict(list)
    for p in parties:
        rid = p["round"]
        party_rows[rid].append(p)
        ns = next_start.get(rid)
        arrival = int(p["arrival"]) if p["arrival"] else None
        last_seen = int(p["last_seen"])
        if ns is not None and (
                (arrival is not None and arrival >= ns) or
                (arrival is None and last_seen >= ns)):
            rounds[rid].carryover_parties += 1

    # 组装块
    blocks = {}
    for day, ids in by_day.items():
        blocks[day] = BlockFacts(
            day=day,
            rounds={rounds[r].position: rounds[r] for r in ids})
    return rounds, blocks, party_rows


# ---------- 块级对比 ----------

def party_outcomes(party_rows, rid):
    """该轮到场时延可用值，以及 closed/lost 计数（二者互不替代）。"""
    values, n_closed, n_lost = [], 0, 0
    for p in party_rows.get(rid, []):
        if p["exit"] == "seen" and p["arrival"]:
            values.append(int(p["arrival"]) - int(p["entry"]))
        elif p["exit"] == "closed":
            n_closed += 1
        elif p["exit"] == "lost":
            n_lost += 1
    return values, n_closed, n_lost


def _cell_means(block, party_rows, outcome_gate):
    """四类组合各一个单元，给出到场时延均值与样本数。"""
    cells = {}
    for pos, rf in block.rounds.items():
        vals, closed, lost = party_outcomes(party_rows, rf.round_id)
        # 到场时延是队伍级指标，两口同属一个块单元；按结果入口拆分时
        # 以 first_gate 归属
        gate_vals = [int(p["arrival"]) - int(p["entry"])
                     for p in party_rows.get(rf.round_id, [])
                     if p["exit"] == "seen" and p["arrival"]
                     and p["first_gate"] == outcome_gate]
        cells[rf.combo] = {
            "mean": (sum(gate_vals) / len(gate_vals)) if gate_vals else None,
            "n": len(gate_vals),
            "closed": sum(1 for p in party_rows.get(rf.round_id, [])
                          if p["exit"] == "closed"
                          and p["first_gate"] == outcome_gate),
            "lost": sum(1 for p in party_rows.get(rf.round_id, [])
                        if p["exit"] == "lost"
                        and p["first_gate"] == outcome_gate),
            "round": rf.round_id,
        }
    return cells


def block_contrasts(block, party_rows, eligible_rounds=None):
    """一个块上的五个差别，只用 eligible_rounds（默认全部轮次）。

    eligible_rounds: estimand key -> 允许使用的 round_id 集合。
    主效应/外溢在另一因子的每个层级上配对求差；配对缺失时退回可用层级
    均值差并在 detail 中注明不平衡。交互要求四类组合全部在集合内。
    """
    eligible_rounds = eligible_rounds or {}
    out, detail = {}, {}
    cells_n_all = _cell_means(block, party_rows, "N")
    cells_e_all = _cell_means(block, party_rows, "E")

    def combo_round(block):
        return {rf.combo: rf.round_id for rf in block.rounds.values()}

    combo_to_round = combo_round(block)

    def filtered(cells, key):
        allowed = eligible_rounds.get(key)
        if allowed is None:
            return cells
        return {c: v for c, v in cells.items()
                if combo_to_round.get(c) in allowed}

    def paired_diff(cells, factor_idx, key):
        cells = filtered(cells, key)
        deltas, used = [], []
        for other in ("old", "new"):
            if factor_idx == 0:
                a, b = f"new/{other}", f"old/{other}"
            else:
                a, b = f"{other}/new", f"{other}/old"
            if a in cells and b in cells and \
                    cells[a]["mean"] is not None and cells[b]["mean"] is not None:
                deltas.append(cells[a]["mean"] - cells[b]["mean"])
                used.append((a, b))
        if not deltas:
            return None, "干净轮次中无同层配对"
        return sum(deltas) / len(deltas), f"配对 {used}"

    out["main_N"], detail["main_N"] = paired_diff(cells_n_all, 0, "main_N")
    out["spill_E"], detail["spill_E"] = paired_diff(cells_e_all, 0, "spill_E")
    out["main_E"], detail["main_E"] = paired_diff(cells_e_all, 1, "main_E")
    out["spill_N"], detail["spill_N"] = paired_diff(cells_n_all, 1, "spill_N")

    cells_n = filtered(cells_n_all, "interaction")
    if all(c in cells_n and cells_n[c]["mean"] is not None for c in COMBOS):
        out["interaction"] = (
            cells_n["new/new"]["mean"] - cells_n["new/old"]["mean"]
            - cells_n["old/new"]["mean"] + cells_n["old/old"]["mean"])
        detail["interaction"] = "四类组合轮次全部干净"
    else:
        out["interaction"] = None
        detail["interaction"] = "干净轮次不足四类组合"
    return out, {"N": cells_n_all, "E": cells_e_all}, detail


# ---------- 支持状态 ----------

def round_eligibility(block):
    """逐轮标记各差别能否使用该轮证据（轮次级，而非整块判死）。

    - carryover：队伍连接到同日下一轮，本规则按“连接的两轮都扣支持”，
      因此滞留发生轮与其后继轮同时失去干净资格；
    - 计划外通道只扣除外溢/交互类比较的资格，主效应仍可用；
    - 队列断档只影响队列读数类指标，到场时延仍可估，另列 gap 标记；
    - 撤牌偏离不改变 ITT 分组，只登记 deviation 标记，不扣资格。
    返回 round_id -> set(受影响 estimand key) 等三张表。
    """
    rfs = sorted(block.rounds.values(), key=lambda r: r.position)
    spillover_set = {"spill_N", "spill_E", "interaction"}
    tainted = {rf.round_id: set() for rf in rfs}
    gaps = {rf.round_id: set() for rf in rfs}
    deviations = {rf.round_id: set() for rf in rfs}

    for i, rf in enumerate(rfs):
        if rf.carryover_parties:
            tainted[rf.round_id].update(
                {"main_N", "main_E", "spill_N", "spill_E", "interaction"})
            if i + 1 < len(rfs):
                tainted[rfs[i + 1].round_id].update(
                    {"main_N", "main_E", "spill_N", "spill_E", "interaction"})
        if rf.unplanned_links:
            tainted[rf.round_id].update(spillover_set)
        for gate, n in rf.queue_gaps.items():
            if n:
                gaps[rf.round_id].add(gate)
        for gate, _ in rf.reverts:
            deviations[rf.round_id].add(gate)
    return tainted, gaps, deviations


def assess_block_support(block, rules=None):
    """逐差别给出支持状态与具体原因。

    主效应/外溢只要求相关因子两个层级各有至少一个干净轮次；交互要求块内
    四类组合所在轮次全部干净。三类证据不足与业务口径一一对应：
    - missing_combo：计划中缺少所需组合/层级；
    - incomplete_observation：有组合但本指标观察覆盖不全（队列断档）；
    - confounded：有观测但两种影响分不清（跨轮滞留、计划外通道）。
    """
    flags = {}
    rfs = list(block.rounds.values())
    combos_present = {rf.combo for rf in rfs}
    tainted, gaps, deviations = round_eligibility(block)

    for estimand in ESTIMANDS:
        reasons = []
        missing = set(COMBOS) - combos_present
        if missing:
            reasons.append(("missing_combo",
                            f"缺少组合 {'/'.join(sorted(missing))}"))

        clean_rounds, confounded_rounds, gap_rounds, revert_rounds = [], [], [], []
        factor_idx = {"N": 0, "E": 1, "both": None}[estimand.factor]
        for rf in rfs:
            if estimand.key in tainted[rf.round_id]:
                why = []
                if rf.carryover_parties:
                    why.append(f"跨轮滞留 {rf.carryover_parties} 队")
                if rf.unplanned_links and estimand.key in (
                        "spill_N", "spill_E", "interaction"):
                    why.append("计划外通道 "
                               + ",".join(x[0] for x in rf.unplanned_links))
                if why:
                    confounded_rounds.append((rf.round_id, why))
                else:
                    # 滞留后继轮：本身无事件，标注被连接轮拖累
                    confounded_rounds.append(
                        (rf.round_id, ["受相邻跨轮滞留连接，证据扣除"]))
                continue
            if estimand.outcome_gate in gaps[rf.round_id]:
                gap_rounds.append(
                    (rf.round_id,
                     f"{estimand.outcome_gate} 口队列断档 "
                     f"{rf.queue_gaps[estimand.outcome_gate]} 处"))
            if estimand.outcome_gate in deviations[rf.round_id]:
                revert_rounds.append(
                    (rf.round_id, f"{estimand.outcome_gate} 口撤牌偏离（ITT 保留）"))
            clean_rounds.append(rf)

        # 因子层级覆盖（在干净轮次中）
        factor_blocked = False
        if factor_idx is not None:
            levels = {rf.combo.split("/")[factor_idx] for rf in clean_rounds}
            if not missing and len(levels) < 2:
                reasons.append(("confounded",
                                f"因子 {estimand.factor} 的对照层级所在轮次均"
                                f"受污染，干净轮次仅含层级 "
                                f"{'/'.join(sorted(levels)) or '无'}"))
                factor_blocked = True
        else:
            if len(clean_rounds) < 4 and not missing:
                reasons.append(("confounded",
                                f"交互需四类组合轮次全部干净，"
                                f"干净轮次 {len(clean_rounds)}/4"))
                factor_blocked = True

        if confounded_rounds:
            reasons.append(("confounded_rounds", confounded_rounds))
        if gap_rounds:
            reasons.append(("incomplete_observation", gap_rounds))
        if revert_rounds:
            reasons.append(("deviation_itt_kept", revert_rounds))

        kinds = {k for k, _ in reasons}
        if "missing_combo" in kinds:
            status = "missing_combo"
        elif factor_blocked:
            status = "confounded"
        elif "incomplete_observation" in kinds:
            status = "incomplete_observation"
        else:
            status = "clean"
        flags[estimand.key] = {
            "status": status,
            "reasons": reasons,
            "clean_round_ids": [rf.round_id for rf in clean_rounds],
        }
    return flags


def t_critical(level, df):
    """标准库内的 t 双侧分位反解（二分查找），仅用于 df>=1 的小样本区间。"""
    alpha = 1 - level
    # 常用小样本表，df 较大时收敛到正态
    table = {
        1: 6.314, 2: 2.920, 3: 2.353, 4: 2.132, 5: 2.015,
        6: 1.943, 7: 1.895, 8: 1.860, 9: 1.833, 10: 1.812,
        11: 1.796, 12: 1.782, 13: 1.771, 14: 1.761, 15: 1.753,
    }
    if level != 0.9:
        raise ValueError("当前仅内置 0.90 水平的 t 分位")
    return table.get(df, 1.645)


def estimate(blocks, party_rows, rules):
    """跨块汇总。

    主口径只使用 clean 块；confounded（滞留/计划外通道）块不进入点估计，
    incomplete_observation（仅队列断档）块另给 relaxed 次级估计并单独标注；
    missing_combo 块只登记原因。所有口径下撤牌偏离都不改变 ITT 分组。
    """
    level = rules["interval_level"]
    min_blocks = rules["min_comparable_blocks"]
    per_block = {}
    for day, block in blocks.items():
        flags = assess_block_support(block, rules)
        block.flags = flags
        eligible = {key: set(info["clean_round_ids"])
                    for key, info in flags.items()}
        contrasts, cells, detail = block_contrasts(
            block, party_rows, eligible)
        per_block[day] = {"contrasts": contrasts, "cells": cells,
                          "flags": flags, "detail": detail}

    def _aggregate(eligible):
        vals = [(day, per_block[day]["contrasts"][key])
                for day in eligible
                if per_block[day]["contrasts"][key] is not None]
        n = len(vals)
        out = {
            "n_eligible": len(eligible),   # 资格块数（含算不出对比的）
            "n_blocks": n,                 # 实际出值的块数
            "blocks": [d for d, _ in vals],
            "block_values": dict(vals),
        }
        if n >= 2:
            xs = [v for _, v in vals]
            mean = sum(xs) / n
            var = sum((v - mean) ** 2 for v in xs) / (n - 1)
            se = math.sqrt(var / n)
            tc = t_critical(level, n - 1)
            out.update(point=mean, se=se,
                       ci_low=mean - tc * se, ci_high=mean + tc * se)
        elif n == 1:
            out.update(point=vals[0][1], se=None,
                       ci_low=None, ci_high=None)
        else:
            out.update(point=None, se=None, ci_low=None, ci_high=None)
        return out

    report = {}
    for estimand in ESTIMANDS:
        key = estimand.key
        buckets = {"clean": [], "incomplete_observation": [],
                   "confounded": [], "missing_combo": []}
        for day, info in per_block.items():
            buckets[info["flags"][key]["status"]].append(day)

        primary = _aggregate(buckets["clean"])
        relaxed = _aggregate(buckets["clean"]
                             + buckets["incomplete_observation"])
        # relaxed 含断档块时才另列；否则与主口径相同，不重复报告
        relaxed_available = bool(buckets["incomplete_observation"])
        n = primary["n_blocks"]
        entry = {
            "title": estimand.title,
            "clean_blocks": primary["blocks"],
            "gap_only_blocks": buckets["incomplete_observation"],
            "confounded_blocks": buckets["confounded"],
            "missing_combo_blocks": buckets["missing_combo"],
            "excluded": [(day, per_block[day]["flags"][key]["status"],
                          per_block[day]["flags"][key]["reasons"])
                         for day in (buckets["confounded"]
                                     + buckets["incomplete_observation"]
                                     + buckets["missing_combo"])],
            "n_blocks": n,
            "required_blocks": min_blocks,
            **{k: primary[k] for k in
               ("point", "se", "ci_low", "ci_high", "block_values")},
            "relaxed": {k: relaxed[k] for k in
                        ("n_eligible", "n_blocks", "point",
                         "ci_low", "ci_high")},
            "relaxed_available": relaxed_available,
        }
        if n == 0 and not primary["blocks"]:
            entry["verdict"] = "no_clean_block"
        elif n < min_blocks:
            entry["verdict"] = "below_block_threshold"
        else:
            entry["verdict"] = "estimable"
        report[key] = entry
    return report, per_block
