from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import discord
from discord.ext import commands

from .constants import (
    DEFAULT_RETENTION_DAYS,
    REQUIRED_SETTING_KEYS,
    SETTING_ADMIN_NOTICE_CHANNEL_ID,
    SETTING_ARCHIVE_CHANNEL_ID,
    SETTING_CANDIDATE_ROLE_ID,
    SETTING_DISCUSSION_CATEGORY_ID,
    SETTING_RETENTION_DAYS,
    SETTING_VERDICT_CHANNEL_ID,
)
from .database import InspectionDatabase, ensure_default_settings
from .utils import channel_mention, role_mention, utc_now_iso


_SETTING_LABELS = {
    SETTING_CANDIDATE_ROLE_ID: "监察候补身份组",
    SETTING_ADMIN_NOTICE_CHANNEL_ID: "admin 通知频道",
    SETTING_DISCUSSION_CATEGORY_ID: "临时讨论频道分类",
    SETTING_VERDICT_CHANNEL_ID: "裁决公示频道",
    SETTING_RETENTION_DAYS: "留任确认周期天数",
    SETTING_ARCHIVE_CHANNEL_ID: "归档频道（可选）",
}


@dataclass(slots=True)
class InspectionSettings:
    guild_id: int
    candidate_role_id: int | None = None
    admin_notice_channel_id: int | None = None
    discussion_category_id: int | None = None
    verdict_channel_id: int | None = None
    retention_days: int = DEFAULT_RETENTION_DAYS
    archive_channel_id: int | None = None

    @property
    def is_complete(self) -> bool:
        return not self.missing_keys()

    def missing_keys(self) -> list[str]:
        missing: list[str] = []
        if not self.candidate_role_id:
            missing.append(SETTING_CANDIDATE_ROLE_ID)
        if not self.admin_notice_channel_id:
            missing.append(SETTING_ADMIN_NOTICE_CHANNEL_ID)
        if not self.discussion_category_id:
            missing.append(SETTING_DISCUSSION_CATEGORY_ID)
        if not self.verdict_channel_id:
            missing.append(SETTING_VERDICT_CHANNEL_ID)
        if self.retention_days <= 0:
            missing.append(SETTING_RETENTION_DAYS)
        return missing

    def missing_labels(self) -> list[str]:
        return [_SETTING_LABELS.get(key, key) for key in self.missing_keys()]

    def render(self) -> str:
        return (
            "当前监察模块设置：\n"
            f"- 监察候补身份组：{role_mention(self.candidate_role_id)}\n"
            f"- admin 通知频道：{channel_mention(self.admin_notice_channel_id)}\n"
            f"- 临时讨论频道分类：`{self.discussion_category_id or '未设置'}`\n"
            f"- 裁决公示频道：{channel_mention(self.verdict_channel_id)}\n"
            f"- 留任确认周期天数：`{self.retention_days}`\n"
            f"- 归档频道（可选）：{channel_mention(self.archive_channel_id)}"
        )


