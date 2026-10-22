"""第六步：把权重导出为 ONNX，供主项目推理使用。

为什么用 ONNX 而不是直接用 torch：
主项目已经通过 ddddocr 依赖了 onnxruntime，导出后推理不需要再引入 200MB 的 torch，
模型只有几百 KB，单张推理 1-2 ms。

    E:\\Miniforge3\\envs\\ai_base\\python.exe train\\export_onnx.py
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import torch

from model import CaptchaNet

HERE = Path(__file__).resolve().parent

try:  # onnxruntime 可能不在训练环境里，缺了只跳过一致性校验
    import onnxruntime as ort
except ImportError:  # pragma: no cover
    ort = None


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 ONNX 模型")
    parser.add_argument("--weights", default=str(HERE / "checkpoints" / "best.pt"))
    parser.add_argument("--output", default=str(HERE.parent / "seu_lecture" / "models" / "captcha.onnx"))
    args = parser.parse_args()

    weights = Path(args.weights)
    if not weights.exists():
        raise SystemExit(f"找不到权重：{weights}，先跑 train.py")

    payload = torch.load(weights, map_location="cpu", weights_only=False)
    channels = payload.get("channels", 32)
    model = CaptchaNet(channels=channels)
    model.load_state_dict(payload["state_dict"])
    model.eval()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    dummy = torch.zeros(1, 1, 30, 70)

    torch.onnx.export(
        model,
        dummy,
        str(output),
        input_names=["image"],
        output_names=["logits"],
        dynamic_axes={"image": {0: "batch"}, "logits": {0: "batch"}},
        opset_version=17,
    )
    print(f"已导出 {output}（{output.stat().st_size / 1024:.0f} KB）")

    # 逐元素比对 torch 与 onnxruntime 的输出，确认导出没有走样
    if ort is None:
        print("当前环境没有 onnxruntime，跳过一致性校验")
        print("用项目环境验证：uv run python train/verify_onnx.py")
        return 0

    sample = torch.rand(4, 1, 30, 70)
    with torch.no_grad():
        expected = model(sample).numpy()
    session = ort.InferenceSession(str(output), providers=["CPUExecutionProvider"])
    actual = session.run(None, {"image": sample.numpy()})[0]
    diff = float(np.abs(expected - actual).max())
    print(f"与 PyTorch 输出最大偏差：{diff:.2e}")
    print(f"验证集整串准确率（存档时记录）：{payload.get('seq_acc', 0):.2%}")
    return 0 if diff < 1e-3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
