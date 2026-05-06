from __future__ import annotations

import secrets
from datetime import timedelta

import discord
from discord.ext import commands

from .constants import (
    ACTIVE_CANDIDATE_STATUSES,
    CANDIDATE_ACTIVE,
    CANDIDATE_CONFIRM_DM_FAILED,
    CANDIDATE_REMOVED,
    CANDIDATE_SELF_EXIT,
    CONFIRM_GRACE_DAYS,
)
from .database import InspectionDatabase
from .settings_service import InspectionSettingsService
from .utils import datetime_to_iso, format_dt, human_status, mention_user, parse_iso, utc_now, utc_now_iso
from .views import build_candidate_confirm_view


class CandidateService:
    """候补成员管理与留任确认服务。"""

    def __init__(
        self,
        bot: commands.Bot,
        db: InspectionDatabase,
        settings_service: InspectionSettingsService,
    ):
        self.bot = bot
        self.db = db
        self.settings_service = settings_service

    async def get_candidate(self, guild_id: int, user_id: int) -> dict | None:
        return await self.db.fetchone(
            "SELECT * FROM inspection_candidates WHERE guild_id = ? AND user_id = ?",
            (int(guild_id), int(user_id)),
        )

    async def list_candidates(self, guild_id: int) -> list[dict]:
        return await self.db.fetchall(
            """
            SELECT * FROM inspection_candidates
            WHERE guild_id = ?
            ORDER BY
              CASE status
                WHEN 'active' THEN 0
                WHEN 'confirm_dm_failed' THEN 1
                WHEN 'self_exit' THEN 2
                WHEN 'removed' THEN 3
                ELSE 9
              END,
              updated_at DESC
            """,
            (int(guild_id),),
        )

    async def list_active_candidates(self, guild_id: int) -> list[dict]:
        placeholders = ",".join("?" for _ in ACTIVE_CANDIDATE_STATUSES)
        return await self.db.fetchall(
            f"""
            SELECT * FROM inspection_candidates
            WHERE guild_id = ? AND status IN ({placeholders})
            ORDER BY created_at ASC
            """,
            (int(guild_id), *ACTIVE_CANDIDATE_STATUSES),
        )

    async def add_candidate(self, guild: discord.Guild, member: discord.Member) -> str:
        settings, error = await self.settings_service.validate_complete(guild)
        if error or settings is None:
            raise ValueError(error or "监察模块尚未完整配置。")
        role = guild.get_role(settings.candidate_role_id or 0)
        if role is None:
            raise ValueError("监察候补身份组不存在，请重新执行 /监察 设置。")

        if role not in member.roles:
            await member.add_roles(role, reason="监察组：添加候补")

        now = utc_now()
        next_confirm_at = now + timedelta(days=settings.retention_days)
        await self._upsert_active_candidate(
            guild.id,
            member.id,
            next_confirm_at=next_confirm_at,
            last_confirmed_at=now,
        )
        return f"已将 {member.mention} 添加为监察候补；下次留任确认：{format_dt(next_confirm_at)}。"

    async def remove_candidate(
        self,
        guild: discord.Guild,
        user_id: int,
        *,
        reason: str | None = None,
        status: str = CANDIDATE_REMOVED,
    ) -> str:
        settings, error = await self.settings_service.validate_complete(guild)
        if error or settings is None:
            raise ValueError(error or "监察模块尚未完整配置。")
        member = await self._get_member(guild, int(user_id))
        role = guild.get_role(settings.candidate_role_id or 0)
        if member is not None and role is not None and role in member.roles:
            await member.remove_roles(role, reason=reason or "监察组：移除候补")

        now = utc_now_iso()
        await self.db.execute(
            """
            INSERT INTO inspection_candidates(
              guild_id, user_id, status, remove_reason, confirm_session_id,
              next_confirm_at, confirm_deadline_at, last_confirmed_at,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
              status = excluded.status,
              remove_reason = excluded.remove_reason,
              confirm_session_id = NULL,
              next_confirm_at = NULL,
              confirm_deadline_at = NULL,
              updated_at = excluded.updated_at
            """,
            (int(guild.id), int(user_id), status, reason, now, now),
        )
        await self.db.commit()

        label = "主动退出" if status == CANDIDATE_SELF_EXIT else "已移除"
        return f"{label}监察候补：{mention_user(int(user_id))}。"

    async def self_exit_candidate(self, guild: discord.Guild, member: discord.Member) -> str:
        current = await self.get_candidate(guild.id, member.id)
        if current is None or current.get("status") not in ACTIVE_CANDIDATE_STATUSES:
            raise ValueError("你当前不是有效监察候补。")
        return await self.remove_candidate(
            guild,
            member.id,
            reason="候补本人主动退出。",
            status=CANDIDATE_SELF_EXIT,
        )

    async def confirm_retention(self, guild: discord.Guild, member: discord.Member) -> str:
        settings, error = await self.settings_service.validate_complete(guild)
        if error or settings is None:
            raise ValueError(error or "监察模块尚未完整配置。")
        role = guild.get_role(settings.candidate_role_id or 0)
        if role is None:
            raise ValueError("监察候补身份组不存在，请重新执行 /监察 设置。")
        if role not in member.roles:
            await member.add_roles(role, reason="监察组：确认留任时补回候补身份组")

        now = utc_now()
        next_confirm_at = now + timedelta(days=settings.retention_days)
        await self._upsert_active_candidate(
            guild.id,
            member.id,
            next_confirm_at=next_confirm_at,
            last_confirmed_at=now,
        )
        return f"已确认 {member.mention} 继续留任；下次确认：{format_dt(next_confirm_at)}。"

    async def _upsert_active_candidate(
        self,
        guild_id: int,
        user_id: int,
        *,
        next_confirm_at,
        last_confirmed_at,
    ) -> None:
        now_iso = utc_now_iso()
        await self.db.execute(
            """
            INSERT INTO inspection_candidates(
              guild_id, user_id, status, remove_reason, confirm_session_id,
              next_confirm_at, confirm_deadline_at, last_confirmed_at,
              created_at, updated_at
            ) VALUES (?, ?, ?, NULL, NULL, ?, NULL, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
              status = excluded.status,
              remove_reason = NULL,
              confirm_session_id = NULL,
              next_confirm_at = excluded.next_confirm_at,
              confirm_deadline_at = NULL,
              last_confirmed_at = excluded.last_confirmed_at,
              updated_at = excluded.updated_at
            """,
            (
                int(guild_id),
                int(user_id),
                CANDIDATE_ACTIVE,
                datetime_to_iso(next_confirm_at),
                datetime_to_iso(last_confirmed_at),
                now_iso,
                now_iso,
            ),
        )
        await self.db.commit()

    async def process_due_candidate_confirmations(self) -> None:
        now = utc_now()
        rows = await self.db.fetchall(
            """
            SELECT * FROM inspection_candidates
            WHERE status = ?
              AND next_confirm_at IS NOT NULL
              AND next_confirm_at <= ?
              AND (confirm_session_id IS NULL OR confirm_session_id = '')
              AND (confirm_deadline_at IS NULL OR confirm_deadline_at = '')
            """,
            (CANDIDATE_ACTIVE, datetime_to_iso(now)),
        )
        for row in rows:
            try:
                await self._send_candidate_confirmation(row, now)
            except Exception:
                # 单个成员失败不应阻断整个后台循环。
                continue

    async def _send_candidate_confirmation(self, row: dict, now) -> None:
        guild_id = int(row["guild_id"])
        user_id = int(row["user_id"])
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        settings = await self.settings_service.get_settings(guild_id)
        session_id = secrets.token_urlsafe(10)
        deadline = now + timedelta(days=CONFIRM_GRACE_DAYS)
        status = CANDIDATE_ACTIVE
        dm_failed = False

        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(
                "监察候补留任确认：是否继续保留监察候补身份？\n"
                f"请在 {format_dt(deadline)} 前选择；逾期未处理将自动移除候补身份。",
                view=build_candidate_confirm_view(session_id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            dm_failed = True
            status = CANDIDATE_CONFIRM_DM_FAILED

        now_iso = utc_now_iso()
        await self.db.execute(
            """
            UPDATE inspection_candidates
            SET status = ?, confirm_session_id = ?, confirm_deadline_at = ?, updated_at = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (status, session_id, datetime_to_iso(deadline), now_iso, guild_id, user_id),
        )
        await self.db.commit()

        if dm_failed:
            await self.settings_service.send_admin_notice(
                guild_id,
                f"监察候补 {mention_user(user_id)} 的留任确认 DM 发送失败。"
                f"请在 {format_dt(deadline)} 前使用 `/监察 成员管理 操作:设置留任 用户:@user` 手动处理。",
            )

    async def process_expired_candidate_confirmations(self) -> None:
        now = utc_now()
        placeholders = ",".join("?" for _ in ACTIVE_CANDIDATE_STATUSES)
        rows = await self.db.fetchall(
            f"""
            SELECT * FROM inspection_candidates
            WHERE status IN ({placeholders})
              AND confirm_session_id IS NOT NULL
              AND confirm_session_id != ''
              AND confirm_deadline_at IS NOT NULL
              AND confirm_deadline_at <= ?
            """,
            (*ACTIVE_CANDIDATE_STATUSES, datetime_to_iso(now)),
        )
        for row in rows:
            guild_id = int(row["guild_id"])
            user_id = int(row["user_id"])
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            try:
                await self.remove_candidate(
                    guild,
                    user_id,
                    reason="留任确认超时未处理。",
                    status=CANDIDATE_REMOVED,
                )
            except Exception:
                # 即使移除身份组失败，也落库为 removed，避免无限重复。
                now_iso = utc_now_iso()
                await self.db.execute(
                    """
                    UPDATE inspection_candidates
                    SET status = ?, remove_reason = ?, confirm_session_id = NULL,
                        next_confirm_at = NULL, confirm_deadline_at = NULL, updated_at = ?
                    WHERE guild_id = ? AND user_id = ?
                    """,
                    (CANDIDATE_REMOVED, "留任确认超时未处理。", now_iso, guild_id, user_id),
                )
                await self.db.commit()
            await self.settings_service.send_admin_notice(
                guild_id,
                f"监察候补 {mention_user(user_id)} 因留任确认超时未处理，已自动移除。",
            )

    async def handle_candidate_button(
        self,
        interaction: discord.Interaction,
        *,
        session_id: str,
        keep: bool,
    ) -> str:
        row = await self.db.fetchone(
            "SELECT * FROM inspection_candidates WHERE confirm_session_id = ?",
            (session_id,),
        )
        if row is None or int(row.get("user_id") or 0) != int(interaction.user.id):
            return "该留任确认按钮已过期或不属于你。"

        deadline = parse_iso(row.get("confirm_deadline_at"))
        if deadline is None or deadline <= utc_now():
            return "该留任确认已过期，请联系管理员手动处理。"

        guild = self.bot.get_guild(int(row["guild_id"]))
        if guild is None:
            return "无法找到对应服务器，该操作已失效。"
        member = await self._get_member(guild, int(row["user_id"]))
        if member is None:
            return "无法在服务器中找到你的成员信息，该操作已失效。"

        if keep:
            return await self.confirm_retention(guild, member)
        return await self.remove_candidate(
            guild,
            member.id,
            reason="候补本人通过留任确认选择退出。",
            status=CANDIDATE_SELF_EXIT,
        )

    async def _get_member(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        member = guild.get_member(int(user_id))
        if member is not None:
            return member
        try:
            return await guild.fetch_member(int(user_id))
        except Exception:
            return None

    @staticmethod
    def render_candidate_list(rows: list[dict]) -> str:
        if not rows:
            return "当前没有监察候补记录。"
        lines = ["监察候补名单："]
        for row in rows[:50]:
            lines.append(
                f"- {mention_user(int(row['user_id']))}｜{human_status(row.get('status'))}"
                f"｜下次确认：{format_dt(row.get('next_confirm_at'))}"
                f"｜确认截止：{format_dt(row.get('confirm_deadline_at'))}"
                f"｜原因：{row.get('remove_reason') or '（无）'}"
            )
        if len(rows) > 50:
            lines.append(f"……另有 {len(rows) - 50} 条记录未显示。")
        return "\n".join(lines)
