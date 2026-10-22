"""验证码识别：优先使用专用 CNN 模型，缺失时回退到 ddddocr。

两条路径：

1. 专用模型（seu_lecture/models/captcha.onnx）
   针对四位数字训练的 242K 参数 CNN，直出四位结果，不会输出字母。
   实测单张 0.15 ms，整串准确率 97.9%（东大 14 张标注样本 14/14）。
   推理走 onnxruntime——它本来就是 ddddocr 的依赖，不额外引入任何东西。

2. 回退 ddddocr
   没有模型文件时启用。它的字符集有 8210 项，含 O、o、s、l 等形近字符，
   所以要自己做 CTC 解码 + 只取数字 + 概率裕度过滤，细节见下方各函数。

调试：`python -c "from seu_lecture import captcha; captcha.warmup()"` 会打印当前用哪条路径。
"""

from __future__ import annotations

import io
import threading
from pathlib import Path
from typing import Any

from . import log

MODEL_PATH = Path(__file__).resolve().parent / "models" / "captcha.onnx"
IMAGE_SIZE = (70, 30)  # 与训练输入一致
DIGITS = "0123456789"
# 用集合判断：字符串形式的 `'' in DIGITS` 恒为 True，会把 blank 也算进来
DIGIT_SET = frozenset(DIGITS)
# 默认裕度门槛：低于它说明两个数字难以区分，放弃这次识别而不是硬猜
DEFAULT_MIN_MARGIN = 0.10

_lock = threading.Lock()
_backend: str | None = None
_failed = False

# 专用模型
_session: Any = None
_numpy: Any = None

# ddddocr 回退
_engine: Any = None
_charset: list[str] = []
_digit_ids: list[int] = []
_id2char: dict[int, str] = {}
_blank: int = 0


def warmup() -> bool:
    """加载模型；优先专用 CNN，失败再回退 ddddocr。"""
    global _backend, _failed, _session, _numpy
    global _engine, _charset, _digit_ids, _id2char, _blank
    if _backend is not None:
        return True
    if _failed:
        return False
    with _lock:
        if _backend is not None:
            return True

        if MODEL_PATH.exists():
            try:
                import numpy as np
                import onnxruntime as ort

                _session = ort.InferenceSession(
                    str(MODEL_PATH), providers=["CPUExecutionProvider"]
                )
                _numpy = np
                _backend = "onnx"
                size = MODEL_PATH.stat().st_size // 1024
                log.info(f"验证码模型已就绪（专用 CNN，{size} KB）")
                return True
            except Exception as exc:
                log.warn(f"专用模型加载失败（{type(exc).__name__}: {exc}），回退 ddddocr")

        try:
            import ddddocr

            engine = ddddocr.DdddOcr(show_ad=False, beta=True)
            charset = list(engine.get_charset())
            blank = next(
                (i for i, c in enumerate(charset) if isinstance(c, str) and not c), 0
            )
            first: dict[str, int] = {}
            for index, char in enumerate(charset):
                if isinstance(char, str) and len(char) == 1 and char in DIGIT_SET:
                    first.setdefault(char, index)
            if len(first) != 10:
                raise RuntimeError(f"charset 中数字字符异常：{len(first)} 个")
            _engine = engine
            _charset = charset
            _blank = blank
            _digit_ids = list(first.values())
            _id2char = {index: char for char, index in first.items()}
            _backend = "ddddocr"
            log.info("验证码模型已就绪（ddddocr 回退 + CTC 解码 + 仅数字）")
            return True
        except Exception as exc:
            _failed = True
            log.error(f"验证码识别模块初始化失败：{type(exc).__name__} {exc}")
            return False


def backend() -> str | None:
    """当前使用的后端，未初始化时为 None。"""
    return _backend


# ── 专用 CNN 路径 ───────────────────────────────────────────

def _to_input(image: bytes) -> Any:
    from PIL import Image

    picture = Image.open(io.BytesIO(image)).convert("L")
    if picture.size != IMAGE_SIZE:
        picture = picture.resize(IMAGE_SIZE, Image.BILINEAR)
    array = _numpy.asarray(picture, dtype=_numpy.float32) / 255.0
    array = (array - 0.5) / 0.5
    return array[None, None, :, :]


