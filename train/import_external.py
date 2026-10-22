"""导入外部四位数字验证码数据集。

数据来源：GT-ZhangAcer/CaptchaDataset（MIT 许可）
    采自河南部分高校登录页，9453 张四位数字图 + 2000 张单字图，标注经人工审核。

关键事实：这批图尺寸同为 70x30，与东大验证码一致；
现有识别器在它上面能达到 97.5%，说明是同一类验证码组件，几乎无域差异。
所以这些干净标签可以直接用于训练，不需要伪标签。

    git clone --depth 1 https://github.com/GT-ZhangAcer/CaptchaDataset.git \
        train/data/external_dataset
    uv run python train/import_external.py
"""

from __future__ import annotations

import argparse
import ast
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_SRC = HERE / "data" / "external_dataset" / "OCR_Dataset"


def main() -> int:
    parser = argparse.ArgumentParser(description="导入外部验证码数据集")
    parser.add_argument("--source", default=str(DEFAULT_SRC))
    parser.add_argument("--prefix", default="ext_")
    args = parser.parse_args()

    source = Path(args.source)
    label_file = source / "label_dict.txt"
    if not label_file.exists():
        raise SystemExit(
            f"找不到 {label_file}\n先执行：\n"
            f"  git clone --depth 1 https://github.com/GT-ZhangAcer/CaptchaDataset.git "
            f"{HERE / 'data' / 'external_dataset'}"
        )

    # 标注文件是 Python dict 字面量（单引号），不能直接 json.loads
    raw = label_file.read_text(encoding="utf-8")
    labels: dict = ast.literal_eval(raw)
    print(f"标注条目 {len(labels)}")

    image_dir = HERE / "data" / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    result: dict[str, str] = {}
    copied = skipped = 0
    for name, label in labels.items():
        text = str(label).strip()
        if len(text) != 4 or not text.isdigit():
            skipped += 1
            continue
        src = source / name
        if not src.exists():
            skipped += 1
            continue
        target_name = args.prefix + name
        target = image_dir / target_name
        if not target.exists():
            shutil.copy2(src, target)
            copied += 1
        result[target_name] = text

    out = HERE / "data" / "labels_external.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"新复制 {copied} 张，跳过 {skipped} 条")
    print(f"标签写入 {out}（{len(result)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
