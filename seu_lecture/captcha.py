"""验证码识别：对四位数字做 CTC 解码，并用概率裕度筛掉不可靠结果。

学校验证码固定为 4 位数字，但 ddddocr 的字符集有 8210 项，包含 O、o、s、l
这类与数字极易混淆的字符，直接取 `text` 会得到 "066s"、"967O"、"4o30"。

这里做四件事：
1. 用 beta 模型（对印刷体数字更准）
2. 自己按 CTC 贪心解码 `probabilities`（原始张量是 ~19 步 × 8210 维，含 blank 与重复步）
3. 解码出的字符若不是数字（如 'o'），就在该步的 10 个数字索引上重新取最优
4. 用 top1 与次优数字的概率差（裕度）衡量把握，把握不足宁可不提交、换一张

实测 14 张带标注样本：直接用 `text` 是 11/14，本方案 14/14；
1023 张真实样本中 93.5% 能被正确切成 4 位。
"""

from __future__ import annotations

import threading
from typing import Any

from . import log

DIGITS = "0123456789"
# 用集合判断：字符串形式的 `'' in DIGITS` 恒为 True，会把 blank 也算进来
DIGIT_SET = frozenset(DIGITS)
# 默认裕度门槛：低于它说明两个数字难以区分，放弃这次识别而不是硬猜
DEFAULT_MIN_MARGIN = 0.10

_lock = threading.Lock()
_engine: Any = None
_failed = False
_charset: list[str] = []
_digit_ids: list[int] = []
_id2char: dict[int, str] = {}
_blank: int = 0


def warmup() -> bool:
    """提前加载模型并建立数字索引，避免开放瞬间才初始化。"""
    global _engine, _failed, _charset, _digit_ids, _id2char, _blank
    if _engine is not None:
        return True
    if _failed:
        return False
    with _lock:
        if _engine is not None:
            return True
        try:
            import ddddocr

            engine = ddddocr.DdddOcr(show_ad=False, beta=True)
            charset = engine.get_charset()
            # charset[0] 是空串，即 CTC 的 blank
            blank = next(
                (i for i, c in enumerate(charset) if isinstance(c, str) and not c), 0
            )
            # 同一字符可能出现多次，取首次出现的索引
            first: dict[str, int] = {}
            for index, char in enumerate(charset):
                if isinstance(char, str) and len(char) == 1 and char in DIGIT_SET:
                    first.setdefault(char, index)
            if len(first) != 10:
                raise RuntimeError(
                    f"charset 中数字字符异常：找到 {len(first)} 个 {sorted(first)}"
                )
            _charset = list(charset)
            _blank = blank
            _digit_ids = list(first.values())
            _id2char = {index: char for char, index in first.items()}
            _engine = engine
            log.info("验证码识别模型已就绪（beta 模型 + CTC 解码 + 仅数字）")
            return True
        except Exception as exc:
            _failed = True
            log.error(f"验证码识别模型加载失败：{type(exc).__name__} {exc}")
            return False


def _flatten(steps: list[Any]) -> list[list[float]]:
    """把 [[[...]]] 这类多余嵌套摊平成「每步一个概率向量」。"""
    return [step[0] if isinstance(step, list) and len(step) == 1 else step for step in steps]


def _ctc_decode(vectors: list[list[float]]) -> list[int]:
    """CTC 贪心解码：逐步取 argmax，合并连续重复，跳过 blank。返回步序号列表。"""
    kept: list[int] = []
    previous = -1
    for position, vector in enumerate(vectors):
        index = max(range(len(vector)), key=lambda i: vector[i])
        if index == _blank:
            previous = -1
            continue
        if index == previous:
            continue
        previous = index
        kept.append(position)
    return kept


def _pick_digit(vector: list[float], preferred: int | None = None) -> tuple[str, float]:
    """在该步上选出数字；preferred 是全集 argmax 的索引。返回（字符, 裕度）。"""
    if preferred is not None and preferred in _id2char:
        best = preferred
    else:
        best = max(_digit_ids, key=lambda i: vector[i])
    runner = max((vector[i] for i in _digit_ids if i != best), default=0.0)
    return _id2char[best], float(vector[best]) - float(runner)


def recognize(image: bytes, *, min_margin: float = DEFAULT_MIN_MARGIN) -> str | None:
    """识别四位数字验证码；把握不足或位数不符时返回 None，由调用方换码重试。"""
    if not image:
        return None
    if not warmup():
        return None

    try:
        with _lock:  # ddddocr 共享实例没有线程安全保证
            result = _engine.classification(image, probability=True)
    except Exception as exc:
        log.warn(f"验证码识别异常：{type(exc).__name__}")
        return None

    vectors = _flatten(result.get("probabilities") or [])
    if not vectors:
        log.warn("验证码没有返回概率张量，换一张")
        return None

    positions = _ctc_decode(vectors)
    if len(positions) != 4:
        log.warn(f"验证码解出 {len(positions)} 位，不是 4 位，换一张")
        return None

    chars: list[str] = []
    worst = 1.0
    for position in positions:
        vector = vectors[position]
        top = max(range(len(vector)), key=lambda i: vector[i])
        char, margin = _pick_digit(vector, top)
        chars.append(char)
        worst = min(worst, margin)

    if worst < min_margin:
        log.warn(f"验证码把握不足（裕度 {worst:.3f} < {min_margin:.2f}），换一张")
        return None
    return "".join(chars)
