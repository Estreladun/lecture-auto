"""讲座数据模型与筛选规则。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")

_ONLINE_HINTS = ("腾讯会议", "线上", "钉钉", "直播", "zoom", "webex", "bilibili")
_CAMPUS_PATTERN = re.compile(r"^(.+?校区)")


def parse_time(value: Any) -> datetime | None:
    """兼容毫秒时间戳、秒时间戳与常见日期字符串。"""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        stamp = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(stamp, SHANGHAI)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return parse_time(int(text))
    normalized = text.replace("/", "-").replace("T", " ")
    try:
        if normalized.endswith("Z"):
            parsed = datetime.fromisoformat(normalized[:-1] + "+00:00")
        else:
            parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Lecture:
    wid: str
    title: str
    category: str
    location: str
    online: bool
    reserve_start: datetime | None
    reserve_end: datetime | None
    start: datetime | None
    end: datetime | None
    reserved: bool
    used: int | None
    capacity: int | None

    @property
    def campus(self) -> str:
        match = _CAMPUS_PATTERN.match(self.location)
        return match.group(1) if match else self.location

    @property
    def full(self) -> bool:
        if self.used is None or self.capacity is None:
            return False
        return self.used >= self.capacity

    @property
    def seat_text(self) -> str:
        if self.used is None or self.capacity is None:
            return "余量未知"
        return f"{self.used}/{self.capacity}"

    @classmethod
    def parse(cls, row: dict[str, Any]) -> "Lecture | None":
        wid = row.get("HD_WID") or row.get("WID")
        title = row.get("JZMC") or row.get("HDMC") or row.get("title")
        if not isinstance(wid, str) or not wid or not isinstance(title, str) or not title.strip():
            return None

        location = row.get("JZDD") or row.get("HDDD") or ""
        if not isinstance(location, str):
            location = ""

        if title.startswith("【线上】"):
            online = True
        elif title.startswith("【线下】"):
            online = False
        else:
            lowered = location.lower()
            online = any(hint in lowered for hint in _ONLINE_HINTS)

        return cls(
            wid=wid,
            title=title.strip(),
            category=str(row.get("JZXL_DISPLAY") or row.get("HDLX_DISPLAY") or ""),
            location=location.strip(),
            online=online,
            reserve_start=parse_time(row.get("YYKSSJ")),
            reserve_end=parse_time(row.get("YYJSSJ")),
            start=parse_time(row.get("KSSJ") or row.get("HDKSSJ") or row.get("JZSJ")),
            end=parse_time(row.get("JSSJ") or row.get("HDJSSJ")),
            reserved=bool(row.get("YY_WID")),
            used=_as_int(row.get("YYRS")),
            capacity=_as_int(row.get("HDZRS")),
        )

    def __str__(self) -> str:
        when = f"{self.start:%m-%d %H:%M}" if self.start else "时间未知"
        window = ""
        if self.reserve_start and self.reserve_end:
            same_day = self.reserve_start.date() == self.reserve_end.date()
            tail = (
                f"{self.reserve_end:%H:%M}"
                if same_day
                else f"{self.reserve_end:%m-%d %H:%M}"
            )
            window = f" | 预约 {self.reserve_start:%m-%d %H:%M}~{tail}"
        place = "线上" if self.online else (self.campus or "线下")
        return f"{self.title}（{when}，{place}，{self.seat_text}）{window}"


def matches(lecture: Lecture, *, keywords: tuple[str, ...], categories: tuple[str, ...],
            campuses: tuple[str, ...], online: bool, offline: bool) -> bool:
    """全部条件为「与」关系；空条件表示不限制。"""
    if lecture.online and not online:
        return False
    if not lecture.online and not offline:
        return False
    if keywords and not any(word in lecture.title for word in keywords):
        return False
    if categories and not any(word in lecture.category for word in categories):
        return False
    if campuses and not any(word in (lecture.campus, lecture.location) for word in campuses):
        return False
    return True


def select(rows: list[dict[str, Any]], settings: Any, now: datetime | None = None) -> list[Lecture]:
    """从原始列表里挑出待抢目标，按预约开放时间排序。"""
    now = now or datetime.now(SHANGHAI)
    chosen: list[Lecture] = []
    seen: set[str] = set()

    for row in rows:
        lecture = Lecture.parse(row)
        if lecture is None or lecture.wid in seen:
            continue
        if lecture.reserved:
            continue
        if lecture.reserve_end and now > lecture.reserve_end:
            continue
        if not matches(
            lecture,
            keywords=settings.keywords,
            categories=settings.categories,
            campuses=settings.campuses,
            online=settings.online,
            offline=settings.offline,
        ):
            continue
        seen.add(lecture.wid)
        chosen.append(lecture)

    chosen.sort(key=lambda item: (item.reserve_start or datetime.max.replace(tzinfo=SHANGHAI), item.wid))
    return chosen
