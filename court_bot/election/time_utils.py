from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .constants import BEIJING_TZ, MAX_DURATION_MINUTES

BEIJING = ZoneInfo(BEIJING_TZ)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BEIJING)
    return dt.astimezone(timezone.utc)


def to_utc_iso(dt: datetime) -> str:
    return to_utc(dt).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_beijing_datetime(value: str | None) -> datetime:
    text = (value or "").strip()
    if not text:
        return datetime.now(BEIJING)
    patterns = ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y/%m/%d")
    for pattern in patterns:
        try:
            dt = datetime.strptime(text, pattern)
            if pattern in ("%Y-%m-%d", "%Y/%m/%d"):
                dt = dt.replace(hour=0, minute=0)
            return dt.replace(tzinfo=BEIJING)
        except ValueError:
            continue
    raise ValueError("时间格式错误，请使用 YYYY-MM-DD HH:mm，例如 2026-05-01 20:00。")


def parse_duration_minutes(value: str, *, allow_zero: bool = False, label: str = "持续时间") -> int:
    text = (value or "").strip().lower()
    if not text:
        raise ValueError(f"{label}不能为空。")
    if "." in text:
        raise ValueError(f"{label}不支持小数；请改用小时或分钟，例如 36小时。")
    if "月" in text:
        raise ValueError(f"{label}不支持按月填写，因为月份长度不固定。")

    total = 0
    matched = False
    # Supports mixed forms: 2天6小时30分钟, 2d6h30m
    token_re = re.compile(r"(\d+)\s*(天|日|小时|时|分钟|分|d|h|m)", re.IGNORECASE)
    for match in token_re.finditer(text):
        matched = True
        amount = int(match.group(1))
        unit = match.group(2).lower()
        if unit in ("天", "日", "d"):
            total += amount * 24 * 60
        elif unit in ("小时", "时", "h"):
            total += amount * 60
        elif unit in ("分钟", "分", "m"):
            total += amount

    if not matched and text.isdigit():
        # Bare number: treat as hours for convenience.
        total = int(text) * 60
        matched = True

    if not matched:
        raise ValueError(f"无法解析{label}，支持：3天、72小时、2天6小时、3d、12h、30分钟。")
    if total < 0 or (total == 0 and not allow_zero):
        raise ValueError(f"{label}必须{'大于或等于 0' if allow_zero else '大于 0'}。")
    if total > MAX_DURATION_MINUTES:
        raise ValueError(f"{label}过长，单阶段最多 30 天。")
    return total


def add_minutes(dt: datetime, minutes: int) -> datetime:
    return dt + timedelta(minutes=int(minutes))


def format_beijing(value: str | datetime | None, *, fallback: str = "未设置") -> str:
    dt = parse_iso(value) if isinstance(value, str) else value
    if dt is None:
        return fallback
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    bj = dt.astimezone(BEIJING)
    return bj.strftime("%Y-%m-%d %H:%M 北京时间")


def format_discord_ts(value: str | datetime | None, *, style: str = "F", fallback: str = "未设置") -> str:
    dt = parse_iso(value) if isinstance(value, str) else value
    if dt is None:
        return fallback
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:{style}>"


def format_time_pair(value: str | datetime | None) -> str:
    if value is None:
        return "未设置"
    dt = parse_iso(value) if isinstance(value, str) else value
    if dt is None:
        return "未设置"
    return f"{format_beijing(dt)}（{format_discord_ts(dt, style='F')}）"


@dataclass(frozen=True)
class ElectionSchedule:
    registration_start_at: datetime
    registration_end_at: datetime
    voting_start_at: datetime
    voting_end_at: datetime


def build_schedule(
    *,
    start_at_text: str | None,
    registration_duration_minutes: int,
    publicity_duration_minutes: int,
    voting_duration_minutes: int,
) -> ElectionSchedule:
    start = parse_beijing_datetime(start_at_text)
    registration_end = add_minutes(start, registration_duration_minutes)
    voting_start = add_minutes(registration_end, publicity_duration_minutes)
    voting_end = add_minutes(voting_start, voting_duration_minutes)
    return ElectionSchedule(
        registration_start_at=start,
        registration_end_at=registration_end,
        voting_start_at=voting_start,
        voting_end_at=voting_end,
    )


def human_duration(minutes: int) -> str:
    minutes = int(minutes or 0)
    days, rem = divmod(minutes, 24 * 60)
    hours, mins = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if mins:
        parts.append(f"{mins}分钟")
    return "".join(parts) if parts else "0分钟"
