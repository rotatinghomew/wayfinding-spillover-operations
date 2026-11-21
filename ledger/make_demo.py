"""生成演示用变更台账 ledger/events_demo.csv（被 .gitignore 忽略，可重建）。

运行：python3 ledger/make_demo.py
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wayfinding.ledger import append_event  # noqa: E402

PATH = Path(__file__).resolve().parent / "events_demo.csv"


def main():
    if PATH.exists():
        PATH.unlink()
    append_event(PATH, "2026-10-22T09:10", "sign", "C3-0", "old/old",
                 "局部签收，已签时段继续占用人员，不事后重抽")
    append_event(PATH, "2026-10-22T09:25", "incompatible", "C3-1", "",
                 "志愿班次已交给对方保留，不能再次配置")
    print(f"已生成 {PATH}")


if __name__ == "__main__":
    main()
