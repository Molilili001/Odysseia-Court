from __future__ import annotations

import logging
import re
from typing import Any

import discord
from discord import app_commands
from discord.app_commands import Choice, locale_str
from discord.ext import commands

from .constants import (
    BATCH_COMPLETED,
    BATCH_PARTIAL_FAILED,
    MAX_FIELDS,
    MAX_SELF_INTRO_LENGTH,
    PUBLICITY_BATCH,
    PUBLICITY_REALTIME,
    PUBLIC_PENDING,
    REG_COUNT_DISPLAY_DETAIL,
    REG_COUNT_DISPLAY_HIDDEN,
    REG_COUNT_DISPLAY_TOTAL,
    PUBLIC_SYNC_STATUS_LABELS,
    REGISTRATION_STATUS_LABELS,
    REG_REJECTED,
    REG_REVOKED,
    REG_WITHDRAWN,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_SETUP,
    STATUS_REGISTRATION,
    STATUS_REGISTRATION_ENDED,
    STATUS_VOTING,
)
from .database import ElectionRepo
from .embeds import (
    build_election_list_embed,
    build_help_embeds,
    build_registration_count_text,
    build_registration_entry_embed,
    build_status_embed,
    build_vote_candidate_list_embeds,
    build_vote_entry_embed,
)
from .permissions import can_register, is_election_admin, missing_candidate_role_message
from .publicity_service import PublicityService
from .result_service import ResultService
from .scheduler import ElectionScheduler
from .text_utils import contains_forbidden_mention, sanitize_public_text
from .time_utils import build_schedule, format_time_pair, parse_duration_minutes, to_utc_iso, utc_now, utc_now_iso
from .views import FieldSelectView, RegistrationEntryView, RegistrationIntroModal, VoteEntryView
from .vote_service import VoteService

log = logging.getLogger(__name__)


ROLE_CLEAR_TOKENS = {"clear", "none", "all", "无", "清空", "全部", "所有", "不限", "不限制"}


def parse_fields_config(raw: str) -> list[tuple[str, int]]:
    text = (raw or "").strip()
    if not text:
        raise ValueError("岗位/字段配置不能为空。")
    parts = [p.strip() for p in re.split(r"[,，\n]+", text) if p.strip()]
    if not parts:
        raise ValueError("岗位/字段配置不能为空。")
    if len(parts) > MAX_FIELDS:
        raise ValueError(f"岗位/字段最多 {MAX_FIELDS} 个。")
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    for part in parts:
        if ":" in part:
            name, count_text = part.split(":", 1)
        elif "：" in part:
            name, count_text = part.split("：", 1)
        else:
            raise ValueError(f"字段配置格式错误：{part}。请使用 大当家:1。")
        name = sanitize_public_text(name, max_len=80, fallback="").strip()
        if not name:
            raise ValueError("岗位/字段名称不能为空。")
        if name in seen:
            raise ValueError(f"岗位/字段名称重复：{name}")
        seen.add(name)
        try:
            count = int(count_text.strip())
        except ValueError:
            raise ValueError(f"岗位/字段人数必须是整数：{part}") from None
        if count < 1:
            raise ValueError(f"岗位/字段人数必须大于 0：{part}")
        out.append((name, count))
    return out


def parse_role_ids_from_text(raw: str | None) -> list[int]:
    if not raw or not raw.strip():
        return []
    if raw.strip().lower() in ROLE_CLEAR_TOKENS:
        return []
    if not re.search(r"\d", raw):
        raise ValueError("身份组列表格式错误：请填写身份组 ID 或身份组提及，例如 <@&123456789>。")
    out: list[int] = []
    seen: set[int] = set()
    for part in re.split(r"[,;，；\s]+", raw.strip()):
        if not part:
            continue
        digits = "".join(ch for ch in part if ch.isdigit())
        if not digits:
            continue
        rid = int(digits)
        if rid not in seen:
            seen.add(rid)
            out.append(rid)
    if not out:
        raise ValueError("身份组列表格式错误：请填写身份组 ID 或身份组提及，例如 <@&123456789>。")
    return out



def require_bot_channel_permissions(
    guild: discord.Guild,
    channels: list[tuple[str, discord.TextChannel | None]],
    *,
    need_read_history: bool = True,
) -> None:
    bot_member = guild.me
    if bot_member is None:
        raise ValueError("无法读取 Bot 在当前服务器的成员状态。")
    missing_lines: list[str] = []
    for label, channel in channels:
        if channel is None:
            continue
        perms = channel.permissions_for(bot_member)
        missing: list[str] = []
        if not perms.view_channel:
            missing.append("查看频道")
        if not perms.send_messages:
            missing.append("发送消息")
        if not perms.embed_links:
            missing.append("嵌入链接/Embed Links")
        if need_read_history and not perms.read_message_history:
            missing.append("读取消息历史")
        if missing:
            missing_lines.append(f"{label} {channel.mention} 缺少：{'、'.join(missing)}")
    if missing_lines:
        raise ValueError("Bot 频道权限不足：\n" + "\n".join(missing_lines))

