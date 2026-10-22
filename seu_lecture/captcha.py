"""验证码识别：ddddocr 全局单例，加载一次复用。"""

from __future__ import annotations

import threading

from . import log

_lock = threading.Lock()
_engine = None
_failed = False


def warmup() -> bool:
    """提前加载模型，避免开放瞬间才初始化。"""
    global _engine, _failed
    if _engine is not None:
        return True
    if _failed:
        return False
    with _lock:
        if _engine is not None:
            return True
        try:
            from ddddocr import DdddOcr

            _engine = DdddOcr(show_ad=False)
            log.info("验证码识别模型已就绪")
            return True
        except Exception as exc:
            _failed = True
            log.error(f"验证码识别模型加载失败：{type(exc).__name__} {exc}")
            return False


def recognize(image: bytes) -> str | None:
    """识别四位数字验证码；无法确定时返回 None。"""
    if not image:
        return None
    if not warmup():
        return None
    try:
        with _lock:  # ddddocr 共享实例没有线程安全保证
            code = _engine.classification(image).strip()
    except Exception as exc:
        log.warn(f"验证码识别异常：{type(exc).__name__}")
        return None
    if len(code) != 4 or not code.isdigit():
        log.warn(f"验证码识别结果不合规：{code!r}")
        return None
    return code
