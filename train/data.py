"""数据加载与增强。

只有一千多张真实样本，增强是决定成败的关键：
- 仿射类（旋转/平移/缩放）模拟字符位置和粗细的天然差异
- 光度类（亮度/对比度/噪声）模拟截图压缩与干扰线强度变化
不做镜像翻转：数字 2、3、5 翻转后就不再是合法数字。
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import numpy as np
import torch
from PIL import Image, ImageEnhance
from torch.utils.data import Dataset

IMAGE_HEIGHT = 30
IMAGE_WIDTH = 70
DIGITS = 4
BACKGROUND = 246  # 样本背景接近纯白，仿射填充用这个值避免引入黑边


def load_label_map(path: Path) -> dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(f"找不到标签文件：{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    valid: dict[str, str] = {}
    for name, label in data.items():
        label = str(label).strip()
        if len(label) == DIGITS and label.isdigit():
            valid[name] = label
    return valid


def _rotate(image: Image.Image, rng: random.Random) -> Image.Image:
    if rng.random() >= 0.7:
        return image
    return image.rotate(
        rng.uniform(-6, 6), resample=Image.BILINEAR, fillcolor=BACKGROUND
    )


def _shift(image: Image.Image, rng: random.Random) -> Image.Image:
    if rng.random() >= 0.7:
        return image
    dx, dy = rng.uniform(-2.5, 2.5), rng.uniform(-1.5, 1.5)
    return image.transform(
        image.size, Image.AFFINE, (1, 0, dx, 0, 1, dy), fillcolor=BACKGROUND
    )


def _scale(image: Image.Image, rng: random.Random) -> Image.Image:
    if rng.random() >= 0.5:
        return image
    factor = rng.uniform(0.90, 1.10)
    width, height = image.size
    scaled = image.resize(
        (max(1, int(width * factor)), max(1, int(height * factor))), Image.BILINEAR
    )
    return scaled.resize((width, height), Image.BILINEAR)


def _photometric(image: Image.Image, rng: random.Random) -> Image.Image:
    if rng.random() < 0.6:
        image = ImageEnhance.Brightness(image).enhance(rng.uniform(0.75, 1.25))
    if rng.random() < 0.6:
        image = ImageEnhance.Contrast(image).enhance(rng.uniform(0.75, 1.30))
    if rng.random() < 0.4:
        array = np.asarray(image, dtype=np.float32)
        noise = np.random.normal(0, rng.uniform(3, 10), array.shape)
        image = Image.fromarray(np.clip(array + noise, 0, 255).astype(np.uint8))
    return image


def augment(image: Image.Image, rng: random.Random) -> Image.Image:
    image = _rotate(image, rng)
    image = _shift(image, rng)
    image = _scale(image, rng)
    return _photometric(image, rng)


def to_tensor(image: Image.Image) -> torch.Tensor:
    array = np.asarray(image.convert("L"), dtype=np.float32) / 255.0
    array = (array - 0.5) / 0.5
    return torch.from_numpy(array).unsqueeze(0)


class CaptchaDataset(Dataset):
    def __init__(self, items: list[tuple[Path, str]], *, train: bool,
                 seed: int = 0) -> None:
        self.items = items
        self.train = train
        self.seed = seed

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int):
        path, label = self.items[index]
        image = Image.open(path).convert("L")
        if self.train:
            # 每条样本一个独立种子，保证多轮之间增强组合不同、但可复现
            rng = random.Random(self.seed + index * 100003 + random.randrange(1 << 30))
            image = augment(image, rng)
        target = torch.tensor([int(char) for char in label], dtype=torch.long)
        return to_tensor(image), target


def build_items(label_map: dict[str, str], image_dir: Path) -> list[tuple[Path, str]]:
    items: list[tuple[Path, str]] = []
    missing = 0
    for name, label in sorted(label_map.items()):
        path = image_dir / name
        if path.exists():
            items.append((path, label))
        else:
            missing += 1
    if missing:
        print(f"警告：{missing} 个标签找不到对应图片，已跳过")
    return items


def split_items(items: list[tuple[Path, str]], ratio: float = 0.15,
                seed: int = 42) -> tuple[list, list]:
    shuffled = items[:]
    random.Random(seed).shuffle(shuffled)
    cut = max(1, int(len(shuffled) * ratio))
    return shuffled[cut:], shuffled[:cut]