def _analyze_model(image: bytes) -> tuple[str, float] | None:
    array = _to_input(image)
    logits = _session.run(None, {"image": array})[0][0]
    shifted = logits - logits.max(axis=-1, keepdims=True)
    exp = _numpy.exp(shifted)
    probs = exp / exp.sum(axis=-1, keepdims=True)

    digits = probs.argmax(axis=1)
    worst = 1.0
    for index in range(len(digits)):
        ordered = _numpy.sort(probs[index])
        worst = min(worst, float(ordered[-1] - ordered[-2]))
    return "".join(str(int(d)) for d in digits), worst


# ── ddddocr 回退路径 ────────────────────────────────────────

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
    if preferred is not None and preferred in _id2char:
        best = preferred
    else:
        best = max(_digit_ids, key=lambda i: vector[i])
    runner = max((vector[i] for i in _digit_ids if i != best), default=0.0)
    return _id2char[best], float(vector[best]) - float(runner)


def _split_four(vectors: list[list[float]]) -> tuple[str, float]:
    """兜底：把步序列均分四段，每段在数字维度上求和后取最优。"""
    n = len(vectors)
    edges = [round(n * k / 4) for k in range(5)]
    chars: list[str] = []
    worst = 1.0
    for start, stop in zip(edges, edges[1:]):
        segment = vectors[start:stop] or [vectors[min(start, n - 1)]]
        totals = dict.fromkeys(_digit_ids, 0.0)
        for vector in segment:
            for index in _digit_ids:
                totals[index] += vector[index]
        best = max(_digit_ids, key=lambda i: totals[i])
        runner = max((totals[i] for i in _digit_ids if i != best), default=0.0)
        chars.append(_id2char[best])
        overall = sum(totals.values())
        if overall > 0:
            worst = min(worst, (totals[best] - runner) / overall)
    return "".join(chars), worst


def _analyze_ddddocr(image: bytes, *, rescue: bool) -> tuple[str, float] | None:
    result = _engine.classification(image, probability=True)
    vectors = _flatten(result.get("probabilities") or [])
    if not vectors:
        return None

    positions = _ctc_decode(vectors)
    if len(positions) != 4:
        if not rescue:
            return None
        return _split_four(vectors)

    chars: list[str] = []
    worst = 1.0
    for position in positions:
        vector = vectors[position]
        top = max(range(len(vector)), key=lambda i: vector[i])
        char, margin = _pick_digit(vector, top)
        chars.append(char)
        worst = min(worst, margin)
    return "".join(chars), worst


# ── 对外接口 ────────────────────────────────────────────────

def analyze(image: bytes, *, rescue: bool = False) -> tuple[str, float] | None:
    """解出四位数字并返回（结果, 裕度）；解不出四位时返回 None。

    裕度是代码里最小的「最优数字 - 次优数字」概率差，值越大越可信。
    不做门槛判断，交由调用方决定。
    """
    if not image or not warmup():
        return None
    try:
        with _lock:  # 两个后端都不是线程安全的
            if _backend == "onnx":
                return _analyze_model(image)
            return _analyze_ddddocr(image, rescue=rescue)
    except Exception as exc:
        log.warn(f"验证码识别异常：{type(exc).__name__}")
        return None


def recognize(image: bytes, *, min_margin: float = DEFAULT_MIN_MARGIN,
              rescue: bool = False) -> str | None:
    """识别四位数字验证码。

    Args:
        min_margin: 裕度门槛，低于它视为不可靠并返回 None
        rescue: ddddocr 后端下，解不出四位时是否强行按四段均分给出结果
    """
    outcome = analyze(image, rescue=rescue)
    if outcome is None:
        if image:
            log.warn("验证码解不出四位，换一张")
        return None

    code, margin = outcome
    if margin < min_margin:
        log.warn(f"验证码把握不足（裕度 {margin:.3f} < {min_margin:.2f}），换一张")
        return None
    return code