class InspectionSettingsService:
    """监察组设置服务，独立于议诉系统设置。"""

    def __init__(self, bot: commands.Bot, db: InspectionDatabase):
        self.bot = bot
        self.db = db

    async def get_settings(self, guild_id: int) -> InspectionSettings:
        await ensure_default_settings(self.db, int(guild_id))
        rows = await self.db.fetchall(
            "SELECT key, value FROM inspection_settings WHERE guild_id = ?",
            (int(guild_id),),
        )
        values: dict[str, str | None] = {str(row["key"]): row.get("value") for row in rows}

        def as_int(key: str) -> int | None:
            raw = values.get(key)
            if raw is None or str(raw).strip() == "":
                return None
            try:
                return int(str(raw))
            except ValueError:
                return None

        retention_days = as_int(SETTING_RETENTION_DAYS) or DEFAULT_RETENTION_DAYS
        return InspectionSettings(
            guild_id=int(guild_id),
            candidate_role_id=as_int(SETTING_CANDIDATE_ROLE_ID),
            admin_notice_channel_id=as_int(SETTING_ADMIN_NOTICE_CHANNEL_ID),
            discussion_category_id=as_int(SETTING_DISCUSSION_CATEGORY_ID),
            verdict_channel_id=as_int(SETTING_VERDICT_CHANNEL_ID),
            retention_days=max(1, retention_days),
            archive_channel_id=as_int(SETTING_ARCHIVE_CHANNEL_ID),
        )

    async def update_settings(
        self,
        guild_id: int,
        *,
        candidate_role_id: int | None = None,
        admin_notice_channel_id: int | None = None,
        discussion_category_id: int | None = None,
        verdict_channel_id: int | None = None,
        retention_days: int | None = None,
        archive_channel_id: int | None = None,
    ) -> InspectionSettings:
        now = utc_now_iso()
        updates: dict[str, Any] = {}
        if candidate_role_id is not None:
            updates[SETTING_CANDIDATE_ROLE_ID] = int(candidate_role_id)
        if admin_notice_channel_id is not None:
            updates[SETTING_ADMIN_NOTICE_CHANNEL_ID] = int(admin_notice_channel_id)
        if discussion_category_id is not None:
            updates[SETTING_DISCUSSION_CATEGORY_ID] = int(discussion_category_id)
        if verdict_channel_id is not None:
            updates[SETTING_VERDICT_CHANNEL_ID] = int(verdict_channel_id)
        if retention_days is not None:
            updates[SETTING_RETENTION_DAYS] = max(1, int(retention_days))
        if archive_channel_id is not None:
            updates[SETTING_ARCHIVE_CHANNEL_ID] = int(archive_channel_id)

        for key, value in updates.items():
            await self.db.execute(
                """
                INSERT INTO inspection_settings(guild_id, key, value, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, key) DO UPDATE SET
                  value = excluded.value,
                  updated_at = excluded.updated_at
                """,
                (int(guild_id), key, str(value), now, now),
            )
        await self.db.commit()
        return await self.get_settings(guild_id)

    async def validate_complete(self, guild: discord.Guild) -> tuple[InspectionSettings | None, str | None]:
        settings = await self.get_settings(guild.id)
        if not settings.is_complete:
            missing = "、".join(settings.missing_labels())
            return None, f"监察模块尚未完整配置，缺少：{missing}。请先由管理员执行 /监察 设置。"

        missing_objects: list[str] = []
        if settings.candidate_role_id and guild.get_role(settings.candidate_role_id) is None:
            missing_objects.append("监察候补身份组已不存在")
        category = guild.get_channel(settings.discussion_category_id or 0)
        if not isinstance(category, discord.CategoryChannel):
            missing_objects.append("临时讨论频道分类已不存在或类型不正确")
        admin_channel = await self.get_message_channel(settings.admin_notice_channel_id)
        if admin_channel is None:
            missing_objects.append("admin 通知频道已不存在或不可发送")
        verdict_channel = await self.get_message_channel(settings.verdict_channel_id)
        if verdict_channel is None:
            missing_objects.append("裁决公示频道已不存在或不可发送")
        if missing_objects:
            return None, "监察模块配置无效：" + "、".join(missing_objects) + "。请重新执行 /监察 设置。"

        return settings, None

    async def get_message_channel(self, channel_id: int | None) -> discord.abc.Messageable | None:
        if not channel_id:
            return None
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except Exception:
                return None
        if hasattr(channel, "send"):
            return channel  # type: ignore[return-value]
        return None

    async def get_admin_notice_channel(self, guild_id: int) -> discord.abc.Messageable | None:
        settings = await self.get_settings(guild_id)
        return await self.get_message_channel(settings.admin_notice_channel_id)

    async def get_verdict_channel(self, guild_id: int) -> discord.abc.Messageable | None:
        settings = await self.get_settings(guild_id)
        return await self.get_message_channel(settings.verdict_channel_id)

    async def get_archive_channel(self, guild_id: int) -> discord.abc.Messageable | None:
        settings = await self.get_settings(guild_id)
        return await self.get_message_channel(settings.archive_channel_id)

    async def send_admin_notice(
        self,
        guild_id: int,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        allowed_mentions: discord.AllowedMentions | None = None,
    ) -> bool:
        channel = await self.get_admin_notice_channel(guild_id)
        if channel is None:
            return False
        try:
            await channel.send(
                content=content,
                embed=embed,
                allowed_mentions=allowed_mentions or discord.AllowedMentions.none(),
            )
            return True
        except Exception:
            return False

    async def first_time_update_would_be_complete(
        self,
        guild_id: int,
        *,
        candidate_role_id: int | None,
        admin_notice_channel_id: int | None,
        discussion_category_id: int | None,
        verdict_channel_id: int | None,
        retention_days: int | None,
        archive_channel_id: int | None = None,
    ) -> tuple[bool, InspectionSettings]:
        current = await self.get_settings(guild_id)
        merged = InspectionSettings(
            guild_id=int(guild_id),
            candidate_role_id=candidate_role_id or current.candidate_role_id,
            admin_notice_channel_id=admin_notice_channel_id or current.admin_notice_channel_id,
            discussion_category_id=discussion_category_id or current.discussion_category_id,
            verdict_channel_id=verdict_channel_id or current.verdict_channel_id,
            retention_days=retention_days or current.retention_days or DEFAULT_RETENTION_DAYS,
            archive_channel_id=archive_channel_id or current.archive_channel_id,
        )
        return merged.is_complete, merged

    @staticmethod
    def required_setting_labels() -> str:
        return "、".join(_SETTING_LABELS[key] for key in REQUIRED_SETTING_KEYS)
