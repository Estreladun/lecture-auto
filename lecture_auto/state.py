"""任务状态文件：原子写入 + 进程互斥，避免重复运行和重复提交。"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import log

SHANGHAI = ZoneInfo("Asia/Shanghai")
VERSION = 1


class StateError(RuntimeError):
    pass


def _lock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _now() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


class Store:
    """一个账号一份状态，保存在 LEC_STATE_DIR 下。"""

    def __init__(self, directory: Path, username: str, *, lock: bool = True) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.path = directory / "state.json"
        self.account = hashlib.sha256(username.encode()).hexdigest()[:16]
        self._lock_fd: int | None = None
        if lock:
            self._lock_fd = os.open(str(directory / "run.lock"), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                _lock(self._lock_fd)
            except OSError as exc:
                os.close(self._lock_fd)
                self._lock_fd = None
                raise StateError(
                    "同一账号已有实例在运行（state/run.lock 被占用）。"
                    "请先关闭其他窗口，或删除状态目录后重试。"
                ) from exc
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            data = {"version": VERSION, "account": self.account, "created_at": _now(),
                    "successes": {}, "attempts": {}, "mail": {}, "reports": {}}
            self._write(data)
            return data
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"状态文件损坏（{self.path}）：{exc}") from exc
        if not isinstance(data, dict) or data.get("version") != VERSION:
            raise StateError(f"状态文件版本不受支持，请删除后重试：{self.path}")
        if data.get("account") != self.account:
            # 换账号就重置，避免把别人的进度当成自己的
            log.warn("检测到状态文件属于其他账号，已重置")
            data = {"version": VERSION, "account": self.account, "created_at": _now(),
                    "successes": {}, "attempts": {}, "mail": {}, "reports": {}}
            self._write(data)
        for key in ("successes", "attempts", "mail", "reports"):
            data.setdefault(key, {})
        return data

    def _write(self, data: dict[str, Any]) -> None:
        data["updated_at"] = _now()
        fd, temp = tempfile.mkstemp(prefix=".state-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def save(self) -> None:
        self._write(self.data)

    # ── 业务记录 ────────────────────────────────────────────

    def already_done(self, wid: str) -> bool:
        return wid in self.data["successes"]

    def record_success(self, lecture: Any) -> None:
        self.data["successes"][lecture.wid] = {
            "title": lecture.title,
            "start": lecture.start.isoformat() if lecture.start else None,
            "online": lecture.online,
            "at": _now(),
        }
        self.save()

    def bump_attempts(self, wid: str) -> int:
        used = int(self.data["attempts"].get(wid, 0)) + 1
        self.data["attempts"][wid] = used
        self.save()
        return used

    def success_count(self) -> int:
        return len(self.data["successes"])

    def record_mail(self, event: str, status: str) -> None:
        self.data["mail"] = {"at": _now(), "event": event, "status": status}
        self.save()

    def report_pending(self, hour: int) -> bool:
        """当天该时间点后是否还没汇报过。"""
        now = datetime.now(SHANGHAI)
        if now.hour < hour:
            return False
        return self.data["reports"].get("last_date") != now.date().isoformat()

    def mark_reported(self) -> None:
        self.data["reports"] = {
            "last_date": datetime.now(SHANGHAI).date().isoformat(),
            "at": _now(),
        }
        self.save()

    def close(self) -> None:
        if self._lock_fd is None:
            return
        try:
            os.close(self._lock_fd)
        except OSError:
            pass
        self._lock_fd = None
