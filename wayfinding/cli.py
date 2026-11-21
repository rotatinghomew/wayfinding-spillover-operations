"""命令行入口：排期、可估计性审计、历史重估、变更回放。

用法：
  python -m wayfinding.cli plan [--scenario baseline|offer_accepted|both]
  python -m wayfinding.cli analyze
  python -m wayfinding.cli export --out output/
  python -m wayfinding.cli replay [--ledger PATH] [--seq N]
  python -m wayfinding.cli timeline [--ledger PATH]
"""

import argparse
import csv
import json
from pathlib import Path

from .planner import build_plans, summarize, ESTIMANDS, DATA
from .analyze import build_facts, estimate
from .ledger import replay, support_timeline


def _rules():
    return json.loads((DATA / "rules.json").read_text(encoding="utf-8"))


def cmd_plan(args):
    plans_by_scenario, signatures = build_plans(DATA)
    min_blocks = _rules()["min_comparable_blocks"]
    scenarios = [args.scenario] if args.scenario != "both" else \
        ["baseline", "offer_accepted"]
    output = {}
    for scenario in scenarios:
        plans = plans_by_scenario[scenario]
        print(f"\n=== 轮换单（场景：{scenario}）===")
        for plan in plans:
            print(f"\n[{plan.day}] feasible={plan.feasible}  "
                  f"候选排法 {plan.candidate_count} 种")
            for slot in plan.slots:
                combo = plan.chosen.get(slot.candidate, "—（本时段不换牌）")
                tag = slot.note or ""
                print(f"  {slot.candidate} {slot.start}-{slot.end} "
                      f"buf={slot.shared_buffer} "
                      f"cover={slot.minimum_coverage} -> {combo}  {tag}")
            for cid, why in plan.blocked:
                print(f"  阻塞 {cid}: {why}")
            for reason in plan.reasons:
                print(f"  原因: {reason}")
        summary = summarize(plans, min_blocks)
        print(f"\n=== 逐差别支持（门槛：{min_blocks} 个独立轮换块）===")
        for estimand in ESTIMANDS:
            s = summary[estimand.key]
            print(f"\n{estimand.key}: {s['title']}")
            print(f"  full={s['counts']['full']} coverage="
                  f"{s['counts']['coverage']} partial="
                  f"{s['counts']['partial']} absent="
                  f"{s['counts']['absent']} -> {s['verdict']}")
            for day, level, detail in s["rows"]:
                if level != "full":
                    print(f"    {day} [{level}] {detail}")
        output[scenario] = summary
    return output


def cmd_analyze(args):
    rules = _rules()
    rounds, blocks, party_rows = build_facts(DATA, rules)
    report, per_block = estimate(blocks, party_rows, rules)
    # 事先业务门槛：到场时延最小可识别增益（分钟），区间半宽需不大于它
    gain = rules["min_arrival_gain_minutes"]

    print("=== 事前分配下的 ITT 重估（到场时延，分钟；新-旧）===")
    print("注：撤牌偏离保留原分配；closed 与 lost 不计入均值且互不替代；")
    print("    主口径只用 clean 块，括号内为加入仅断档块的 relaxed 口径\n")
    for estimand in ESTIMANDS:
        e = report[estimand.key]
        print(f"{estimand.key}: {e['title']}")
        print(f"  clean 块 {e['n_blocks']}/{e['required_blocks']}："
              f"{', '.join(e['clean_blocks']) or '无'}")
        print(f"  仅观察断档块：{', '.join(e['gap_only_blocks']) or '无'}；"
              f"分不清（confounded）：{', '.join(e['confounded_blocks']) or '无'}；"
              f"缺组合：{', '.join(e['missing_combo_blocks']) or '无'}")
        if e["point"] is not None:
            if e["ci_low"] is not None:
                half = (e["ci_high"] - e["ci_low"]) / 2
                precision = ("达到" if half <= gain else "未达")
                ci = f"{e['ci_low']:.2f} ~ {e['ci_high']:.2f}"
                print(f"  点估计={e['point']:.2f}，90% 区间 {ci}，"
                      f"半宽 {half:.2f} vs 门槛 {gain} 分钟 -> {precision}精度门槛")
            else:
                print(f"  点估计={e['point']:.2f}（仅 1 块，无区间）")
        else:
            print("  无可用 clean 块，主口径不出估计")
        r = e["relaxed"]
        if e["relaxed_available"] and r["point"] is not None and r["ci_low"] is not None:
            print(f"  relaxed（资格块 {r['n_eligible']} 个，含仅断档块；"
                  f"出值 {r['n_blocks']} 个）：{r['point']:.2f}，90% 区间 "
                  f"{r['ci_low']:.2f} ~ {r['ci_high']:.2f}（仅供敏感性参考）")
        for day, status, reasons in e["excluded"]:
            parts = []
            for kind, payload in reasons:
                if isinstance(payload, str):
                    parts.append(f"{kind}: {payload}")
                else:
                    rendered = []
                    for rid, why in payload:
                        rendered.append(
                            f"{rid}[{why if isinstance(why, str) else '；'.join(why)}]")
                    parts.append(f"{kind}: " + ",".join(rendered))
            print(f"  排除 {day} [{status}] " + "；".join(parts))
        print()

    print("=== 轮次事实摘要（closed=闭馆确认未到达，lost=观察中断，互不替代）===")
    for day in sorted(blocks):
        for pos, rf in sorted(blocks[day].rounds.items()):
            flags = []
            if rf.reverts:
                flags.append("撤牌 " + ",".join(g for g, _ in rf.reverts))
            if rf.carryover_parties:
                flags.append(f"滞留 {rf.carryover_parties} 队")
            if rf.unplanned_links:
                flags.append("计划外通道 "
                             + ",".join(x[0] for x in rf.unplanned_links))
            gaps = [f"{g}断档{n}" for g, n in rf.queue_gaps.items() if n]
            if gaps:
                flags.append(" ".join(gaps))
            rows = party_rows.get(rf.round_id, [])
            n_closed = sum(1 for p in rows if p["exit"] == "closed")
            n_lost = sum(1 for p in rows if p["exit"] == "lost")
            actual = "/".join(rf.actual.get(g, "?") for g in ("N", "E"))
            print(f"  {rf.round_id} 分配 {rf.combo} 实际 {actual} "
                  f"closed={n_closed} lost={n_lost} "
                  + ("| " + "；".join(flags) if flags else ""))
    return report


