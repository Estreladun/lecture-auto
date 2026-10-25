"""统一日志输出：终端带时间戳，同时按天落盘。"""

from __future__ import annotations

import sys
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")

_lock = threading.Lock()
_log_dir: Path | None = None
_warned: set[str] = set()


def setup(directory: Path | None) -> None:
    global _log_dir
    _log_dir = directory
    if _log_dir is not None:
        _log_dir.mkdir(parents=True, exist_ok=True)


def log(message: str, *, level: str = "INFO") -> None:
    now = datetime.now(SHANGHAI)
    # 带日期：一次运行常跨好几天（讲座分散在不同日子），只给时分秒会看不出是哪天
    line = f"[{now:%Y-%m-%d %H:%M:%S}] {level:<5} {message}"
    with _lock:
        print(line, flush=True)
        if _log_dir is None:
            return
        try:
            path = _log_dir / f"{now:%Y-%m-%d}.log"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"[{now:%Y-%m-%d %H:%M:%S}] {level:<5} {message}\n")
        except OSError as exc:  # 日志写不进去不能中断主流程
            if str(exc) not in _warned:
                _warned.add(str(exc))
                print(f"日志写入失败：{exc}", file=sys.stderr, flush=True)


def info(message: str) -> None:
    log(message)


def warn(message: str) -> None:
    log(message, level="WARN")


def error(message: str) -> None:
    log(message, level="ERROR")
