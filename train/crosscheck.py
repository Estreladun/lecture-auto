"""交叉校验：用训练好的模型反过来检查训练标签。

思路：模型和标签是两条独立的判断路径。两者不一致的地方，
必定至少有一方是错的，值得人工看一眼。

怎么解读分歧率：
- 外部数据集是人工审核的干净标签，它的分歧率约等于**模型自身的错误率**
- 本地伪标签的分歧率如果明显高于这个基线，说明伪标签确实有噪声

    uv run python train/crosscheck.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image

HERE = Path(__file__).resolve().parent
MODEL = HERE.parent / "seu_lecture" / "models" / "captcha.onnx"


def predict(session: ort.InferenceSession, path: Path) -> tuple[str, float]:
    picture = Image.open(path).convert("L")
    if picture.size != (70, 30):
        picture = picture.resize((70, 30), Image.BILINEAR)
    array = np.asarray(picture, dtype=np.float32) / 255.0
    array = ((array - 0.5) / 0.5)[None, None, :, :]

    logits = session.run(None, {"image": array})[0][0]
    shifted = logits - logits.max(axis=-1, keepdims=True)
    probs = np.exp(shifted) / np.exp(shifted).sum(axis=-1, keepdims=True)
    digits = probs.argmax(axis=1)
    margins = [sorted(probs[i])[-1] - sorted(probs[i])[-2] for i in range(len(digits))]
    return "".join(str(int(d)) for d in digits), float(min(margins))


def main() -> int:
    parser = argparse.ArgumentParser(description="交叉校验训练标签")
    parser.add_argument("--labels", default=str(HERE / "data" / "labels.json"))
    parser.add_argument("--external-prefix", default="ext_")
    args = parser.parse_args()

    labels = json.loads(Path(args.labels).read_text(encoding="utf-8"))
    session = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
    image_dir = HERE / "data" / "images"

    groups = {"external": {"total": 0, "diff": 0}, "local": {"total": 0, "diff": 0}}
    disagreements: list[dict] = []

    for name, label in sorted(labels.items()):
        path = image_dir / name
        if not path.exists():
            continue
        group = "external" if name.startswith(args.external_prefix) else "local"
        groups[group]["total"] += 1
        guess, margin = predict(session, path)
        if guess != label:
            groups[group]["diff"] += 1
            disagreements.append({
                "name": name,
                "group": group,
                "label": label,
                "model": guess,
                "margin": round(margin, 4),
            })

    print(f"{'来源':<12}{'样本数':<10}{'分歧':<8}{'分歧率'}")
    for group, stats in groups.items():
        total = stats["total"]
        rate = stats["diff"] / total if total else 0
        name = "外部(人工审核)" if group == "external" else "本地(伪标签)"
        print(f"{name:<12}{total:<10}{stats['diff']:<8}{rate:.2%}")

    base = groups["external"]
    local = groups["local"]
    base_rate = base["diff"] / base["total"] if base["total"] else 0
    local_rate = local["diff"] / local["total"] if local["total"] else 0
    print(f"\n以外部数据的分歧率 {base_rate:.2%} 作为模型误差基线：")
    if local_rate <= base_rate * 1.5:
        print("  本地伪标签分歧率接近基线，标签质量可用")
    else:
        print(f"  本地伪标签分歧率高出基线，约有 {local['diff'] - int(local['total']*base_rate)} 张标签存疑")

    out = HERE / "out" / "crosscheck.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(disagreements, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n分歧明细 {len(disagreements)} 条 -> {out}")

    locals_only = [d for d in disagreements if d["group"] == "local"]
    if locals_only:
        print("\n本地分歧样本（前 10 条，建议人工核对）：")
        for item in locals_only[:10]:
            print(f"  {item['name']:<20} 标签 {item['label']}  模型 {item['model']}  裕度 {item['margin']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
