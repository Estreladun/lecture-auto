"""第一步：用现有识别器给真实样本打伪标签，并按置信度分级。

这么做的原因：新增标注是训练的主要成本，而现有 ddddocr 方案已经有 93% 准确率。
先用它标一遍，只让人工复核「它自己也没把握」的那部分，能把标注量压缩 60% 以上。

输出 train/out/auto_labels.json：
    {"captcha_1.jpg": {"label": "1234", "margin": 0.93, "tier": "high"}, ...}

三档划分：
    high   裕度 >= 0.80   直接当训练标签
    mid    0.30 ~ 0.80    建议人工复核（错得最多的一档）
    low    < 0.30 或解不出  必须人工标注
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from seu_lecture import captcha  # noqa: E402

HIGH, MID = 0.80, 0.30


def prepare_images(source: Path, target: Path) -> int:
    target.mkdir(parents=True, exist_ok=True)
    if any(target.glob("*.jpg")):
        count = len(list(target.glob("*.jpg")))
        print(f"图片目录已存在 {count} 张，跳过复制")
        return count
    files = sorted(source.glob("*.jpg"))
    if not files:
        raise SystemExit(f"源目录没有图片：{source}")
    for path in files:
        shutil.copy2(path, target / path.name)
    print(f"已复制 {len(files)} 张图片到 {target}")
    return len(files)


def main() -> int:
    parser = argparse.ArgumentParser(description="给验证码样本打伪标签")
    parser.add_argument(
        "--source",
        default=str(ROOT.parent / "Lecture-Reserve-SEU-master" / "images"),
        help="原始图片目录",
    )
    args = parser.parse_args()

    image_dir = Path(__file__).resolve().parent / "data" / "images"
    prepare_images(Path(args.source), image_dir)

    if not captcha.warmup():
        print("识别模型加载失败，检查 ddddocr 是否已安装")
        return 1

    files = sorted(image_dir.glob("*.jpg"))
    records: dict[str, dict] = {}
    tiers = {"high": 0, "mid": 0, "low": 0}

    for index, path in enumerate(files, 1):
        outcome = captcha.analyze(path.read_bytes(), rescue=True)
        if outcome is None:
            records[path.name] = {"label": None, "margin": 0.0, "tier": "low"}
            tiers["low"] += 1
        else:
            code, margin = outcome
            tier = "high" if margin >= HIGH else "mid" if margin >= MID else "low"
            records[path.name] = {
                "label": code,
                "margin": round(float(margin), 4),
                "tier": tier,
            }
            tiers[tier] += 1
        if index % 200 == 0:
            print(f"  已处理 {index}/{len(files)}")

    out = Path(__file__).resolve().parent / "out" / "auto_labels.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")

    total = len(files)
    print(f"\n共 {total} 张，结果写入 {out}")
    print(f"  高可信（直接训练）：{tiers['high']:4d}  {tiers['high']/total:5.1%}")
    print(f"  待复核：            {tiers['mid']:4d}  {tiers['mid']/total:5.1%}")
    print(f"  必标注：            {tiers['low']:4d}  {tiers['low']/total:5.1%}")
    print(f"\n下一步：uv run python train/make_review.py 生成人工复核页面")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
