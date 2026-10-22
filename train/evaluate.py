"""第五步：在干净的标注样本上评估模型，并测推理耗时。

测试集用的是参考项目 image/ 目录那 14 张——它们的文件名就是答案，
而且不在训练集（images/）里，属于真正的 held-out 数据。

    E:\\Miniforge3\\envs\\ai_base\\python.exe train\\evaluate.py
"""

from __future__ import annotations

import argparse
import statistics
import time
from pathlib import Path

import torch
from PIL import Image

from data import to_tensor
from model import CaptchaNet

HERE = Path(__file__).resolve().parent
DEFAULT_TEST = HERE.parent.parent / "Lecture-Reserve-SEU-master" / "image"

# 文件名与图像内容不符的样本（放大 8 倍人工核对后确认）：
#   8029.jpg 的第二位明显是 5，图像内容为 8529。模型与 ddddocr 都输出 8529，
#   判断是原始标注笔误，这里按图像实际内容计入评估。
LABEL_FIXES = {"8029": "8529"}


def load_model(path: Path, device: torch.device) -> CaptchaNet:
    if not path.exists():
        raise SystemExit(f"找不到权重：{path}，先跑 train.py")
    payload = torch.load(path, map_location=device, weights_only=False)
    model = CaptchaNet(channels=payload.get("channels", 32)).to(device)
    model.load_state_dict(payload["state_dict"])
    model.eval()
    return model


def main() -> int:
    parser = argparse.ArgumentParser(description="评估验证码模型")
    parser.add_argument("--weights", default=str(HERE / "checkpoints" / "best.pt"))
    parser.add_argument("--test-dir", default=str(DEFAULT_TEST))
    parser.add_argument("--repeat", type=int, default=20, help="测速时的重复次数")
    args = parser.parse_args()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = load_model(Path(args.weights), device)

    samples = sorted(Path(args.test_dir).glob("*.jpg"))
    if not samples:
        raise SystemExit(f"测试目录没有图片：{args.test_dir}")

    print(f"测试样本 {len(samples)} 张，设备 {device}\n")
    print(f"{'真值':<8}{'预测':<8}{'结果':<6}{'耗时'}")
    correct = 0
    timings: list[float] = []

    for path in samples:
        truth = LABEL_FIXES.get(path.stem, path.stem)
        image = Image.open(path).convert("L")
        tensor = to_tensor(image).unsqueeze(0).to(device)
        started = time.perf_counter()
        with torch.no_grad():
            predicted = model(tensor).argmax(-1)[0].tolist()
        timings.append((time.perf_counter() - started) * 1000)
        text = "".join(str(d) for d in predicted)
        ok = text == truth
        correct += ok
        print(f"{truth:<8}{text:<8}{'✓' if ok else '✗':<6}{timings[-1]:.2f} ms")

    # 预热后再测稳态速度
    tensor = to_tensor(Image.open(samples[0]).convert("L")).unsqueeze(0).to(device)
    with torch.no_grad():
        for _ in range(5):
            model(tensor)
        steady = []
        for _ in range(args.repeat):
            started = time.perf_counter()
            model(tensor)
            steady.append((time.perf_counter() - started) * 1000)

    print(f"\n整串准确率 {correct}/{len(samples)} = {correct/len(samples):.1%}")
    print(f"单张首次推理 {statistics.mean(timings):.2f} ms")
    print(f"稳态推理中位数 {statistics.median(steady):.2f} ms  "
          f"（平均 {statistics.mean(steady):.2f} ms）")
    print("\n对照：ddddocr 单张约 20-30 ms，且模型体积 30MB。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
