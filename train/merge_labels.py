"""第三步：合并伪标签与人工复核结果，产出最终训练标签。

优先级：人工复核 > 高可信伪标签 > 丢弃
人工标为 null（跳过）或未覆盖的低可信样本一律不参与训练，宁可少一点也别引入错标签。
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    auto = load(OUT / "auto_labels.json")
    reviewed = load(OUT / "reviewed.json")
    if not auto:
        raise SystemExit("缺少 auto_labels.json，先运行 autolabel.py")
    if not reviewed:
        print("警告：没有找到 reviewed.json，本地样本只使用高可信伪标签")
        print("      （外部数据集的干净标签仍会参与训练）")

    final: dict[str, str] = {}
    from_review = from_auto = dropped = 0

    for name, info in auto.items():
        if name in reviewed:
            label = reviewed[name]
            if label and len(str(label)) == 4 and str(label).isdigit():
                final[name] = str(label)
                from_review += 1
            else:
                dropped += 1
            continue
        if info.get("tier") == "high" and info.get("label"):
            final[name] = info["label"]
            from_auto += 1
        else:
            dropped += 1

    # 外部数据集标签经人工审核，直接并入
    external = load(HERE / "data" / "labels_external.json")
    final.update(external)

    target = HERE / "data" / "labels.json"
    target.write_text(json.dumps(final, ensure_ascii=False, indent=1), encoding="utf-8")

    total = len(auto)
    print(f"本地人工确认：{from_review}")
    print(f"本地伪标签采纳：{from_auto}")
    print(f"本地丢弃：{dropped} / {total}")
    print(f"外部干净标签：{len(external)}")
    print(f"最终训练集：{len(final)} 张 -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
