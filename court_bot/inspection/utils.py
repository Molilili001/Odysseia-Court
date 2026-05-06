from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable

import discord


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def datetime_to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_dt(value: str | datetime | None, *, fallback: str = "（未设置）") -> str:
    if value is None:
        return fallback
    dt = parse_iso(value) if isinstance(value, str) else value
    if dt is None:
        return fallback
    ts = int(dt.timestamp())
    return f"<t:{ts}:F>（<t:{ts}:R>）"


def is_server_admin(member: discord.Member) -> bool:
    return bool(member.guild.owner_id == member.id or member.guild_permissions.administrator)


def trim_text(text: str | None, max_len: int = 1000) -> str:
    value = (text or "").strip()
    if len(value) <= max_len:
        return value
    return value[: max_len - 1] + "…"


def sanitize_channel_name(text: str, *, prefix: str = "监察") -> str:
    raw = re.sub(r"\s+", "-", text.strip())
    raw = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "", raw)
    raw = raw.strip("-_")
    if not raw:
        raw = prefix
    return raw[:90]


def mention_user(user_id: int) -> str:
    return f"<@{int(user_id)}>"


def mention_users(user_ids: Iterable[int]) -> str:
    values = [mention_user(uid) for uid in user_ids]
    return "、".join(values) if values else "（无）"


def channel_mention(channel_id: int | None) -> str:
    return f"<#{int(channel_id)}>" if channel_id else "（未设置）"


def role_mention(role_id: int | None) -> str:
    return f"<@&{int(role_id)}>" if role_id else "（未设置）"


def human_status(status: str | None) -> str:
    mapping = {
        "active": "有效",
        "confirm_dm_failed": "留任确认 DM 失败",
        "removed": "已移除",
        "self_exit": "主动退出",
        "collecting_responses": "收集响应中",
        "ban_pending": "等待 Ban / 抽取",
        "blocked_insufficient_responses": "响应人数不足",
        "active_discussion": "讨论中",
        "voting": "投票中",
        "verdict_published": "已公示裁决",
        "cancelled": "已取消",
        "invited": "已邀请",
        "willing": "愿意参与",
        "declined": "已拒绝",
        "dm_failed": "DM 失败",
        "selected": "已抽中",
        "not_selected": "未抽中",
        "banned": "已被 Ban",
        "replaced": "已被替换",
    }
    return mapping.get(status or "", status or "未知")


def normalize_ids(values: Iterable[int | None]) -> list[int]:
    out: list[int] = []
    for value in values:
        if value is None:
            continue
        out.append(int(value))
    return out