def cmd_replay(args):
    min_blocks = _rules()["min_comparable_blocks"]
    plans, summary, events = replay(
        DATA, args.ledger, min_blocks,
        up_to_seq=args.seq, scenario=args.scenario)
    print(f"回放至事件序号 {args.seq if args.seq is not None else '最新'}"
          f"（共 {len(events)} 条台账事件）")
    for estimand in ESTIMANDS:
        s = summary[estimand.key]
        print(f"{estimand.key}: full={s['counts']['full']} "
              f"coverage={s['counts']['coverage']} "
              f"partial={s['counts']['partial']} "
              f"absent={s['counts']['absent']} -> {s['verdict']}")
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2,
                         default=str))
    return summary


def cmd_timeline(args):
    min_blocks = _rules()["min_comparable_blocks"]
    timeline = support_timeline(DATA, args.ledger, min_blocks)
    print("seq\t事件\t各差别 full 块数/结论（仅列变化点）")
    for item in timeline:
        if not item["changed"]:
            continue
        ev = item["event"]
        ev_txt = "初始" if ev is None else f"{ev.ts} {ev.kind} {ev.candidate}"
        snap = " | ".join(f"{k}={v[0]}/{v[1]}" for k, v in item["snapshot"].items())
        print(f"{item['seq']}\t{ev_txt}\t{snap}")
    return timeline


def cmd_export(args):
    """导出整张轮换单 CSV 与逐差别支持审计 JSON。"""
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rules = _rules()
    min_blocks = rules["min_comparable_blocks"]
    plans_by_scenario, _ = build_plans(DATA)

    sheet_path = out / "next_round_sheet.csv"
    with sheet_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scenario", "day", "candidate", "start", "end",
                    "combo", "shared_buffer", "coverage", "status", "note"])
        for scenario, plans in plans_by_scenario.items():
            for plan in plans:
                blocked = dict(plan.blocked)
                for slot in plan.slots:
                    if slot.candidate in plan.chosen:
                        combo = plan.chosen[slot.candidate]
                        status = "fixed" if slot.candidate in {
                            s.candidate for s in plan.slots
                            if s.fixed_combo or
                            (scenario == "offer_accepted" and s.offer_combo)
                        } else "randomizable"
                        note = slot.note
                    else:
                        combo, status, note = (
                            "", "blocked", blocked.get(slot.candidate, ""))
                    w.writerow([scenario, plan.day, slot.candidate,
                                slot.start, slot.end, combo,
                                slot.shared_buffer, slot.minimum_coverage,
                                status, note])

    audit = {}
    for scenario, plans in plans_by_scenario.items():
        audit[scenario] = {
            key: {
                "title": s["title"],
                "counts": s["counts"],
                "full_blocks": s["full_blocks"],
                "required": s["required"],
                "verdict": s["verdict"],
                "block_detail": [
                    {"day": d, "level": lv, "detail": dt}
                    for d, lv, dt in s["rows"] if lv != "full"],
            }
            for key, s in summarize(plans, min_blocks).items()}
    audit_path = out / "design_audit.json"
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2),
                          encoding="utf-8")

    rounds, blocks, party_rows = build_facts(DATA, rules)
    report, _ = estimate(blocks, party_rows, rules)
    slim = {key: {k: v for k, v in e.items()
                  if k in ("title", "verdict", "n_blocks", "required_blocks",
                           "point", "ci_low", "ci_high", "clean_blocks",
                           "gap_only_blocks", "confounded_blocks",
                           "missing_combo_blocks", "relaxed",
                           "relaxed_available")}
            for key, e in report.items()}
    (out / "historical_itt_report.json").write_text(
        json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写出 {sheet_path}")
    print(f"已写出 {audit_path}")
    print(f"已写出 {out / 'historical_itt_report.json'}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="指路牌轮换设计与证据分析")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="生成下一轮整张轮换单并审计支持")
    p_plan.add_argument("--scenario", default="both",
                        choices=["baseline", "offer_accepted", "both"])
    p_plan.set_defaults(func=cmd_plan)

    p_an = sub.add_parser("analyze", help="历史轮次 ITT 重估")
    p_an.set_defaults(func=cmd_analyze)

    p_ex = sub.add_parser("export", help="导出轮换单 CSV 与审计/分析 JSON")
    p_ex.add_argument("--out", default="output")
    p_ex.set_defaults(func=cmd_export)

    p_rp = sub.add_parser("replay", help="按台账回放排期与支持状态")
    p_rp.add_argument("--ledger", default=str(DATA.parent / "ledger" / "events.csv"))
    p_rp.add_argument("--seq", type=int, default=None)
    p_rp.add_argument("--scenario", default="baseline",
                      choices=["baseline", "offer_accepted"])
    p_rp.add_argument("--json", action="store_true")
    p_rp.set_defaults(func=cmd_replay)

    p_tl = sub.add_parser("timeline", help="逐事件列出支持变化轨迹")
    p_tl.add_argument("--ledger", default=str(DATA.parent / "ledger" / "events.csv"))
    p_tl.set_defaults(func=cmd_timeline)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
