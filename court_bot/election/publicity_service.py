from __future__ import annotations

import logging

import discord

from .constants import (
    BATCH_COMPLETED,
    BATCH_PARTIAL_FAILED,
    BATCH_PUBLISHING,
    PUBLIC_FAILED,
    PUBLIC_PENDING,
    PUBLIC_SYNCED,
    PUBLICITY_BATCH,
    REG_ACTIVE,
)
from .database import ElectionRepo
from .embeds import build_candidate_public_embed, build_result_embeds
from .time_utils import utc_now_iso

log = logging.getLogger(__name__)


class PublicityService:
    def __init__(self, bot, repo: ElectionRepo):
        self.bot = bot
        self.repo = repo

    async def _get_text_channel(self, channel_id: int | None) -> discord.TextChannel | None:
        if not channel_id:
            return None
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except Exception:
                return None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def alert(self, election: dict, message: str) -> None:
        channel = await self._get_text_channel(election.get("alert_channel_id") or election.get("public_channel_id"))
        safe_message = message[:1800]
        if channel is not None:
            try:
                await channel.send(safe_message, allowed_mentions=discord.AllowedMentions.none())
                return
            except Exception:
                log.exception("Failed to send election alert")
        creator_id = int(election.get("created_by") or 0)
        if creator_id:
            try:
                user = self.bot.get_user(creator_id) or await self.bot.fetch_user(creator_id)
                await user.send(safe_message)
            except Exception:
                log.exception("Failed to DM election alert")
        await self.repo.log(int(election["id"]), int(election["guild_id"]), None, "alert_failed_or_sent", {"message": safe_message})

    async def sync_registration_publicity(self, election: dict, registration: dict, *, allow_create: bool = True) -> bool:
        field_names = await self.repo.get_field_names_by_key(int(election["id"]))
        channel_id = int(registration.get("public_channel_id") or election.get("public_channel_id") or 0)
        channel = await self._get_text_channel(channel_id)
        if channel is None:
            error = "无法读取公示频道。"
            await self.repo.update_registration_public_message(int(registration["id"]), channel_id=channel_id or None, message_id=None, status=PUBLIC_FAILED, error=error)
            return False

        embed = build_candidate_public_embed(election, registration, field_names)
        message_id = int(registration.get("public_message_id") or 0)
        try:
            if message_id:
                try:
                    message = await channel.fetch_message(message_id)
                    await message.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                except discord.NotFound:
                    if not allow_create:
                        raise
                    msg = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                    await self.repo.update_registration_public_message(
                        int(registration["id"]), channel_id=int(channel.id), message_id=int(msg.id), status=PUBLIC_SYNCED, error=None
                    )
                    return True
            else:
                if not allow_create:
                    await self.repo.update_registration_public_message(
                        int(registration["id"]), channel_id=int(channel.id), message_id=None, status=PUBLIC_PENDING, error=None
                    )
                    return False
                msg = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                message_id = int(msg.id)
            await self.repo.update_registration_public_message(
                int(registration["id"]), channel_id=int(channel.id), message_id=message_id, status=PUBLIC_SYNCED, error=None
            )
            return True
        except Exception as exc:
            error = str(exc)[:500]
            await self.repo.update_registration_public_message(
                int(registration["id"]), channel_id=int(channel.id), message_id=message_id or None, status=PUBLIC_FAILED, error=error
            )
            await self.repo.log(int(election["id"]), int(election["guild_id"]), None, "publicity_sync_failed", {"registration_id": int(registration["id"]), "error": error})
            return False

    async def publish_batch_publicity(self, election: dict) -> tuple[int, int]:
        if election.get("publicity_mode") != PUBLICITY_BATCH:
            return (0, 0)
        await self.repo.set_batch_publicity_status(int(election["id"]), BATCH_PUBLISHING)
        registrations = await self.repo.list_active_registrations(int(election["id"]))
        success = 0
        failed = 0
        for reg in registrations:
            ok = await self.sync_registration_publicity(election, reg, allow_create=True)
            if ok:
                success += 1
            else:
                failed += 1
        if failed:
            await self.repo.set_batch_publicity_status(
                int(election["id"]),
                BATCH_PARTIAL_FAILED,
                f"统一公示部分失败：成功 {success}，失败 {failed}",
                published_at=utc_now_iso() if success else None,
            )
            await self.alert(
                election,
                f"⚠️ 募选 #{election['id']}《{election['name']}》统一公示部分失败：成功 {success}，失败 {failed}。请在公示期内执行 /募选 同步公示 修复。",
            )
        else:
            await self.repo.set_batch_publicity_status(int(election["id"]), BATCH_COMPLETED, None, published_at=utc_now_iso())
        await self.repo.log(int(election["id"]), int(election["guild_id"]), None, "batch_publicity_published", {"success": success, "failed": failed})
        return success, failed

    async def sync_scope(self, election: dict, *, scope: str = "failed", user_id: int | None = None) -> tuple[int, int]:
        regs = await self.repo.list_registrations(int(election["id"]))
        selected = []
        for reg in regs:
            if user_id is not None and int(reg.get("user_id") or 0) != int(user_id):
                continue
            if scope == "failed" and reg.get("public_sync_status") != PUBLIC_FAILED:
                continue
            if scope == "published" and not reg.get("public_message_id"):
                continue
            if scope == "active" and reg.get("status") != REG_ACTIVE:
                continue
            selected.append(reg)
        success = 0
        failed = 0
        for reg in selected:
            ok = await self.sync_registration_publicity(election, reg, allow_create=True)
            if ok:
                success += 1
            else:
                failed += 1
        if election.get("publicity_mode") == PUBLICITY_BATCH:
            active = await self.repo.list_active_registrations(int(election["id"]))
            active_with_messages = [r for r in active if r.get("public_message_id")]
            if len(active) == len(active_with_messages) and failed == 0:
                await self.repo.set_batch_publicity_status(int(election["id"]), BATCH_COMPLETED, None, published_at=utc_now_iso())
        return success, failed

    async def publish_result(self, election: dict, result: dict) -> None:
        channel = await self._get_text_channel(election.get("public_channel_id"))
        if channel is None:
            await self.alert(election, f"募选 #{election['id']} 结果无法发布：找不到公示频道。")
            return
        for embed in build_result_embeds(election, result):
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
