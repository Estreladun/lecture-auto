"""第四步：训练四位数字验证码模型。

用 CPU 就能跑完：模型不到 1M 参数，输入 30x70，一千多张样本几十轮只要几分钟。

    E:\\Miniforge3\\envs\\ai_base\\python.exe train\\train.py

产物：train/checkpoints/best.pt
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import torch
from torch import nn
from torch.utils.data import DataLoader

from data import CaptchaDataset, build_items, load_label_map, split_items
from model import CaptchaNet, parameter_count

HERE = Path(__file__).resolve().parent


def evaluate(model: nn.Module, loader: DataLoader, device: torch.device) -> tuple[float, float]:
    model.eval()
    seq_ok = char_ok = total = 0
    with torch.no_grad():
        for images, targets in loader:
            images, targets = images.to(device), targets.to(device)
            predicted = model(images).argmax(-1)
            seq_ok += int((predicted == targets).all(dim=1).sum())
            char_ok += int((predicted == targets).sum())
            total += targets.numel() // 4
    return seq_ok / max(total, 1), char_ok / max(total * 4, 1)


def main() -> int:
    parser = argparse.ArgumentParser(description="训练验证码模型")
    parser.add_argument("--epochs", type=int, default=60)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--channels", type=int, default=32)
    parser.add_argument("--val-ratio", type=float, default=0.15)
    parser.add_argument("--workers", type=int, default=0, help="Windows 下建议保持 0")
    args = parser.parse_args()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    torch.manual_seed(42)

    label_map = load_label_map(HERE / "data" / "labels.json")
    items = build_items(label_map, HERE / "data" / "images")
    if len(items) < 100:
        raise SystemExit(f"样本太少（{len(items)}），先完成标注")

    train_items, val_items = split_items(items, ratio=args.val_ratio)
    print(f"训练 {len(train_items)} 张 / 验证 {len(val_items)} 张  设备 {device}")

    train_loader = DataLoader(
        CaptchaDataset(train_items, train=True), batch_size=args.batch_size,
        shuffle=True, num_workers=args.workers, drop_last=len(train_items) > args.batch_size,
    )
    val_loader = DataLoader(
        CaptchaDataset(val_items, train=False), batch_size=args.batch_size,
        num_workers=args.workers,
    )

    model = CaptchaNet(channels=args.channels).to(device)
    print(f"参数量 {parameter_count(model):,}")

    criterion = nn.CrossEntropyLoss(label_smoothing=0.05)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.OneCycleLR(
        optimizer, max_lr=args.lr, total_steps=args.epochs * max(len(train_loader), 1)
    )

    best = 0.0
    checkpoint = HERE / "checkpoints" / "best.pt"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()

    for epoch in range(1, args.epochs + 1):
        model.train()
        running = 0.0
        for images, targets in train_loader:
            images, targets = images.to(device), targets.to(device)
            logits = model(images)
            loss = criterion(logits.reshape(-1, 10), targets.reshape(-1))
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            scheduler.step()
            running += float(loss) * images.size(0)
        train_loss = running / max(len(train_items), 1)

        seq_acc, char_acc = evaluate(model, val_loader, device)
        marker = ""
        if seq_acc > best:
            best = seq_acc
            torch.save(
                {
                    "state_dict": model.state_dict(),
                    "channels": args.channels,
                    "seq_acc": seq_acc,
                    "char_acc": char_acc,
                },
                checkpoint,
            )
            marker = "  <- 保存"
        print(
            f"轮 {epoch:3d}/{args.epochs}  loss {train_loss:.4f}  "
            f"整串 {seq_acc:.2%}  单字 {char_acc:.2%}{marker}"
        )

    elapsed = time.time() - started
    print(f"\n训练完成，耗时 {elapsed/60:.1f} 分钟，最佳整串准确率 {best:.2%}")
    print(f"权重：{checkpoint}")
    print("下一步：uv run python train/evaluate.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
