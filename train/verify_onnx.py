"""第七步：用项目环境验证 ONNX 模型（不依赖 torch）。

项目 venv 里已经有 onnxruntime（ddddocr 的依赖），所以能直接跑这个脚本。

    uv run python train/verify_onnx.py
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image

HERE = Path(__file__).resolve().parent
MODEL = HERE.parent / "seu_lecture" / "models" / "captcha.onnx"
TEST_DIR = HERE.parent.parent / "Lecture-Reserve-SEU-master" / "image"

# 见 evaluate.py 的说明：8029.jpg 的文件名与图像内容不符
LABEL_FIXES = {"8029": "8529"}


def preprocess(path: Path) -> np.ndarray:
    array = np.asarray(Image.open(path).convert("L"), dtype=np.float32) / 255.0
    array = (array - 0.5) / 0.5
    return array[None, None, :, :]


def softmax(values: np.ndarray) -> np.ndarray:
    shifted = values - values.max(axis=-1, keepdims=True)
    exp = np.exp(shifted)
    return exp / exp.sum(axis=-1, keepdims=True)


def main() -> int:
    if not MODEL.exists():
        raise SystemExit(f"找不到模型：{MODEL}，先跑 export_onnx.py")

    session = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
    print(f"模型 {MODEL.name}  {MODEL.stat().st_size/1024:.0f} KB\n")

    samples = sorted(TEST_DIR.glob("*.jpg"))
    print(f"{'真值':<8}{'预测':<8}{'裕度':<9}{'结果':<6}{'耗时'}")
    correct = 0
    timings: list[float] = []

    for path in samples:
        truth = LABEL_FIXES.get(path.stem, path.stem)
        tensor = preprocess(path)
        started = time.perf_counter()
        logits = session.run(None, {"image": tensor})[0][0]
        timings.append((time.perf_counter() - started) * 1000)
        probs = softmax(logits)
        digits = probs.argmax(axis=1)
        margins = [probs[i][digits[i]] - np.partition(probs[i], -2)[-2] for i in range(4)]
        text = "".join(str(d) for d in digits)
        ok = text == truth
        correct += ok
        print(f"{truth:<8}{text:<8}{min(margins):<9.3f}{'✓' if ok else '✗':<6}{timings[-1]:.2f} ms")

    steady = []
    tensor = preprocess(samples[0])
    for _ in range(30):
        started = time.perf_counter()
        session.run(None, {"image": tensor})
        steady.append((time.perf_counter() - started) * 1000)

    print(f"\n整串准确率 {correct}/{len(samples)} = {correct/len(samples):.1%}")
    print(f"首次推理 {statistics.mean(timings):.2f} ms")
    print(f"稳态中位数 {statistics.median(steady):.2f} ms（平均 {statistics.mean(steady):.2f} ms）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
