"""环境变量统一管理：所有可调项集中在这里解析与校验。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv

from .client import Endpoints

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


class ConfigError(ValueError):
    """配置缺失或格式非法。"""


# ── 基础解析 ────────────────────────────────────────────────

def _text(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _flag(key: str, default: bool) -> bool:
    raw = _text(key)
    if not raw:
        return default
    lowered = raw.lower()
    if lowered in ("1", "true", "yes", "on", "y"):
        return True
    if lowered in ("0", "false", "no", "off", "n"):
        return False
    raise ConfigError(f"{key} 应为布尔值（true/false），当前为 {raw!r}")


def _number(key: str, default: float, *, minimum: float, cast: type = float) -> Any:
    raw = _text(key)
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        raise ConfigError(f"{key} 应为数字，当前为 {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{key} 不能小于 {minimum:g}，当前为 {value:g}")
    return value


def _url(key: str, label: str | None, *, required: bool = True) -> str:
    raw = _text(key)
    if not raw:
        if required:
            raise ConfigError(
                f"未配置 {key}（{label}）。请参考 .env.example 填写要接入的校园系统地址"
            )
        return ""
    if not raw.startswith(("http://", "https://")):
        raise ConfigError(f"{key} 必须以 http:// 或 https:// 开头，当前为 {raw!r}")
    if not urlsplit(raw).hostname:
        raise ConfigError(f"{key} 不是合法地址，当前为 {raw!r}")
    return raw


def _list(key: str) -> tuple[str, ...]:
    """JSON 数组；也容忍逗号分隔写法。"""
    raw = _text(key)
    if not raw:
        return ()
    if raw.startswith("["):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{key} 不是合法的 JSON 数组：{exc}") from None
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConfigError(f"{key} 必须是字符串数组")
        return tuple(item.strip() for item in value if item.strip())
    return tuple(part.strip() for part in raw.split(",") if part.strip())


# ── 邮件配置 ────────────────────────────────────────────────

_SMTP_HOSTS = {
    "qq.com": "smtp.qq.com",
    "foxmail.com": "smtp.qq.com",
    "163.com": "smtp.163.com",
    "126.com": "smtp.126.com",
    "gmail.com": "smtp.gmail.com",
    "outlook.com": "smtp.office365.com",
    "hotmail.com": "smtp.office365.com",
}


@dataclass(frozen=True)
class Smtp:
    host: str
    port: int
    username: str
    password: str
    sender: str
    recipient: str

    @classmethod
    def load(cls) -> "Smtp | None":
        username = _text("SMTP_USERNAME")
        # 授权码/应用专用密码由服务商固定生成，首尾空格只可能是复制带来的垃圾，统一清掉
        password = _text("SMTP_PASSWORD")
        # Gmail 应用专用密码常被复制成「4 组 4 位」的展示格式，实际认证必须去掉空格
        compact = password.replace(" ", "")
        if compact != password and re.fullmatch(r"[A-Za-z0-9]{16}", compact):
            password = compact
        if not username and not password:
            return None
        if not username or not password:
            raise ConfigError("SMTP_USERNAME 与 SMTP_PASSWORD 需要同时填写")
        host = _text("SMTP_HOST") or _SMTP_HOSTS.get(username.rpartition("@")[2].lower(), "")
        if not host:
            raise ConfigError(
                f"无法从 {username!r} 推断 SMTP 服务器，请显式设置 SMTP_HOST"
            )
        port = _number("SMTP_PORT", 465, minimum=1, cast=int)
        sender = _text("MAIL_FROM", username)
        recipient = _text("MAIL_TO", sender)
        return cls(host, port, username, password, sender, recipient)


# ── 主配置 ──────────────────────────────────────────────────

@dataclass(frozen=True)
class Settings:
    username: str
    password: str
    entry: str
    cas_base: str
    gateway: str
    gateway_ip: str
    api_bases: tuple[str, ...]
    keywords: tuple[str, ...]
    categories: tuple[str, ...]
    campuses: tuple[str, ...]
    online: bool
    offline: bool
    poll_seconds: float
    burst_attempts: int
    keepalive_seconds: float
    prefetch_seconds: float
    max_attempts: int
    state_dir: Path
    smtp: Smtp | None
    report_hour: int
    use_proxy: bool

    @property
    def endpoints(self) -> Endpoints:
        return Endpoints(
            entry=self.entry,
            cas=self.cas_base,
            gateway=self.gateway,
            gateway_ip=self.gateway_ip,
            api_bases=self.api_bases,
        )

    @classmethod
    def load(cls, env_path: Path | None = None, *, require_credentials: bool = True) -> "Settings":
        path = env_path or DEFAULT_ENV_PATH
        if path.exists():
            # 不覆盖已存在的进程环境变量，保持「进程变量优先」。
            load_dotenv(path, override=False)

        username = _text("SEU_USERNAME")
        # 密码不做 strip：首尾空格可能是密码的一部分，用单引号写入时必须原样保留
        password = os.environ.get("SEU_PASSWORD", "")
        if require_credentials and (not username or not password.strip()):
            raise ConfigError(
                f"未配置账号或密码。请复制 .env.example 为 .env 并填写 "
                f"SEU_USERNAME / SEU_PASSWORD（期望路径：{path}）"
            )

        entry = _url("SEU_ENTRY_URL", "教务系统入口地址")
        cas_base = _url("SEU_CAS_BASE", "统一认证接口前缀")
        if not cas_base.endswith("/"):
            cas_base += "/"
        gateway = _url("SEU_GATEWAY", None, required=False).rstrip("/")
        gateway_ip = _text("SEU_GATEWAY_IP")
        api_bases = tuple(item.rstrip("/") for item in _list("SEU_API_BASES"))

        online = _flag("SEU_ENABLE_ONLINE", True)
        offline = _flag("SEU_ENABLE_OFFLINE", True)
        if not online and not offline:
            raise ConfigError("SEU_ENABLE_ONLINE 与 SEU_ENABLE_OFFLINE 至少需要一个为 true")

        state_dir = Path(_text("SEU_STATE_DIR", "state")).expanduser()
        if not state_dir.is_absolute():
            state_dir = PROJECT_ROOT / state_dir

        return cls(
            username=username,
            password=password,
            entry=entry,
            cas_base=cas_base,
            gateway=gateway,
            gateway_ip=gateway_ip,
            api_bases=api_bases,
            keywords=_list("SEU_TARGET_KEYWORDS"),
            categories=_list("SEU_REQUIRED_CATEGORIES"),
            campuses=_list("SEU_PREFERRED_CAMPUS"),
            online=online,
            offline=offline,
            poll_seconds=_number("SEU_POLL_SECONDS", 0.2, minimum=0.05),
            burst_attempts=_number("SEU_BURST_ATTEMPTS", 5, minimum=1, cast=int),
            keepalive_seconds=_number("SEU_KEEPALIVE_SECONDS", 30, minimum=5),
            prefetch_seconds=_number("SEU_PREFETCH_SECONDS", 2, minimum=0),
            max_attempts=_number("SEU_MAX_ATTEMPTS", 0, minimum=0, cast=int),
            state_dir=state_dir,
            smtp=Smtp.load(),
            report_hour=_number("SEU_MAIL_REPORT_HOUR", 20, minimum=0, cast=int),
            use_proxy=_flag("SEU_USE_PROXY", False),
        )

    def describe(self) -> str:
        """启动摘要，绝不包含凭据。"""
        def show(items: tuple[str, ...]) -> str:
            return "、".join(items) if items else "不限"

        modes = "、".join(
            name for name, enabled in (("线上", self.online), ("线下", self.offline)) if enabled
        )
        host = urlsplit(self.entry).hostname or self.entry
        return (
            f"账号 {self.username[:3]}***  形式 {modes}\n"
            f"站点 {host}\n"
            f"关键词 {show(self.keywords)}\n"
            f"分类 {show(self.categories)}\n"
            f"校区 {show(self.campuses)}\n"
            f"代理 {'跟随系统' if self.use_proxy else '直连'}\n"
            f"邮件 {'已启用' if self.smtp else '未启用'}"
        )
