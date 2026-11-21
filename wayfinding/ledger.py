"""轮换计划的变更台账（事件溯源）。

每次局部签收、撤回、约束修订都登记为一条只增事件；已签时段继续占用
人员，不按更有利的组合事后重抽。回放任意时点的状态即可回答：
“某一种比较是在哪次变更之后失去支持的”。

事件文件为 UTF-8 CSV，列：
  seq, ts, kind, candidate, combo, detail
kind 取值：sign（签认固定）、release（撤回签认，时段回到可随机）、
incompatible（班次不可再配置）、constraint（场地方约束修订，detail 说明）。
"""

import csv
from dataclasses import dataclass
from pathlib import Path

from .planner import load_planning_inputs, _blocks, plan_day, summarize

LEDGER_HEADER = ["seq", "ts", "kind", "candidate", "combo", "detail"]


@dataclass
class LedgerEvent:
    seq: int
    ts: str
    kind: str
    candidate: str
    combo: str
    detail: str


def read_ledger(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return [LedgerEvent(int(r["seq"]), r["ts"], r["kind"],
                        r["candidate"], r["combo"], r["detail"])
            for r in rows]


def append_event(path, ts, kind, candidate, combo, detail):
    path = Path(path)
    events = read_ledger(path)
    seq = (max((e.seq for e in events), default=-1) + 1)
    event = LedgerEvent(seq, ts, kind, candidate, combo, detail)
    new_file = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(LEDGER_HEADER)
        writer.writerow([event.seq, event.ts, event.kind,
                         event.candidate, event.combo, event.detail])
    return event


def apply_events(slots, events):
    """把台账事件叠加到输入快照上，返回带覆盖状态的时段字典（深复制语义）。"""
    working = {cid: SlotView(s) for cid, s in slots.items()}
    for e in events:
        if e.kind == "sign":
            view = working.get(e.candidate)
            if view is None:
                raise ValueError(f"事件 {e.seq} 引用未知时段 {e.candidate}")
            view.fixed_combo = e.combo
            view.offer_combo = None
            view.history.append((e.seq, f"签认固定 {e.combo}"))
        elif e.kind == "release":
            view = working[e.candidate]
            view.fixed_combo = None
            view.history.append((e.seq, "撤回签认，回到可随机"))
        elif e.kind == "incompatible":
            working[e.candidate].unavailable = True
            working[e.candidate].history.append((e.seq, "班次不可再配置"))
        elif e.kind == "constraint":
            for view in working.values():
                view.history.append((e.seq, f"约束修订：{e.detail}"))
        else:
            raise ValueError(f"未知事件类型 {e.kind}")
    return working


class SlotView:
    """planner.Slot 的可覆盖视图，字段与原对象保持一致。"""

    def __init__(self, slot):
        self.candidate = slot.candidate
        self.day = slot.day
        self.shift = slot.shift
        self.start = slot.start
        self.end = slot.end
        self.allowed = slot.allowed
        self.shared_buffer = slot.shared_buffer
        self.guide_hours = slot.guide_hours
        self.minimum_coverage = slot.minimum_coverage
        self.fixed_combo = slot.fixed_combo
        self.unavailable = slot.unavailable
        self.offer_combo = slot.offer_combo
        self.note = slot.note
        self.history = []

    @property
    def position(self):
        return self.candidate.split("-")[1]


def replay(data_dir, ledger_path, min_comparable_blocks,
           up_to_seq=None, scenario="baseline"):
    """回放至指定事件序号（含），重排整张单并复核逐差别支持。"""
    slots, _ = load_planning_inputs(data_dir)
    events = read_ledger(ledger_path)
    if up_to_seq is not None:
        events = [e for e in events if e.seq <= up_to_seq]
    working = apply_events(slots, events)
    days = _blocks(working)
    plans = [plan_day(day, days[day], scenario) for day in sorted(days)]
    return plans, summarize(plans, min_comparable_blocks), events


def support_timeline(data_dir, ledger_path, min_comparable_blocks):
    """逐事件回放，记录每个差别支持等级的变化轨迹。"""
    all_events = read_ledger(ledger_path)
    timeline = []
    prev = None
    checkpoints = [-1] + [e.seq for e in all_events]
    for seq in checkpoints:
        plans, summary, events = replay(
            data_dir, ledger_path, min_comparable_blocks, up_to_seq=seq)
        snap = {key: (s["counts"]["full"], s["verdict"])
                for key, s in summary.items()}
        changed = prev is None or snap != prev
        timeline.append({
            "seq": seq,
            "event": None if seq < 0 else next(
                (e for e in all_events if e.seq == seq), None),
            "snapshot": snap,
            "changed": changed,
        })
        prev = snap
    return timeline
