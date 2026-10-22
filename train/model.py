"""四位数字验证码识别网络。

设计取舍：
- 固定长度 4 位，所以不用 CTC，直接四个「十选一」分类头，比通用 OCR 简单得多
- 宽度方向用 AdaptiveAvgPool 强制压成 4 段，让每个位置各自出结果，
  符合「验证码等宽排布」的先验，小数据下比全连接好训
- 四个位置共享同一个 Linear 头：字体风格一致，共享权重参数更少、更抗过拟合
"""

from __future__ import annotations

import torch
from torch import nn

IMAGE_HEIGHT = 30
IMAGE_WIDTH = 70
DIGITS = 4


class CaptchaNet(nn.Module):
    """输入 (B, 1, 30, 70) 灰度图，输出 (B, 4, 10) 的 logits。"""

    def __init__(self, channels: int = 32) -> None:
        super().__init__()
        c = channels
        self.backbone = nn.Sequential(
            nn.Conv2d(1, c, 3, padding=1), nn.BatchNorm2d(c), nn.ReLU(inplace=True),
            nn.MaxPool2d(2),                                  # 15 x 35
            nn.Conv2d(c, c * 2, 3, padding=1), nn.BatchNorm2d(c * 2), nn.ReLU(inplace=True),
            nn.MaxPool2d(2),                                  #  7 x 17
            nn.Conv2d(c * 2, c * 4, 3, padding=1), nn.BatchNorm2d(c * 4), nn.ReLU(inplace=True),
            nn.MaxPool2d(2),                                  #  3 x 8
            nn.Conv2d(c * 4, c * 4, 3, padding=1), nn.BatchNorm2d(c * 4), nn.ReLU(inplace=True),
        )
        # 高度压成 1，宽度压成 4 段，正好对应四个数字位
        self.pool = nn.AdaptiveAvgPool2d((1, DIGITS))
        self.dropout = nn.Dropout(0.2)
        self.head = nn.Linear(c * 4, 10)

    def forward(self, images: torch.Tensor) -> torch.Tensor:
        feat = self.backbone(images)                 # B, C*4, 3, 8
        feat = self.pool(feat)                       # B, C*4, 1, 4
        feat = feat.squeeze(2).transpose(1, 2)       # B, 4, C*4
        return self.head(self.dropout(feat))         # B, 4, 10


def parameter_count(model: nn.Module) -> int:
    return sum(p.numel() for p in model.parameters())