class ElectionGroup(app_commands.Group):
    def __init__(self, cog: "ElectionCog"):
        super().__init__(
            name=locale_str("election", zh_CN="募选", zh_TW="募選", en_US="募选", en_GB="募选"),
            description=locale_str("Election system", zh_CN="募选系统", zh_TW="募選系統", en_US="募选系统", en_GB="募选系统"),
        )
        self.cog = cog

    def _admin(self, interaction: discord.Interaction) -> bool:
        return isinstance(interaction.user, discord.Member) and is_election_admin(interaction.user)

    @app_commands.command(name=locale_str("create", zh_CN="创建", zh_TW="建立", en_US="创建", en_GB="创建"), description="创建一场募选")
    @app_commands.rename(
        name=locale_str("name", zh_CN="名称", zh_TW="名稱", en_US="名称", en_GB="名称"),
        fields_config=locale_str("fields_config", zh_CN="岗位配置", zh_TW="崗位配置", en_US="岗位配置", en_GB="岗位配置"),
        vote_max_selections=locale_str("vote_max_selections", zh_CN="每人最多投票数", zh_TW="每人最多投票數", en_US="每人最多投票数", en_GB="每人最多投票数"),
        publicity_mode=locale_str("publicity_mode", zh_CN="公示模式", zh_TW="公示模式", en_US="公示模式", en_GB="公示模式"),
        registration_duration=locale_str("registration_duration", zh_CN="报名持续时间", zh_TW="報名持續時間", en_US="报名持续时间", en_GB="报名持续时间"),
        publicity_duration=locale_str("publicity_duration", zh_CN="公示持续时间", zh_TW="公示持續時間", en_US="公示持续时间", en_GB="公示持续时间"),
        voting_duration=locale_str("voting_duration", zh_CN="投票持续时间", zh_TW="投票持續時間", en_US="投票持续时间", en_GB="投票持续时间"),
        registration_channel=locale_str("registration_channel", zh_CN="报名频道", zh_TW="報名頻道", en_US="报名频道", en_GB="报名频道"),
        voting_channel=locale_str("voting_channel", zh_CN="投票频道", zh_TW="投票頻道", en_US="投票频道", en_GB="投票频道"),
        public_channel=locale_str("public_channel", zh_CN="公示频道", zh_TW="公示頻道", en_US="公示频道", en_GB="公示频道"),
        registration_roles=locale_str("registration_roles", zh_CN="允许报名身份组", zh_TW="允許報名身分組", en_US="允许报名身份组", en_GB="允许报名身份组"),
        voting_roles=locale_str("voting_roles", zh_CN="允许投票身份组", zh_TW="允許投票身分組", en_US="允许投票身份组", en_GB="允许投票身份组"),
        alert_channel=locale_str("alert_channel", zh_CN="告警频道", zh_TW="告警頻道", en_US="告警频道", en_GB="告警频道"),
        start_at=locale_str("start_at", zh_CN="报名开始时间", zh_TW="報名開始時間", en_US="报名开始时间", en_GB="报名开始时间"),
        send_entry=locale_str("send_entry", zh_CN="立即发送入口", zh_TW="立即發送入口", en_US="立即发送入口", en_GB="立即发送入口"),
        registration_count_display=locale_str("registration_count_display", zh_CN="报名人数显示", zh_TW="報名人數顯示", en_US="报名人数显示", en_GB="报名人数显示"),
    )
    @app_commands.describe(
        name="募选名称，对外展示在报名入口、公示、投票和结果中",
        fields_config="格式：大当家:1,二当家:3,执行成员:9",
        vote_max_selections="每名投票者最多可选择的候选人数",
        publicity_mode="公示模式：实时公示或报名结束后统一公示",
        registration_duration="例如：3天、72小时、2天6小时",
        publicity_duration="例如：1天、12小时、0小时",
        voting_duration="例如：2天、48小时",
        registration_channel="发送报名入口、接收报名交互的频道",
        voting_channel="发布统一投票面板的频道",
        public_channel="发布候选人公示和结果的频道",
        registration_roles="允许报名身份组 ID/提及，逗号分隔；不填表示所有成员可报名",
        voting_roles="允许投票身份组 ID/提及，逗号分隔；不填表示所有成员可投票",
        alert_channel="公示失败、流程异常等告警频道；不填则优先使用公示频道",
        start_at="北京时间 YYYY-MM-DD HH:mm；不填为立即开始",
        send_entry="是否在创建后立即发送报名入口",
        registration_count_display="报名入口是否公开当前报名人数；不填默认不显示",
    )
    @app_commands.choices(
        publicity_mode=[Choice(name="实时公示", value=PUBLICITY_REALTIME), Choice(name="报名结束后统一公示", value=PUBLICITY_BATCH)],
        registration_count_display=[Choice(name="只显示总人数", value=REG_COUNT_DISPLAY_TOTAL), Choice(name="详细显示", value=REG_COUNT_DISPLAY_DETAIL)],
    )
    async def create(
        self,
        interaction: discord.Interaction,
        name: str,
        fields_config: str,
        vote_max_selections: int,
        publicity_mode: Choice[str],
        registration_duration: str,
        publicity_duration: str,
        voting_duration: str,
        registration_channel: discord.TextChannel,
        voting_channel: discord.TextChannel,
        public_channel: discord.TextChannel,
        registration_roles: str | None = None,
        voting_roles: str | None = None,
        alert_channel: discord.TextChannel | None = None,
        start_at: str | None = None,
        send_entry: bool = True,
        registration_count_display: Choice[str] | None = None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if not self._admin(interaction):
            await interaction.response.send_message("无权限（需要 Manage Guild 或 Administrator）。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            fields = parse_fields_config(fields_config)
            require_bot_channel_permissions(
                interaction.guild,
                [
                    ("报名频道", registration_channel),
                    ("投票频道", voting_channel),
                    ("公示频道", public_channel),
                    ("告警频道", alert_channel),
                ],
            )
            if vote_max_selections < 1:
                raise ValueError("每人最多投票数必须大于 0。")
            candidate_role_ids = parse_role_ids_from_text(registration_roles)
            for rid in candidate_role_ids:
                if interaction.guild.get_role(rid) is None:
                    raise ValueError(f"找不到允许报名身份组：{rid}")
            role_ids = parse_role_ids_from_text(voting_roles)
            for rid in role_ids:
                if interaction.guild.get_role(rid) is None:
                    raise ValueError(f"找不到允许投票身份组：{rid}")
            reg_minutes = parse_duration_minutes(registration_duration, label="报名持续时间")
            pub_minutes = parse_duration_minutes(publicity_duration, allow_zero=True, label="公示持续时间")
            vote_minutes = parse_duration_minutes(voting_duration, label="投票持续时间")
            schedule = build_schedule(
                start_at_text=start_at,
                registration_duration_minutes=reg_minutes,
                publicity_duration_minutes=pub_minutes,
                voting_duration_minutes=vote_minutes,
            )
            if schedule.voting_end_at <= schedule.registration_start_at:
                raise ValueError("时间配置不合法。")
            now_utc = utc_now()
            initial_status = STATUS_REGISTRATION if now_utc >= schedule.registration_start_at.astimezone(now_utc.tzinfo) else STATUS_SETUP
            election_id = await self.cog.repo.create_election(
                guild_id=interaction.guild.id,
                name=sanitize_public_text(name, max_len=120, fallback="未命名募选"),
                publicity_mode=publicity_mode.value,
                registration_channel_id=registration_channel.id,
                voting_channel_id=voting_channel.id,
                public_channel_id=public_channel.id,
                alert_channel_id=alert_channel.id if alert_channel else None,
                allowed_candidate_role_ids=candidate_role_ids,
                allowed_voter_role_ids=role_ids,
                registration_count_display=registration_count_display.value if registration_count_display else REG_COUNT_DISPLAY_HIDDEN,
                vote_max_selections=vote_max_selections,
                registration_duration_minutes=reg_minutes,
                publicity_duration_minutes=pub_minutes,
                voting_duration_minutes=vote_minutes,
                registration_start_at=to_utc_iso(schedule.registration_start_at),
                registration_end_at=to_utc_iso(schedule.registration_end_at),
                voting_start_at=to_utc_iso(schedule.voting_start_at),
                voting_end_at=to_utc_iso(schedule.voting_end_at),
                created_by=interaction.user.id,
                fields=fields,
                initial_status=initial_status,
            )
            election = await self.cog.repo.get_election(election_id)
            field_rows = await self.cog.repo.list_fields(election_id)
            if election and send_entry:
                await self.cog.send_registration_entry(election, field_rows, channel=registration_channel)
            await interaction.edit_original_response(content=f"已创建募选 #{election_id}。{' 已发送报名入口。' if send_entry else ''}")
        except Exception as exc:
            await interaction.edit_original_response(content=f"创建失败：{exc}")

    @app_commands.command(name=locale_str("entry", zh_CN="设置入口", zh_TW="設定入口", en_US="设置入口", en_GB="设置入口"), description="发送或重发报名入口")
    @app_commands.rename(
        election_id=locale_str("election_id", zh_CN="募选id", zh_TW="募選id", en_US="募选id", en_GB="募选id"),
        channel=locale_str("channel", zh_CN="频道", zh_TW="頻道", en_US="频道", en_GB="频道"),
    )
    @app_commands.describe(election_id="募选 ID；不填时自动选择当前未完成募选", channel="报名入口要发送到的频道；不填则使用配置的报名频道")
    async def entry(self, interaction: discord.Interaction, election_id: int | None = None, channel: discord.TextChannel | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if not self._admin(interaction):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            election = await self.cog.repo.resolve_election(interaction.guild.id, election_id)
            fields = await self.cog.repo.list_fields(int(election["id"]))
            target = channel or interaction.guild.get_channel(int(election.get("registration_channel_id") or 0)) or interaction.channel
            if not isinstance(target, discord.TextChannel):
                raise ValueError("无法定位报名频道。")
            await self.cog.send_registration_entry(election, fields, channel=target)
            await interaction.edit_original_response(content="已发送报名入口。")
        except Exception as exc:
            await interaction.edit_original_response(content=f"操作失败：{exc}")

    @app_commands.command(name=locale_str("refresh_entry", zh_CN="刷新入口", zh_TW="刷新入口", en_US="refresh_entry", en_GB="refresh_entry"), description="原地刷新已发送的报名入口")
    @app_commands.rename(election_id=locale_str("election_id", zh_CN="募选id", zh_TW="募選id", en_US="募选id", en_GB="募选id"))
    @app_commands.describe(election_id="募选 ID；不填时自动选择当前未完成募选")
    async def refresh_entry(self, interaction: discord.Interaction, election_id: int | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if not self._admin(interaction):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            election = await self.cog.repo.resolve_election(interaction.guild.id, election_id)
            refreshed = await self.cog.refresh_registration_entry(election, reason="manual_refresh_entry")
            if not refreshed:
                await interaction.edit_original_response(
                    content=(
                        "刷新失败：该募选尚未记录报名入口消息，或 Bot 无法读取/编辑原消息。"
                        "如需重新生成入口，可使用 /募选 设置入口。"
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await interaction.edit_original_response(content=f"已刷新募选 #{election['id']} 的报名入口。")
        except Exception as exc:
            await interaction.edit_original_response(content=f"刷新失败：{exc}")

    @app_commands.command(name=locale_str("refresh_display", zh_CN="刷新展示", zh_TW="刷新展示", en_US="refresh_display", en_GB="refresh_display"), description="原地刷新报名入口、公示和投票展示消息")
    @app_commands.choices(
        scope=[
            Choice(name="自动", value="auto"),
            Choice(name="全部", value="all"),
            Choice(name="报名入口", value="entry"),
            Choice(name="公示", value="publicity"),
            Choice(name="投票面板", value="vote"),
        ]
    )
    @app_commands.rename(
        election_id=locale_str("election_id", zh_CN="募选id", zh_TW="募選id", en_US="募选id", en_GB="募选id"),
        scope=locale_str("scope", zh_CN="范围", zh_TW="範圍", en_US="范围", en_GB="范围"),
    )
    @app_commands.describe(election_id="募选 ID；不填时自动选择当前未完成募选", scope="刷新范围；不填默认按当前阶段自动刷新")
    async def refresh_display(self, interaction: discord.Interaction, election_id: int | None = None, scope: Choice[str] | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if not self._admin(interaction):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            election = await self.cog.repo.resolve_election(interaction.guild.id, election_id)
            report = await self.cog.refresh_display_messages(
                interaction.guild,
                election,
                scope=(scope.value if scope else "auto"),
            )
            await interaction.edit_original_response(content=report[:1900], allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            await interaction.edit_original_response(content=f"刷新展示失败：{exc}")

    @app_commands.command(name=locale_str("status", zh_CN="状态", zh_TW="狀態", en_US="状态", en_GB="状态"), description="查看募选状态")
    @app_commands.rename(election_id=locale_str("election_id", zh_CN="募选id", zh_TW="募選id", en_US="募选id", en_GB="募选id"))
    @app_commands.describe(election_id="募选 ID；不填时自动选择当前未完成募选")
    async def status(self, interaction: discord.Interaction, election_id: int | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        try:
            election = await self.cog.repo.resolve_election(interaction.guild.id, election_id)
            fields = await self.cog.repo.list_fields(int(election["id"]))
            counts = await self.cog.repo.count_registrations_by_status(int(election["id"]))
            vote_count = await self.cog.repo.count_vote_records(int(election["id"]))
            is_admin = isinstance(interaction.user, discord.Member) and is_election_admin(interaction.user)
            await interaction.response.send_message(
                embed=build_status_embed(election, fields, counts, vote_count, is_admin_view=is_admin),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:
            await interaction.response.send_message(f"查询失败：{exc}", ephemeral=True)

    @app_commands.command(name=locale_str("list", zh_CN="列表", zh_TW="列表", en_US="列表", en_GB="列表"), description="列出募选")
    @app_commands.rename(include_completed=locale_str("include_completed", zh_CN="包含已完成", zh_TW="包含已完成", en_US="包含已完成", en_GB="包含已完成"))
    @app_commands.describe(include_completed="是否同时列出已完成或已取消的募选")
    async def list_cmd(self, interaction: discord.Interaction, include_completed: bool = False) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        rows = await self.cog.repo.list_elections(interaction.guild.id, include_completed=include_completed, limit=20)
        await interaction.response.send_message(embed=build_election_list_embed(rows), ephemeral=True)

    @app_commands.command(name=locale_str("candidate", zh_CN="候选人", zh_TW="候選人", en_US="候选人", en_GB="候选人"), description="管理员管理候选人")
    @app_commands.choices(operation=[Choice(name="查看", value="view"), Choice(name="打回", value="reject"), Choice(name="撤销", value="revoke"), Choice(name="恢复", value="restore"), Choice(name="重发公示", value="republish")])
    @app_commands.rename(
        operation=locale_str("operation", zh_CN="操作", zh_TW="操作", en_US="操作", en_GB="操作"),
        user=locale_str("user", zh_CN="候选人", zh_TW="候選人", en_US="候选人", en_GB="候选人"),
        election_id=locale_str("election_id", zh_CN="募选id", zh_TW="募選id", en_US="募选id", en_GB="募选id"),
        reason=locale_str("reason", zh_CN="原因", zh_TW="原因", en_US="原因", en_GB="原因"),
    )
    @app_commands.describe(operation="候选人管理操作", user="候选人；查看操作可不填", election_id="募选 ID；不填时自动选择当前未完成募选", reason="打回、撤销或恢复时记录到审计日志的原因")
    async def candidate(self, interaction: discord.Interaction, operation: Choice[str], user: discord.Member | None = None, election_id: int | None = None, reason: str | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if not self._admin(interaction):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            election = await self.cog.repo.resolve_election(interaction.guild.id, election_id)
            if operation.value == "view":
                regs = await self.cog.repo.list_registrations(int(election["id"]))
                lines = [f"{r['display_name']}｜{r['user_id']}｜{r['status']}｜公示：{r.get('public_sync_status')}" for r in regs[:40]]
                await interaction.edit_original_response(content=("\n".join(lines) or "暂无报名。")[:1900])
                return
            if user is None:
                raise ValueError("该操作需要指定用户。")
            if operation.value == "reject":
                reg = await self.cog.repo.set_registration_status(election_id=int(election["id"]), user_id=user.id, status="rejected", reason=reason or "管理员打回", operator_id=interaction.user.id)
            elif operation.value == "revoke":
                reg = await self.cog.repo.set_registration_status(election_id=int(election["id"]), user_id=user.id, status="revoked", reason=reason or "管理员撤销", operator_id=interaction.user.id)
            elif operation.value == "restore":
                reg = await self.cog.repo.set_registration_status(election_id=int(election["id"]), user_id=user.id, status="active", reason=reason, operator_id=interaction.user.id)
            elif operation.value == "republish":
                reg = await self.cog.repo.get_registration(int(election["id"]), user.id)
                if not reg:
                    raise ValueError("未找到报名记录。")
            else:
                raise ValueError("未知操作。")
            await self.cog.publicity.sync_registration_publicity(election, reg, allow_create=bool(reg.get("public_message_id") or election.get("publicity_mode") != PUBLICITY_BATCH or election.get("batch_publicity_status") == BATCH_COMPLETED))
            await self.cog.repo.log(int(election["id"]), interaction.guild.id, interaction.user.id, f"candidate_{operation.value}", {"user_id": user.id, "reason": reason})
            if str(election.get("registration_count_display") or REG_COUNT_DISPLAY_HIDDEN) != REG_COUNT_DISPLAY_HIDDEN:
                fresh = await self.cog.repo.get_election(int(election["id"])) or election
                await self.cog.refresh_registration_entry(fresh, reason=f"candidate_{operation.value}")
            await interaction.edit_original_response(content="操作完成。")
        except Exception as exc:
            await interaction.edit_original_response(content=f"操作失败：{exc}")

    @app_commands.command(name=locale_str("candidate_roles", zh_CN="报名身份组", zh_TW="報名身分組", en_US="报名身份组", en_GB="报名身份组"), description="查看或更新本场募选允许报名身份组")
    @app_commands.rename(
        election_id=locale_str("election_id", zh_CN="募选id", zh_TW="募選id", en_US="募选id", en_GB="募选id"),
        roles=locale_str("roles", zh_CN="身份组列表", zh_TW="身分組列表", en_US="身份组列表", en_GB="身份组列表"),
    )
    @app_commands.describe(election_id="募选 ID；不填时自动选择当前未完成募选", roles="允许报名身份组 ID/提及，逗号或空格分隔；不填则只查看当前设置，传空字符串可清空限制")
    async def candidate_roles(self, interaction: discord.Interaction, election_id: int | None = None, roles: str | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if not self._admin(interaction):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            election = await self.cog.repo.resolve_election(interaction.guild.id, election_id)
            if roles is None:
                role_ids = self.cog.repo.decode_role_ids(election.get("allowed_candidate_role_ids"))
                text = "所有服务器成员可报名" if not role_ids else "拥有以下任意一个身份组可报名：" + "、".join(f"<@&{rid}>" for rid in role_ids)
                await interaction.edit_original_response(content=text, allowed_mentions=discord.AllowedMentions.none())
                return
            if election.get("status") not in (STATUS_SETUP, STATUS_REGISTRATION):
                raise ValueError("只能在未开始或报名阶段修改报名身份组限制。")
            role_ids = [] if roles == "" else parse_role_ids_from_text(roles)
            for rid in role_ids:
                if interaction.guild.get_role(rid) is None:
                    raise ValueError(f"找不到身份组：{rid}")
            await self.cog.repo.set_allowed_candidate_role_ids(int(election["id"]), role_ids)
            await self.cog.repo.log(int(election["id"]), interaction.guild.id, interaction.user.id, "candidate_roles_updated", {"role_ids": role_ids})
            fresh = await self.cog.repo.get_election(int(election["id"])) or election
            await self.cog.refresh_registration_entry(fresh, reason="candidate_roles_updated")
            await interaction.edit_original_response(content="已更新报名身份组限制。" + ("未配置身份组，所有成员可报名。" if not role_ids else "拥有任意一个配置身份组即可报名。"))
        except Exception as exc:
            await interaction.edit_original_response(content=f"设置失败：{exc}")

    @app_commands.command(name=locale_str("voter_roles", zh_CN="投票身份组", zh_TW="投票身分組", en_US="投票身份组", en_GB="投票身份组"), description="查看或更新本场募选允许投票身份组")
    @app_commands.rename(
        election_id=locale_str("election_id", zh_CN="募选id", zh_TW="募選id", en_US="募选id", en_GB="募选id"),
        roles=locale_str("roles", zh_CN="身份组列表", zh_TW="身分組列表", en_US="身份组列表", en_GB="身份组列表"),
    )
    @app_commands.describe(election_id="募选 ID；不填时自动选择当前未完成募选", roles="允许投票身份组 ID/提及，逗号或空格分隔；不填则只查看当前设置，传空字符串可清空限制")
    async def voter_roles(self, interaction: discord.Interaction, election_id: int | None = None, roles: str | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if not self._admin(interaction):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            election = await self.cog.repo.resolve_election(interaction.guild.id, election_id)
            if roles is None:
                role_ids = self.cog.repo.decode_role_ids(election.get("allowed_voter_role_ids"))
                text = "所有服务器成员可投票" if not role_ids else "拥有以下任意一个身份组可投票：" + "、".join(f"<@&{rid}>" for rid in role_ids)
                await interaction.edit_original_response(content=text, allowed_mentions=discord.AllowedMentions.none())
                return
            if election.get("status") in (STATUS_VOTING, STATUS_COMPLETED, STATUS_CANCELLED):
                raise ValueError("投票开始后不能修改投票身份组限制。")
            role_ids = [] if roles == "" else parse_role_ids_from_text(roles)
            for rid in role_ids:
                if interaction.guild.get_role(rid) is None:
                    raise ValueError(f"找不到身份组：{rid}")
            await self.cog.repo.set_allowed_voter_role_ids(int(election["id"]), role_ids)
            await self.cog.repo.log(int(election["id"]), interaction.guild.id, interaction.user.id, "voter_roles_updated", {"role_ids": role_ids})
            fresh = await self.cog.repo.get_election(int(election["id"])) or election
            await self.cog.refresh_registration_entry(fresh, reason="voter_roles_updated")
            await interaction.edit_original_response(content="已更新投票身份组限制。" + ("未配置身份组，所有成员可投票。" if not role_ids else "拥有任意一个配置身份组即可投票。"))
        except Exception as exc:
            await interaction.edit_original_response(content=f"设置失败：{exc}")


    @app_commands.command(name=locale_str("ops_check", zh_CN="运维自检", zh_TW="運維自檢", en_US="运维自检", en_GB="运维自检"), description="运行募选模块自检，不推进状态")
    @app_commands.rename(election_id=locale_str("election_id", zh_CN="募选id", zh_TW="募選id", en_US="募选id", en_GB="募选id"))
    @app_commands.describe(election_id="只检查指定募选 ID；不填则检查当前服务器所有未完成募选")
    async def ops_check(self, interaction: discord.Interaction, election_id: int | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if not self._admin(interaction):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            report = await self.cog.build_ops_check_report(interaction.guild, election_id=election_id)
            await interaction.edit_original_response(content=report[:1900], allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            await interaction.edit_original_response(content=f"自检失败：{exc}")

    @app_commands.command(name=locale_str("ops_tick", zh_CN="运维tick", zh_TW="運維tick", en_US="运维tick", en_GB="运维tick"), description="手动执行一次募选 Scheduler tick，会实际推进到期状态")
    @app_commands.rename(confirm=locale_str("confirm", zh_CN="确认执行", zh_TW="確認執行", en_US="确认执行", en_GB="确认执行"))
    @app_commands.describe(confirm="必须设置为 True；该命令会实际推进到期募选状态")
    async def ops_tick(self, interaction: discord.Interaction, confirm: bool = False) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if not self._admin(interaction):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return
        if not confirm:
            await interaction.response.send_message("该命令会实际推进到期募选状态。若确认执行，请设置 confirm=True。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            before = await self.cog.repo.list_elections(interaction.guild.id, include_completed=False, limit=50)
            await self.cog.scheduler.tick()
            after = await self.cog.repo.list_elections(interaction.guild.id, include_completed=False, limit=50)
            before_map = {int(row["id"]): str(row.get("status")) for row in before}
            after_map = {int(row["id"]): str(row.get("status")) for row in after}
            changed = [f"#{eid}: {old} -> {after_map.get(eid, 'completed/cancelled/hidden')}" for eid, old in before_map.items() if after_map.get(eid) != old]
            await interaction.edit_original_response(content="Scheduler tick 已执行。\n" + ("状态变化：\n" + "\n".join(changed) if changed else "本服务器未发现状态变化。"))
        except Exception as exc:
            await interaction.edit_original_response(content=f"执行失败：{exc}")

    @app_commands.command(name=locale_str("ops_advance", zh_CN="运维推进", zh_TW="運維推進", en_US="运维推进", en_GB="运维推进"), description="按募选 ID 强制推进一场募选到下一阶段")
    @app_commands.rename(
        election_id=locale_str("election_id", zh_CN="募选id", zh_TW="募選id", en_US="募选id", en_GB="募选id"),
        confirm=locale_str("confirm", zh_CN="确认执行", zh_TW="確認執行", en_US="确认执行", en_GB="确认执行"),
    )
    @app_commands.describe(election_id="要推进的募选 ID；该命令必须明确指定", confirm="必须设置为 True；该命令会绕过时间等待并推进一个阶段")
    async def ops_advance(self, interaction: discord.Interaction, election_id: int, confirm: bool = False) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if not self._admin(interaction):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return
        if not confirm:
            await interaction.response.send_message(
                "该命令会按指定募选 ID 强制推进到下一阶段。若确认执行，请设置 确认执行=True。",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            election = await self.cog.repo.resolve_election(interaction.guild.id, election_id)
            message = await self.cog.advance_election_one_phase(election, operator_id=interaction.user.id)
            await interaction.edit_original_response(content=message)
        except Exception as exc:
            await interaction.edit_original_response(content=f"推进失败：{exc}")

    @app_commands.command(name=locale_str("result_preview", zh_CN="计票预览", zh_TW="計票預覽", en_US="计票预览", en_GB="计票预览"), description="仅计算并预览结果，不写入、不发布")
    @app_commands.rename(election_id=locale_str("election_id", zh_CN="募选id", zh_TW="募選id", en_US="募选id", en_GB="募选id"))
    @app_commands.describe(election_id="募选 ID；不填时自动选择当前未完成募选")
    async def result_preview(self, interaction: discord.Interaction, election_id: int | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if not self._admin(interaction):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            election = await self.cog.repo.resolve_election(interaction.guild.id, election_id)
            result = await self.cog.result_service.calculate(election)
            lines = [f"计票预览｜募选 #{election['id']}《{election['name']}》", f"作废：{result.get('is_void')}"]
            if result.get("void_reason"):
                lines.append(f"原因：{result.get('void_reason')}")
            lines.append(f"投票人数：{result.get('total_voters', 0)}；总票数：{result.get('total_votes', 0)}")
            for field in result.get("fields", []):
                winners = field.get("winners") or []
                winner_text = "、".join(f"{w.get('display_name')}({w.get('user_id')}, {w.get('votes')}票)" for w in winners) or "无"
                lines.append(f"- {field.get('field_name')}：{winner_text}；空缺 {field.get('vacancies', 0)}")
            await interaction.edit_original_response(content="\n".join(lines)[:1900], allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            await interaction.edit_original_response(content=f"预览失败：{exc}")


    @app_commands.command(name=locale_str("sync_publicity", zh_CN="同步公示", zh_TW="同步公示", en_US="同步公示", en_GB="同步公示"), description="修复或刷新公示")
    @app_commands.choices(scope=[Choice(name="失败项", value="failed"), Choice(name="全部已公示", value="published"), Choice(name="全部有效候选人", value="active")])
    @app_commands.rename(
        scope=locale_str("scope", zh_CN="范围", zh_TW="範圍", en_US="范围", en_GB="范围"),
        election_id=locale_str("election_id", zh_CN="募选id", zh_TW="募選id", en_US="募选id", en_GB="募选id"),
        user=locale_str("user", zh_CN="候选人", zh_TW="候選人", en_US="候选人", en_GB="候选人"),
    )
    @app_commands.describe(scope="同步范围", election_id="募选 ID；不填时自动选择当前未完成募选", user="只同步指定候选人；不填则按范围同步")
    async def sync_publicity(self, interaction: discord.Interaction, scope: Choice[str], election_id: int | None = None, user: discord.Member | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if not self._admin(interaction):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            election = await self.cog.repo.resolve_election(interaction.guild.id, election_id)
            success, failed = await self.cog.publicity.sync_scope(election, scope=scope.value, user_id=user.id if user else None)
            await interaction.edit_original_response(content=f"同步完成：成功 {success}，失败 {failed}。")
        except Exception as exc:
            await interaction.edit_original_response(content=f"同步失败：{exc}")

    @app_commands.command(name=locale_str("start_vote", zh_CN="开始投票", zh_TW="開始投票", en_US="开始投票", en_GB="开始投票"), description="应急手动开始投票")
    @app_commands.rename(election_id=locale_str("election_id", zh_CN="募选id", zh_TW="募選id", en_US="募选id", en_GB="募选id"))
    @app_commands.describe(election_id="募选 ID；不填时自动选择当前未完成募选")
    async def start_vote(self, interaction: discord.Interaction, election_id: int | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if not self._admin(interaction):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            election = await self.cog.repo.resolve_election(interaction.guild.id, election_id)
            await self.cog.open_voting_phase(election, manual=True)
            await interaction.edit_original_response(content="已尝试开始投票。")
        except Exception as exc:
            await interaction.edit_original_response(content=f"开始投票失败：{exc}")

    @app_commands.command(name=locale_str("finish", zh_CN="结束并计票", zh_TW="結束並計票", en_US="结束并计票", en_GB="结束并计票"), description="应急手动结束并计票")
    @app_commands.rename(election_id=locale_str("election_id", zh_CN="募选id", zh_TW="募選id", en_US="募选id", en_GB="募选id"))
    @app_commands.describe(election_id="募选 ID；不填时自动选择当前未完成募选")
    async def finish(self, interaction: discord.Interaction, election_id: int | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if not self._admin(interaction):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            election = await self.cog.repo.resolve_election(interaction.guild.id, election_id)
            await self.cog.complete_election(election, manual=True)
            await interaction.edit_original_response(content="已结束并计票。")
        except Exception as exc:
            await interaction.edit_original_response(content=f"结束失败：{exc}")

    @app_commands.command(name=locale_str("recalculate", zh_CN="重算结果", zh_TW="重算結果", en_US="重算结果", en_GB="重算结果"), description="重新计算结果")
    @app_commands.rename(election_id=locale_str("election_id", zh_CN="募选id", zh_TW="募選id", en_US="募选id", en_GB="募选id"))
    @app_commands.describe(election_id="要重新计算并发布结果的募选 ID")
    async def recalculate(self, interaction: discord.Interaction, election_id: int) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if not self._admin(interaction):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        election = await self.cog.repo.resolve_election(interaction.guild.id, election_id)
        result = await self.cog.result_service.calculate(election)
        await self.cog.repo.set_result(int(election["id"]), result, void_reason=result.get("void_reason"))
        await self.cog.publicity.publish_result(election, result)
        await interaction.edit_original_response(content="已重算并发布结果。")

    @app_commands.command(name=locale_str("invalidate_vote", zh_CN="清除投票", zh_TW="清除投票", en_US="清除投票", en_GB="清除投票"), description="作废某人的投票记录，不允许重投")
    @app_commands.rename(
        voter=locale_str("voter", zh_CN="投票者", zh_TW="投票者", en_US="投票者", en_GB="投票者"),
        election_id=locale_str("election_id", zh_CN="募选id", zh_TW="募選id", en_US="募选id", en_GB="募选id"),
        reason=locale_str("reason", zh_CN="原因", zh_TW="原因", en_US="原因", en_GB="原因"),
    )
    @app_commands.describe(voter="要清除投票记录的成员", election_id="募选 ID；不填时自动选择当前未完成募选", reason="清除原因，会写入审计日志")
    async def invalidate_vote(self, interaction: discord.Interaction, voter: discord.Member, election_id: int | None = None, reason: str | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if not self._admin(interaction):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return
        election = await self.cog.repo.resolve_election(interaction.guild.id, election_id)
        await self.cog.repo.invalidate_vote(election_id=int(election["id"]), voter_id=voter.id, operator_id=interaction.user.id, reason=reason or "管理员作废投票")
        await self.cog.repo.log(int(election["id"]), interaction.guild.id, interaction.user.id, "vote_invalidated", {"voter_id": voter.id, "reason": reason})
        await interaction.response.send_message("已作废该用户投票记录；该用户不能重新投票。", ephemeral=True)

    @app_commands.command(name=locale_str("cancel", zh_CN="取消", zh_TW="取消", en_US="取消", en_GB="取消"), description="取消未完成募选")
    @app_commands.rename(
        election_id=locale_str("election_id", zh_CN="募选id", zh_TW="募選id", en_US="募选id", en_GB="募选id"),
        reason=locale_str("reason", zh_CN="原因", zh_TW="原因", en_US="原因", en_GB="原因"),
    )
    @app_commands.describe(election_id="募选 ID；不填时自动选择当前未完成募选", reason="取消原因，会写入募选状态和审计日志")
    async def cancel(self, interaction: discord.Interaction, election_id: int | None = None, reason: str | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if not self._admin(interaction):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return
        election = await self.cog.repo.resolve_election(interaction.guild.id, election_id)
        await self.cog.repo.set_election_status(int(election["id"]), STATUS_CANCELLED, void_reason=reason or "管理员取消")
        await self.cog.repo.log(int(election["id"]), interaction.guild.id, interaction.user.id, "election_cancelled", {"reason": reason})
        fresh = await self.cog.repo.get_election(int(election["id"])) or election
        await self.cog.refresh_registration_entry(fresh, reason="election_cancelled")
        await interaction.response.send_message("已取消募选。", ephemeral=True)

    @app_commands.command(name=locale_str("audit", zh_CN="审计", zh_TW="審計", en_US="审计", en_GB="审计"), description="查看募选审计日志")
    @app_commands.rename(
        election_id=locale_str("election_id", zh_CN="募选id", zh_TW="募選id", en_US="募选id", en_GB="募选id"),
        limit=locale_str("limit", zh_CN="数量限制", zh_TW="數量限制", en_US="数量限制", en_GB="数量限制"),
    )
    @app_commands.describe(election_id="只查看指定募选 ID 的审计日志；不填则查看本服务器全部募选日志", limit="返回日志数量，范围 1-50")
    async def audit(self, interaction: discord.Interaction, election_id: int | None = None, limit: int = 20) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if not self._admin(interaction):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return
        logs = await self.cog.repo.list_audit_logs(interaction.guild.id, election_id=election_id, limit=max(1, min(50, limit)))
        lines = [f"#{row['id']}｜election={row.get('election_id')}｜{row['action']}｜operator={row.get('operator_id')}" for row in logs]
        await interaction.response.send_message(("\n".join(lines) or "暂无审计日志。")[:1900], ephemeral=True)

    @app_commands.command(name=locale_str("my_registration", zh_CN="我的报名", zh_TW="我的報名", en_US="我的报名", en_GB="我的报名"), description="查看我的报名")
    @app_commands.rename(election_id=locale_str("election_id", zh_CN="募选id", zh_TW="募選id", en_US="募选id", en_GB="募选id"))
    @app_commands.describe(election_id="募选 ID；不填时自动选择当前未完成募选")
    async def my_registration(self, interaction: discord.Interaction, election_id: int | None = None) -> None:
        await self.cog.show_my_registration(interaction, election_id=election_id)

    @app_commands.command(name=locale_str("help", zh_CN="帮助", zh_TW="幫助", en_US="帮助", en_GB="帮助"), description="募选系统帮助")
    async def help_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embeds=build_help_embeds(), ephemeral=True)


class ElectionCog(commands.Cog, name="ElectionCog"):
    def __init__(self, bot):
        self.bot = bot
        self.repo = ElectionRepo(bot.db)
        self.publicity = PublicityService(bot, self.repo)
        self.result_service = ResultService(self.repo)
        self.vote_service = VoteService(bot, self.repo)
        self.scheduler = ElectionScheduler(self)
        self.group = ElectionGroup(self)
        self._registered_guilds: list[discord.Object] = []
        command_guild_ids = getattr(getattr(bot, "config", None), "command_guild_ids", ())
        if command_guild_ids:
            self._registered_guilds = [discord.Object(id=guild_id) for guild_id in command_guild_ids]
            bot.tree.add_command(self.group, guilds=self._registered_guilds)
        else:
            bot.tree.add_command(self.group)

    async def cog_load(self) -> None:
        await self.repo.ensure_schema()
        self.bot.add_view(RegistrationEntryView(cog=self))
        self.bot.add_view(VoteEntryView(cog=self))
        self.scheduler.start()
        log.info("Election cog loaded")

    async def cog_unload(self) -> None:
        self.scheduler.cancel()
        try:
            if self._registered_guilds:
                for guild in self._registered_guilds:
                    self.bot.tree.remove_command(self.group.name, guild=guild)
            else:
                self.bot.tree.remove_command(self.group.name)
        except Exception:
            pass

    async def _election_from_interaction_message(self, interaction: discord.Interaction, *, vote: bool = False) -> dict[str, Any]:
        if interaction.guild is None or interaction.message is None:
            raise ValueError("无法定位募选消息。")
        finder = self.repo.find_by_vote_message if vote else self.repo.find_by_entry_message
        election = await finder(interaction.guild.id, interaction.message.id)
        if not election:
            raise ValueError("无法根据当前消息定位募选。")
        return election

    async def build_ops_check_report(self, guild: discord.Guild, *, election_id: int | None = None) -> str:
        rows = [await self.repo.resolve_election(guild.id, election_id)] if election_id is not None else await self.repo.list_elections(guild.id, include_completed=False, limit=20)
        if not rows:
            return "募选运维自检：当前服务器没有未完成募选。"
        lines: list[str] = [f"募选运维自检｜Guild {guild.id}", f"检查募选数：{len(rows)}"]
        bot_member = guild.me
        for election in rows:
            fields = await self.repo.list_fields(int(election["id"]))
            counts = await self.repo.count_registrations_by_status(int(election["id"]))
            vote_count = await self.repo.count_vote_records(int(election["id"]))
            allowed_candidate_roles = self.repo.decode_role_ids(election.get("allowed_candidate_role_ids"))
            allowed_voter_roles = self.repo.decode_role_ids(election.get("allowed_voter_role_ids"))
            missing_roles = sorted({rid for rid in [*allowed_candidate_roles, *allowed_voter_roles] if guild.get_role(int(rid)) is None})
            lines.append("")
            lines.append(f"#{election['id']}｜{election['name']}｜状态={election.get('status')}｜公示={election.get('publicity_mode')}")
            lines.append(f"字段数={len(fields)}｜有效报名={counts.get('active', 0)}｜投票记录={vote_count}｜报名身份组={'所有成员' if not allowed_candidate_roles else len(allowed_candidate_roles)}｜投票身份组={'所有成员' if not allowed_voter_roles else len(allowed_voter_roles)}")
            if missing_roles:
                lines.append("⚠️ 缺失身份组：" + "、".join(str(r) for r in missing_roles))
            channels = [
                ("报名频道", election.get("registration_channel_id")),
                ("投票频道", election.get("voting_channel_id")),
                ("公示频道", election.get("public_channel_id")),
                ("告警频道", election.get("alert_channel_id")),
            ]
            for label, channel_id in channels:
                if not channel_id:
                    continue
                channel = guild.get_channel(int(channel_id))
                if not isinstance(channel, discord.TextChannel):
                    lines.append(f"⚠️ {label} {channel_id}：当前缓存中不可见或不是文字频道")
                    continue
                if bot_member is None:
                    lines.append(f"⚠️ {label} {channel.mention}：无法读取 Bot 成员状态")
                    continue
                perms = channel.permissions_for(bot_member)
                missing = []
                if not perms.view_channel:
                    missing.append("View")
                if not perms.send_messages:
                    missing.append("Send")
                if not perms.embed_links:
                    missing.append("Embed")
                if not perms.read_message_history:
                    missing.append("History")
                lines.append(f"{'✅' if not missing else '⚠️'} {label} {channel.mention} 权限：{'OK' if not missing else ','.join(missing)}")
            if election.get("publicity_mode") == PUBLICITY_BATCH:
                lines.append(f"统一公示状态={election.get('batch_publicity_status')} 错误={election.get('batch_publicity_error') or '无'}")
        return "\n".join(lines)


    async def _registration_count_text(self, election: dict[str, Any], fields: list[dict[str, Any]]) -> str | None:
        mode = str(election.get("registration_count_display") or REG_COUNT_DISPLAY_HIDDEN)
        if mode == REG_COUNT_DISPLAY_HIDDEN:
            return None
        registrations = await self.repo.list_active_registrations(int(election["id"]))
        return build_registration_count_text(fields, registrations, mode=mode)

    async def send_registration_entry(self, election: dict, fields: list[dict[str, Any]], *, channel: discord.TextChannel) -> discord.Message:
        count_text = await self._registration_count_text(election, fields)
        msg = await channel.send(embed=build_registration_entry_embed(election, fields, registration_count_text=count_text), view=RegistrationEntryView(cog=self), allowed_mentions=discord.AllowedMentions.none())
        await self.repo.set_registration_entry_message(int(election["id"]), int(msg.id), int(channel.id))
        return msg

    async def refresh_registration_entry(self, election: dict, *, reason: str | None = None) -> bool:
        message_id = int(election.get("registration_entry_message_id") or 0)
        channel_id = int(election.get("registration_entry_channel_id") or election.get("registration_channel_id") or 0)
        if not message_id or not channel_id:
            return False
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                return False
        if not isinstance(channel, discord.TextChannel):
            return False
        fresh = await self.repo.get_election(int(election["id"])) or election
        fields = await self.repo.list_fields(int(fresh["id"]))
        count_text = await self._registration_count_text(fresh, fields)
        try:
            message = await channel.fetch_message(message_id)
            await message.edit(
                embed=build_registration_entry_embed(fresh, fields, registration_count_text=count_text),
                view=RegistrationEntryView(cog=self),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.repo.log(int(fresh["id"]), int(fresh["guild_id"]), None, "registration_entry_refreshed", {"reason": reason})
            return True
        except Exception:
            log.exception("Failed to refresh registration entry for election %s", election.get("id"))
            return False

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

    @staticmethod
    def _candidate_list_page_from_footer(footer_text: str | None, election_id: int) -> int | None:
        prefix = f"Election ID: {int(election_id)}｜候选人名单 "
        text = str(footer_text or "")
        if not text.startswith(prefix):
            return None
        page_text = text[len(prefix) :].split("/", 1)[0].strip()
        try:
            return int(page_text)
        except ValueError:
            return None

    async def _refresh_vote_display(self, election: dict[str, Any]) -> list[str]:
        vote_id = int(election.get("vote_id") or 0)
        message_id = int(election.get("vote_message_id") or 0)
        if not vote_id or not message_id:
            return ["投票面板：未初始化或未记录消息 ID，跳过。"]

        vote = await self.repo.get_vote(vote_id)
        channel_id = int((vote or {}).get("channel_id") or election.get("voting_channel_id") or 0)
        channel = await self._get_text_channel(channel_id)
        if channel is None:
            return ["投票面板：无法读取投票频道。"]

        active_regs = await self.repo.list_active_registrations(int(election["id"]))
        active_regs = await self.vote_service._registrations_with_field_names(int(election["id"]), active_regs, guild=channel.guild, include_usernames=True)
        candidate_list_embeds = build_vote_candidate_list_embeds(election, active_regs)
        lines: list[str] = []
        try:
            message = await channel.fetch_message(message_id)
            await message.edit(
                embeds=[build_vote_entry_embed(election, len(active_regs), guild=channel.guild), *candidate_list_embeds[:1]],
                view=VoteEntryView(cog=self),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            lines.append("投票主面板：成功。")
        except Exception as exc:
            lines.append(f"投票主面板：失败（{str(exc)[:120]}）。")
            return lines

        total_pages = len(candidate_list_embeds)
        if total_pages <= 1:
            lines.append("投票候选人名单：仅 1 页，已随主面板刷新。")
            return lines

        found: dict[int, discord.Message] = {}
        history_error = ""
        try:
            async for history_message in channel.history(limit=200):
                if int(history_message.id) == message_id:
                    continue
                for embed in history_message.embeds:
                    page = self._candidate_list_page_from_footer(getattr(embed.footer, "text", None), int(election["id"]))
                    if page is not None and page > 1 and page not in found:
                        found[page] = history_message
        except Exception as exc:
            history_error = str(exc)[:120]

        refreshed = 0
        missing: list[int] = []
        for page_number, embed in enumerate(candidate_list_embeds[1:], start=2):
            target = found.get(page_number)
            if target is None:
                missing.append(page_number)
                continue
            try:
                await target.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                refreshed += 1
            except Exception:
                missing.append(page_number)

        if history_error:
            lines.append(f"投票候选人名单：主面板外页面未扫描完成（{history_error}）。")
        lines.append(f"投票候选人名单：额外页成功 {refreshed}，未定位/失败 {len(missing)}。")
        if missing:
            lines.append("未刷新页码：" + "、".join(str(page) for page in missing[:20]))
        return lines

    async def refresh_display_messages(self, guild: discord.Guild, election: dict[str, Any], *, scope: str = "auto") -> str:
        scope = str(scope or "auto")
        valid_scopes = {"auto", "all", "entry", "publicity", "vote"}
        if scope not in valid_scopes:
            raise ValueError("未知刷新范围。")

        status = str(election.get("status") or "")
        if scope == "auto":
            if status in (STATUS_SETUP, STATUS_REGISTRATION):
                scopes = ["entry"]
            elif status == STATUS_REGISTRATION_ENDED:
                scopes = ["entry", "publicity"]
            elif status == STATUS_VOTING:
                scopes = ["entry", "publicity", "vote"]
            else:
                scopes = ["entry", "publicity", "vote"]
        elif scope == "all":
            scopes = ["entry", "publicity", "vote"]
        else:
            scopes = [scope]

        fresh = await self.repo.get_election(int(election["id"])) or election
        lines = [f"刷新展示｜募选 #{fresh['id']}《{fresh['name']}》", f"范围：{scope}｜状态：{fresh.get('status')}"]
        if "entry" in scopes:
            ok = await self.refresh_registration_entry(fresh, reason="manual_refresh_display")
            lines.append("报名入口：" + ("成功。" if ok else "未刷新（未记录入口消息或无法编辑）。"))
        if "publicity" in scopes:
            success, failed = await self.publicity.sync_scope(fresh, scope="published")
            lines.append(f"公示：成功 {success}，失败 {failed}。")
        if "vote" in scopes:
            lines.extend(await self._refresh_vote_display(fresh))
        await self.repo.log(int(fresh["id"]), int(guild.id), None, "display_refreshed", {"scope": scope, "scopes": scopes})
        return "\n".join(lines)

    async def can_user_register(self, user: discord.abc.User | discord.Member, election: dict[str, Any]) -> bool:
        if not isinstance(user, discord.Member):
            return False
        allowed_candidate_roles = self.repo.decode_role_ids(election.get("allowed_candidate_role_ids"))
        return can_register(user, allowed_candidate_roles)

    async def open_registration_flow(self, interaction: discord.Interaction, *, is_edit: bool) -> None:
        try:
            election = await self._election_from_interaction_message(interaction)
        except Exception as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        allowed_candidate_roles = self.repo.decode_role_ids(election.get("allowed_candidate_role_ids"))
        if not await self.can_user_register(interaction.user, election):
            await interaction.response.send_message(
                missing_candidate_role_message(allowed_candidate_roles) or "你没有报名资格。",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        existing = await self.repo.get_registration(int(election["id"]), interaction.user.id)
        if election.get("status") != STATUS_REGISTRATION:
            if is_edit and election.get("status") == STATUS_REGISTRATION_ENDED and existing and existing.get("status") == REG_REJECTED:
                selected_field_keys = self.repo.decode_field_keys(existing.get("selected_field_keys"))
                await interaction.response.send_modal(
                    RegistrationIntroModal(
                        cog=self,
                        election_id=int(election["id"]),
                        selected_field_keys=selected_field_keys,
                        is_edit=True,
                        intro_only=True,
                        current_intro=str(existing.get("self_intro") or ""),
                    )
                )
                return
            await interaction.response.send_message("当前不在报名阶段。", ephemeral=True)
            return
        if is_edit and not existing:
            await interaction.response.send_message("你尚未报名，不能编辑。", ephemeral=True)
            return
        if existing and existing.get("status") == REG_REVOKED:
            await interaction.response.send_message("你的报名已被撤销，不能自助报名，请联系管理员。", ephemeral=True)
            return
        fields = await self.repo.list_fields(int(election["id"]))
        existing_field_keys = self.repo.decode_field_keys(existing.get("selected_field_keys")) if existing else None
        await interaction.response.send_message(
            "请选择你愿意参选的岗位：",
            view=FieldSelectView(cog=self, election=election, fields=fields, is_edit=is_edit, existing_field_keys=existing_field_keys),
            ephemeral=True,
        )

    async def handle_registration_submit(self, interaction: discord.Interaction, *, election_id: int, selected_field_keys: list[str], self_intro: str, is_edit: bool, intro_only: bool = False) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        election = await self.repo.get_election(election_id)
        if not election:
            await interaction.response.send_message("未找到募选。", ephemeral=True)
            return
        allowed_candidate_roles = self.repo.decode_role_ids(election.get("allowed_candidate_role_ids"))
        if not await self.can_user_register(interaction.user, election):
            await interaction.response.send_message(
                missing_candidate_role_message(allowed_candidate_roles) or "你没有报名资格。",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        existing = await self.repo.get_registration(election_id, interaction.user.id)
        if intro_only:
            if election.get("status") != STATUS_REGISTRATION_ENDED:
                await interaction.response.send_message("当前不在公示阶段，不能使用仅修改宣言流程。", ephemeral=True)
                return
            if not existing or existing.get("status") != REG_REJECTED:
                await interaction.response.send_message("只有公示阶段被打回的报名可以仅修改宣言。", ephemeral=True)
                return
            original_field_keys = self.repo.decode_field_keys(existing.get("selected_field_keys"))
            submitted_field_keys = [str(key) for key in selected_field_keys]
            if submitted_field_keys != original_field_keys:
                await interaction.response.send_message("公示阶段只能修改参选宣言，不能修改参选岗位。", ephemeral=True)
                return
            selected_field_keys = original_field_keys
        elif election.get("status") != STATUS_REGISTRATION:
            await interaction.response.send_message("当前不在报名阶段。", ephemeral=True)
            return
        if contains_forbidden_mention(self_intro):
            await interaction.response.send_message("参选宣言不能包含用户提及、身份组提及、@everyone 或 @here。", ephemeral=True)
            return
        if len(self_intro or "") > MAX_SELF_INTRO_LENGTH:
            await interaction.response.send_message(f"参选宣言最多 {MAX_SELF_INTRO_LENGTH} 字。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        display_name = str(existing.get("display_name") or interaction.user.display_name) if intro_only and existing else interaction.user.display_name
        reg = await self.repo.upsert_registration(
            election=election,
            user_id=interaction.user.id,
            display_name=display_name,
            selected_field_keys=selected_field_keys,
            self_intro=self_intro,
            is_re_register_after_withdraw=bool(existing and existing.get("status") == REG_WITHDRAWN and not is_edit),
            public_status_override=PUBLIC_PENDING if intro_only else None,
        )
        if intro_only or election.get("publicity_mode") == PUBLICITY_REALTIME:
            await self.publicity.sync_registration_publicity(election, reg, allow_create=True)
        await self.repo.log(election_id, interaction.guild.id, interaction.user.id, "registration_submitted", {"fields": selected_field_keys, "is_edit": is_edit, "intro_only": intro_only})
        if str(election.get("registration_count_display") or REG_COUNT_DISPLAY_HIDDEN) != REG_COUNT_DISPLAY_HIDDEN:
            fresh = await self.repo.get_election(election_id) or election
            await self.refresh_registration_entry(fresh, reason="registration_submitted")
        if intro_only:
            message = "参选宣言已更新，并已重新提交为有效报名。"
        else:
            message = "报名已保存。" + ("实时公示已同步。" if election.get("publicity_mode") == PUBLICITY_REALTIME else "本场为统一公示模式，报名期内不会公开你的报名。")
        await interaction.edit_original_response(content=message)

    async def show_my_registration(self, interaction: discord.Interaction, election_id: int | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        try:
            election = await self.repo.resolve_election(interaction.guild.id, election_id)
            reg = await self.repo.get_registration(int(election["id"]), interaction.user.id)
            if not reg:
                await interaction.response.send_message("你尚未报名。", ephemeral=True)
                return
            fields = await self.repo.get_field_names_by_key(int(election["id"]))
            names = [fields.get(k, k) for k in self.repo.decode_field_keys(reg.get("selected_field_keys"))]
            status_label = REGISTRATION_STATUS_LABELS.get(str(reg.get("status")), str(reg.get("status")))
            public_status_label = PUBLIC_SYNC_STATUS_LABELS.get(str(reg.get("public_sync_status")), str(reg.get("public_sync_status")))
            content = (
                f"募选：#{election['id']} {election['name']}\n"
                f"报名状态：{status_label}\n"
                f"参选岗位：{'、'.join(names) or '无'}\n"
                f"报名时间：{format_time_pair(reg.get('registered_at'))}\n"
                f"公示状态：{public_status_label}"
            )
            await interaction.response.send_message(content[:1900], ephemeral=True)
        except Exception as exc:
            await interaction.response.send_message(f"查询失败：{exc}", ephemeral=True)

    async def withdraw_registration(self, interaction: discord.Interaction) -> None:
        try:
            election = await self._election_from_interaction_message(interaction)
        except Exception as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if election.get("status") != STATUS_REGISTRATION:
            await interaction.response.send_message("当前不在报名阶段，不能撤回。", ephemeral=True)
            return
        reg = await self.repo.get_registration(int(election["id"]), interaction.user.id)
        if not reg:
            await interaction.response.send_message("你尚未报名。", ephemeral=True)
            return
        reg = await self.repo.set_registration_status(election_id=int(election["id"]), user_id=interaction.user.id, status=REG_WITHDRAWN, reason="用户撤回", operator_id=interaction.user.id)
        if election.get("publicity_mode") == PUBLICITY_REALTIME or reg.get("public_message_id"):
            await self.publicity.sync_registration_publicity(election, reg, allow_create=False)
        if str(election.get("registration_count_display") or REG_COUNT_DISPLAY_HIDDEN) != REG_COUNT_DISPLAY_HIDDEN:
            fresh = await self.repo.get_election(int(election["id"])) or election
            await self.refresh_registration_entry(fresh, reason="registration_withdrawn")
        await interaction.response.send_message("已撤回报名。若在报名期内重新报名，将刷新报名时间。", ephemeral=True)

    async def start_vote_interaction(self, interaction: discord.Interaction) -> None:
        try:
            election = await self._election_from_interaction_message(interaction, vote=True)
        except Exception as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.vote_service.open_vote_selection(interaction, election)

    async def confirm_vote(self, interaction: discord.Interaction, *, election: dict, selected_user_ids: list[int]) -> None:
        await self.vote_service.confirm_vote(interaction, election=election, selected_user_ids=selected_user_ids)

    async def close_registration_phase(self, election: dict) -> None:
        if election.get("status") != STATUS_REGISTRATION:
            return
        await self.repo.set_election_status(int(election["id"]), STATUS_REGISTRATION_ENDED)
        await self.repo.log(int(election["id"]), int(election["guild_id"]), None, "registration_ended", {})
        if election.get("publicity_mode") == PUBLICITY_REALTIME and election.get("registration_entry_message_id"):
            await self.publicity.sync_scope(election, scope="active")
        fresh = await self.repo.get_election(int(election["id"])) or election
        await self.refresh_registration_entry(fresh, reason="registration_ended")
        active = await self.repo.list_active_registrations(int(election["id"]))
        if not active:
            await self.complete_election(election, void_reason="无人有效报名，本次募选作废")
            return
        if election.get("publicity_mode") == PUBLICITY_BATCH:
            await self.publicity.publish_batch_publicity(election)

    async def advance_election_one_phase(self, election: dict, *, operator_id: int) -> str:
        fresh = await self.repo.get_election(int(election["id"])) or election
        old_status = str(fresh.get("status"))
        if old_status == STATUS_SETUP:
            await self.repo.set_election_status(int(fresh["id"]), STATUS_REGISTRATION)
            updated = await self.repo.get_election(int(fresh["id"])) or fresh
            await self.refresh_registration_entry(updated, reason="manual_advance_to_registration")
            await self.repo.log(int(fresh["id"]), int(fresh["guild_id"]), operator_id, "manual_advance_to_registration", {})
            return f"已推进募选 #{fresh['id']}：未开始 -> 报名中。"
        if old_status == STATUS_REGISTRATION:
            await self.close_registration_phase(fresh)
            updated = await self.repo.get_election(int(fresh["id"])) or fresh
            await self.repo.log(int(fresh["id"]), int(fresh["guild_id"]), operator_id, "manual_advance_from_registration", {"status": updated.get("status")})
            return f"已推进募选 #{fresh['id']}：报名中 -> {updated.get('status')}。"
        if old_status == STATUS_REGISTRATION_ENDED:
            await self.open_voting_phase(fresh, manual=True)
            updated = await self.repo.get_election(int(fresh["id"])) or fresh
            await self.repo.log(int(fresh["id"]), int(fresh["guild_id"]), operator_id, "manual_advance_from_publicity", {"status": updated.get("status")})
            return f"已推进募选 #{fresh['id']}：报名结束/公示期 -> {updated.get('status')}。"
        if old_status == STATUS_VOTING:
            await self.complete_election(fresh, manual=True)
            updated = await self.repo.get_election(int(fresh["id"])) or fresh
            return f"已推进募选 #{fresh['id']}：投票中 -> {updated.get('status')}。"
        raise ValueError("该募选已经完成或取消，不能继续推进。")

    async def open_voting_phase(self, election: dict, *, manual: bool = False) -> None:
        fresh = await self.repo.get_election(int(election["id"])) or election
        if manual and fresh.get("status") == STATUS_REGISTRATION:
            await self.close_registration_phase(fresh)
            fresh = await self.repo.get_election(int(election["id"])) or fresh
        if fresh.get("status") == STATUS_COMPLETED:
            return
        if fresh.get("status") == STATUS_VOTING:
            if int(fresh.get("vote_id") or 0) and int(fresh.get("vote_message_id") or 0):
                return
            await self.vote_service.create_vote_panel(fresh)
            await self.repo.log(int(fresh["id"]), int(fresh["guild_id"]), None, "voting_panel_repaired", {"manual": manual})
            return
        if fresh.get("status") != STATUS_REGISTRATION_ENDED:
            if manual:
                raise ValueError("当前状态不能开始投票；需处于报名中或报名结束/公示期。")
            return
        if fresh.get("publicity_mode") == PUBLICITY_BATCH and fresh.get("batch_publicity_status") != BATCH_COMPLETED:
            if fresh.get("batch_publicity_status") in (BATCH_PARTIAL_FAILED, "pending", "publishing"):
                if manual:
                    raise ValueError("统一公示尚未完整成功，不能开始投票。")
                await self.complete_election(fresh, void_reason="统一公示未完整成功，本次募选作废")
                return
        active = await self.repo.list_active_registrations(int(fresh["id"]))
        if not active:
            await self.complete_election(fresh, void_reason="无人有效报名，本次募选作废")
            return
        if fresh.get("vote_message_id"):
            await self.repo.set_election_status(int(fresh["id"]), STATUS_VOTING)
            updated = await self.repo.get_election(int(fresh["id"])) or fresh
            await self.refresh_registration_entry(updated, reason="voting_started_existing_panel")
            return
        await self.repo.set_election_status(int(fresh["id"]), STATUS_VOTING)
        fresh = await self.repo.get_election(int(fresh["id"])) or fresh
        await self.refresh_registration_entry(fresh, reason="voting_started")
        await self.vote_service.create_vote_panel(fresh)
        await self.repo.log(int(fresh["id"]), int(fresh["guild_id"]), None, "voting_started", {"manual": manual})

    async def complete_election(self, election: dict, *, manual: bool = False, void_reason: str | None = None) -> None:
        fresh = await self.repo.get_election(int(election["id"])) or election
        if fresh.get("status") == STATUS_COMPLETED:
            return
        result = await self.result_service.calculate(fresh, void_reason=void_reason)
        await self.repo.set_result(int(fresh["id"]), result, void_reason=result.get("void_reason"))
        if fresh.get("vote_id"):
            await self.repo.set_vote_closed_at(int(fresh["vote_id"]))
        await self.repo.set_election_status(int(fresh["id"]), STATUS_COMPLETED, completed_at=utc_now_iso(), void_reason=result.get("void_reason"))
        completed = await self.repo.get_election(int(fresh["id"])) or fresh
        await self.refresh_registration_entry(completed, reason="election_completed")
        await self.publicity.publish_result(fresh, result)
        await self.repo.log(int(fresh["id"]), int(fresh["guild_id"]), None, "election_completed", {"manual": manual, "void_reason": result.get("void_reason")})


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ElectionCog(bot))
