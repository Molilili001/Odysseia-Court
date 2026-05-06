from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.app_commands import Choice, locale_str
from discord.ext import commands, tasks

from .candidate_service import CandidateService
from .archive_service import InspectionArchiveService
from .case_service import CaseService
from .constants import (
    ARCHIVE_ACTION_DELETE,
    ARCHIVE_ACTION_LOCK,
    ARCHIVE_ACTION_ONLY,
    CASE_MAINTENANCE_INTERVAL_MINUTES,
    CASE_OP_BAN_DRAW,
    CASE_OP_CANCEL,
    CASE_OP_DRAW,
    CASE_OP_REPLACE,
    CASE_OP_STATUS,
    CANDIDATE_MAINTENANCE_INTERVAL_HOURS,
    INSPECTION_DB_PATH,
    MEMBER_OP_ADD,
    MEMBER_OP_CONFIRM,
    MEMBER_OP_LIST,
    MEMBER_OP_REMOVE,
    MEMBER_OP_SELF_EXIT,
    VOTE_NO,
    VOTE_YES,
)
from .database import InspectionDatabase
from .settings_service import InspectionSettingsService
from .utils import format_dt, is_server_admin
from .vote_service import VoteService


log = logging.getLogger(__name__)


def _admin_required(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and is_server_admin(interaction.user)


class InspectionGroup(app_commands.Group):
    def __init__(self, cog: "InspectionCog"):
        super().__init__(
            name=locale_str(
                "inspection",
                zh_CN="监察",
                zh_TW="監察",
                en_US="监察",
                en_GB="监察",
            ),
            description=locale_str(
                "Inspection team system",
                zh_CN="监察组系统",
                zh_TW="監察組系統",
                en_US="监察组系统",
                en_GB="监察组系统",
            ),
        )
        self.cog = cog

    @app_commands.command(
        name=locale_str("setup", zh_CN="设置", zh_TW="設定", en_US="设置", en_GB="设置"),
        description=locale_str(
            "Configure inspection module",
            zh_CN="查看或更新监察模块设置",
            zh_TW="查看或更新監察模組設定",
            en_US="查看或更新监察模块设置",
            en_GB="查看或更新监察模块设置",
        ),
    )
    @app_commands.rename(
        candidate_role=locale_str("candidate_role", zh_CN="监察候补身份组", zh_TW="監察候補身分組", en_US="监察候补身份组", en_GB="监察候补身份组"),
        admin_notice_channel=locale_str("admin_notice_channel", zh_CN="admin通知频道", zh_TW="admin通知頻道", en_US="admin通知频道", en_GB="admin通知频道"),
        discussion_category=locale_str("discussion_category", zh_CN="临时讨论频道分类", zh_TW="臨時討論頻道分類", en_US="临时讨论频道分类", en_GB="临时讨论频道分类"),
        verdict_channel=locale_str("verdict_channel", zh_CN="裁决公示频道", zh_TW="裁決公示頻道", en_US="裁决公示频道", en_GB="裁决公示频道"),
        retention_days=locale_str("retention_days", zh_CN="留任确认周期天数", zh_TW="留任確認週期天數", en_US="留任确认周期天数", en_GB="留任确认周期天数"),
        archive_channel=locale_str("archive_channel", zh_CN="归档频道", zh_TW="歸檔頻道", en_US="归档频道", en_GB="归档频道"),
    )
    @app_commands.describe(
        candidate_role="监察候补身份组",
        admin_notice_channel="后台/管理通知频道",
        discussion_category="用于创建临时私密讨论频道的分类",
        verdict_channel="裁决结果公示频道",
        retention_days="留任确认周期天数，默认 30，至少 1",
        archive_channel="监察归档频道（可选；不影响核心监察命令）",
    )
    async def setup(
        self,
        interaction: discord.Interaction,
        candidate_role: discord.Role | None = None,
        admin_notice_channel: discord.TextChannel | None = None,
        discussion_category: discord.CategoryChannel | None = None,
        verdict_channel: discord.TextChannel | None = None,
        retention_days: int | None = None,
        archive_channel: discord.TextChannel | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if not _admin_required(interaction):
            await interaction.response.send_message("无权限（需要服务器所有者或 Administrator 权限）。", ephemeral=True)
            return
        if retention_days is not None and retention_days < 1:
            await interaction.response.send_message("留任确认周期天数至少为 1。", ephemeral=True)
            return

        has_update = any((candidate_role, admin_notice_channel, discussion_category, verdict_channel, retention_days is not None, archive_channel))
        if has_update:
            settings = await self.cog.settings_service.update_settings(
                interaction.guild.id,
                candidate_role_id=candidate_role.id if candidate_role else None,
                admin_notice_channel_id=admin_notice_channel.id if admin_notice_channel else None,
                discussion_category_id=discussion_category.id if discussion_category else None,
                verdict_channel_id=verdict_channel.id if verdict_channel else None,
                retention_days=retention_days,
                archive_channel_id=archive_channel.id if archive_channel else None,
            )
            suffix = "\n\n配置已完整，可以使用其他监察命令。" if settings.is_complete else "\n\n配置尚未完整，缺少：" + "、".join(settings.missing_labels())
            await interaction.response.send_message("已保存监察模块设置。\n" + settings.render() + suffix, ephemeral=True)
            return

        settings = await self.cog.settings_service.get_settings(interaction.guild.id)
        suffix = "\n\n配置状态：完整。" if settings.is_complete else "\n\n配置状态：不完整，缺少：" + "、".join(settings.missing_labels())
        await interaction.response.send_message(settings.render() + suffix, ephemeral=True)

    @app_commands.command(
        name=locale_str("members", zh_CN="成员管理", zh_TW="成員管理", en_US="成员管理", en_GB="成员管理"),
        description=locale_str(
            "Manage inspection candidates",
            zh_CN="添加、移除、退出、查看或确认监察候补",
            zh_TW="新增、移除、退出、查看或確認監察候補",
            en_US="添加、移除、退出、查看或确认监察候补",
            en_GB="添加、移除、退出、查看或确认监察候补",
        ),
    )
    @app_commands.rename(
        operation=locale_str("operation", zh_CN="操作", zh_TW="操作", en_US="操作", en_GB="操作"),
        user=locale_str("user", zh_CN="用户", zh_TW="使用者", en_US="用户", en_GB="用户"),
        reason=locale_str("reason", zh_CN="原因", zh_TW="原因", en_US="原因", en_GB="原因"),
    )
    @app_commands.describe(operation="要执行的成员管理操作", user="目标用户", reason="原因（移除时建议填写）")
    @app_commands.choices(
        operation=[
            Choice(name="添加候补", value=MEMBER_OP_ADD),
            Choice(name="移除候补", value=MEMBER_OP_REMOVE),
            Choice(name="主动退出", value=MEMBER_OP_SELF_EXIT),
            Choice(name="查看名单", value=MEMBER_OP_LIST),
            Choice(name="设置留任", value=MEMBER_OP_CONFIRM),
        ]
    )
    async def members(
        self,
        interaction: discord.Interaction,
        operation: Choice[str],
        user: discord.Member | None = None,
        reason: str | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        _, config_error = await self.cog.settings_service.validate_complete(interaction.guild)
        if config_error:
            await interaction.response.send_message(config_error, ephemeral=True)
            return

        op = operation.value
        if op == MEMBER_OP_SELF_EXIT:
            if not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message("无法读取你的成员信息。", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                message = await self.cog.candidate_service.self_exit_candidate(interaction.guild, interaction.user)
            except Exception as exc:
                message = f"操作失败：{exc}"
            await interaction.edit_original_response(content=message)
            return

        if not _admin_required(interaction):
            await interaction.response.send_message("无权限（需要服务器所有者或 Administrator 权限）。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if op == MEMBER_OP_LIST:
                rows = await self.cog.candidate_service.list_candidates(interaction.guild.id)
                message = self.cog.candidate_service.render_candidate_list(rows)
            elif op == MEMBER_OP_ADD:
                if user is None:
                    raise ValueError("添加候补需要指定用户。")
                message = await self.cog.candidate_service.add_candidate(interaction.guild, user)
            elif op == MEMBER_OP_REMOVE:
                if user is None:
                    raise ValueError("移除候补需要指定用户。")
                message = await self.cog.candidate_service.remove_candidate(interaction.guild, user.id, reason=reason or "管理员移除。")
            elif op == MEMBER_OP_CONFIRM:
                if user is None:
                    raise ValueError("设置留任需要指定用户。")
                message = await self.cog.candidate_service.confirm_retention(interaction.guild, user)
            else:
                message = "未知操作。"
        except Exception as exc:
            message = f"操作失败：{exc}"
        await interaction.edit_original_response(content=message[:1900])

    @app_commands.command(
        name=locale_str("start", zh_CN="启动监察", zh_TW="啟動監察", en_US="启动监察", en_GB="启动监察"),
        description=locale_str(
            "Start an inspection case",
            zh_CN="创建监察案件并邀请候补响应",
            zh_TW="建立監察案件並邀請候補回應",
            en_US="创建监察案件并邀请候补响应",
            en_GB="创建监察案件并邀请候补响应",
        ),
    )
    @app_commands.rename(
        description=locale_str("description", zh_CN="案件说明", zh_TW="案件說明", en_US="案件说明", en_GB="案件说明"),
        complainant_statement=locale_str("complainant_statement", zh_CN="投诉方说明", zh_TW="投訴方說明", en_US="投诉方说明", en_GB="投诉方说明"),
        defendant_statement=locale_str("defendant_statement", zh_CN="被投诉方说明", zh_TW="被投訴方說明", en_US="被投诉方说明", en_GB="被投诉方说明"),
        response_hours=locale_str("response_hours", zh_CN="响应期限小时", zh_TW="回應期限小時", en_US="响应期限小时", en_GB="响应期限小时"),
        ban_hours=locale_str("ban_hours", zh_CN="ban阶段期限小时", zh_TW="ban階段期限小時", en_US="ban阶段期限小时", en_GB="ban阶段期限小时"),
        material_link=locale_str("material_link", zh_CN="材料链接", zh_TW="材料連結", en_US="材料链接", en_GB="材料链接"),
    )
    @app_commands.describe(
        description="案件说明",
        complainant_statement="投诉方说明",
        defendant_statement="被投诉方说明",
        response_hours="候补响应期限，至少 1 小时",
        ban_hours="响应结束后 Ban 阶段期限，至少 1 小时",
        material_link="材料链接（可选）",
    )
    async def start(
        self,
        interaction: discord.Interaction,
        description: str,
        complainant_statement: str,
        defendant_statement: str,
        response_hours: int,
        ban_hours: int,
        material_link: str | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if not _admin_required(interaction):
            await interaction.response.send_message("无权限（需要服务器所有者或 Administrator 权限）。", ephemeral=True)
            return
        _, config_error = await self.cog.settings_service.validate_complete(interaction.guild)
        if config_error:
            await interaction.response.send_message(config_error, ephemeral=True)
            return
        if response_hours < 1 or ban_hours < 1:
            await interaction.response.send_message("响应期限和 Ban 阶段期限都至少为 1 小时。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            case, stats = await self.cog.case_service.create_case(
                interaction.guild,
                created_by=interaction.user.id,
                description=description,
                complainant_statement=complainant_statement,
                defendant_statement=defendant_statement,
                response_hours=response_hours,
                ban_hours=ban_hours,
                material_link=material_link,
            )
            message = (
                f"已创建监察案件 #{int(case['id'])}。\n"
                f"- 已发送邀请：{stats['invited']}\n"
                f"- DM 失败：{stats['dm_failed']}\n"
                f"- 因不在服务器/缺身份组跳过：{stats['skipped']}\n"
                f"- 响应截止：{format_dt(case.get('response_deadline_at'))}"
            )
        except Exception as exc:
            message = f"创建监察案件失败：{exc}"
        await interaction.edit_original_response(content=message[:1900])

    @app_commands.command(
        name=locale_str("case", zh_CN="案件管理", zh_TW="案件管理", en_US="案件管理", en_GB="案件管理"),
        description=locale_str(
            "Manage inspection cases",
            zh_CN="查看状态、Ban并抽取、无Ban抽取、补抽或取消案件",
            zh_TW="查看狀態、Ban並抽取、無Ban抽取、補抽或取消案件",
            en_US="查看状态、Ban并抽取、无Ban抽取、补抽或取消案件",
            en_GB="查看状态、Ban并抽取、无Ban抽取、补抽或取消案件",
        ),
    )
    @app_commands.rename(
        operation=locale_str("operation", zh_CN="操作", zh_TW="操作", en_US="操作", en_GB="操作"),
        case_id=locale_str("case_id", zh_CN="案件id", zh_TW="案件id", en_US="案件id", en_GB="案件id"),
        complainant_ban1=locale_str("complainant_ban1", zh_CN="投诉方ban1", zh_TW="投訴方ban1", en_US="投诉方ban1", en_GB="投诉方ban1"),
        complainant_ban2=locale_str("complainant_ban2", zh_CN="投诉方ban2", zh_TW="投訴方ban2", en_US="投诉方ban2", en_GB="投诉方ban2"),
        defendant_ban1=locale_str("defendant_ban1", zh_CN="被投诉方ban1", zh_TW="被投訴方ban1", en_US="被投诉方ban1", en_GB="被投诉方ban1"),
        defendant_ban2=locale_str("defendant_ban2", zh_CN="被投诉方ban2", zh_TW="被投訴方ban2", en_US="被投诉方ban2", en_GB="被投诉方ban2"),
        replacement_user=locale_str("replacement_user", zh_CN="替换用户", zh_TW="替換使用者", en_US="替换用户", en_GB="替换用户"),
        reason=locale_str("reason", zh_CN="原因", zh_TW="原因", en_US="原因", en_GB="原因"),
    )
    @app_commands.describe(
        operation="案件管理操作",
        case_id="监察案件 ID",
        complainant_ban1="投诉方 Ban 1",
        complainant_ban2="投诉方 Ban 2",
        defendant_ban1="被投诉方 Ban 1",
        defendant_ban2="被投诉方 Ban 2",
        replacement_user="补抽时要替换掉的当前临时监察成员",
        reason="取消或补抽原因",
    )
    @app_commands.choices(
        operation=[
            Choice(name="查看状态", value=CASE_OP_STATUS),
            Choice(name="Ban并抽取", value=CASE_OP_BAN_DRAW),
            Choice(name="无Ban抽取", value=CASE_OP_DRAW),
            Choice(name="补抽", value=CASE_OP_REPLACE),
            Choice(name="取消", value=CASE_OP_CANCEL),
        ]
    )
    async def case_manage(
        self,
        interaction: discord.Interaction,
        operation: Choice[str],
        case_id: int,
        complainant_ban1: discord.Member | None = None,
        complainant_ban2: discord.Member | None = None,
        defendant_ban1: discord.Member | None = None,
        defendant_ban2: discord.Member | None = None,
        replacement_user: discord.Member | None = None,
        reason: str | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if not _admin_required(interaction):
            await interaction.response.send_message("无权限（需要服务器所有者或 Administrator 权限）。", ephemeral=True)
            return
        _, config_error = await self.cog.settings_service.validate_complete(interaction.guild)
        if config_error:
            await interaction.response.send_message(config_error, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            op = operation.value
            if op == CASE_OP_STATUS:
                message = await self.cog.case_service.render_case_status(case_id)
            elif op == CASE_OP_BAN_DRAW:
                message = await self.cog.case_service.ban_and_draw(
                    interaction.guild,
                    case_id,
                    operator_id=interaction.user.id,
                    complainant_bans=[complainant_ban1.id if complainant_ban1 else None, complainant_ban2.id if complainant_ban2 else None],
                    defendant_bans=[defendant_ban1.id if defendant_ban1 else None, defendant_ban2.id if defendant_ban2 else None],
                )
            elif op == CASE_OP_DRAW:
                message = await self.cog.case_service.draw_case(
                    interaction.guild,
                    case_id,
                    operator_id=interaction.user.id,
                    ban_user_ids=[],
                )
            elif op == CASE_OP_REPLACE:
                if replacement_user is None:
                    raise ValueError("补抽需要指定替换用户。")
                message = await self.cog.case_service.replace_member(
                    interaction.guild,
                    case_id,
                    replaced_user_id=replacement_user.id,
                    operator_id=interaction.user.id,
                    reason=reason,
                )
            elif op == CASE_OP_CANCEL:
                message = await self.cog.case_service.cancel_case(interaction.guild, case_id, reason=reason)
            else:
                message = "未知操作。"
        except Exception as exc:
            message = f"操作失败：{exc}"
        await interaction.edit_original_response(content=message[:1900])

    @app_commands.command(
        name=locale_str("vote_panel", zh_CN="投票面板", zh_TW="投票面板", en_US="投票面板", en_GB="投票面板"),
        description=locale_str(
            "Create inspection vote panel",
            zh_CN="在本案临时讨论频道创建匿名投票面板",
            zh_TW="在本案臨時討論頻道建立匿名投票面板",
            en_US="在本案临时讨论频道创建匿名投票面板",
            en_GB="在本案临时讨论频道创建匿名投票面板",
        ),
    )
    @app_commands.rename(
        vote_hours=locale_str("vote_hours", zh_CN="投票时限小时", zh_TW="投票時限小時", en_US="投票时限小时", en_GB="投票时限小时"),
        case_id=locale_str("case_id", zh_CN="案件id", zh_TW="案件id", en_US="案件id", en_GB="案件id"),
    )
    @app_commands.describe(vote_hours="投票时限，至少 1 小时", case_id="监察案件 ID；不填则从当前频道反查")
    async def vote_panel(
        self,
        interaction: discord.Interaction,
        vote_hours: int,
        case_id: int | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if vote_hours < 1:
            await interaction.response.send_message("投票时限至少为 1 小时。", ephemeral=True)
            return
        _, config_error = await self.cog.settings_service.validate_complete(interaction.guild)
        if config_error:
            await interaction.response.send_message(config_error, ephemeral=True)
            return
        is_admin = _admin_required(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            message = await self.cog.vote_service.start_vote_panel(
                interaction,
                case_id=case_id,
                vote_hours=vote_hours,
                is_admin=is_admin,
            )
        except Exception as exc:
            message = f"创建投票面板失败：{exc}"
        await interaction.edit_original_response(content=message[:1900])

    @app_commands.command(
        name=locale_str("archive", zh_CN="归档", zh_TW="歸檔", en_US="归档", en_GB="归档"),
        description=locale_str(
            "Archive an inspection case",
            zh_CN="导出监察临时讨论频道为 HTML/ZIP，可选择保留、锁定或删除原频道",
            zh_TW="匯出監察臨時討論頻道為 HTML/ZIP，可選擇保留、鎖定或刪除原頻道",
            en_US="导出监察临时讨论频道为 HTML/ZIP，可选择保留、锁定或删除原频道",
            en_GB="导出监察临时讨论频道为 HTML/ZIP，可选择保留、锁定或删除原频道",
        ),
    )
    @app_commands.rename(
        case_id=locale_str("case_id", zh_CN="案件id", zh_TW="案件id", en_US="案件id", en_GB="案件id"),
        action=locale_str("action", zh_CN="处理方式", zh_TW="處理方式", en_US="处理方式", en_GB="处理方式"),
        archive_channel=locale_str("archive_channel", zh_CN="归档频道", zh_TW="歸檔頻道", en_US="归档频道", en_GB="归档频道"),
    )
    @app_commands.describe(
        case_id="监察案件 ID；仅支持已公示裁决或已取消案件",
        action="归档后的频道处理方式，默认仅归档",
        archive_channel="本次归档发送频道；不填则使用 /监察 设置 的归档频道，未配置则回退 admin 通知频道",
    )
    @app_commands.choices(
        action=[
            Choice(name="仅归档", value=ARCHIVE_ACTION_ONLY),
            Choice(name="归档并锁定频道（仅管理可见）", value=ARCHIVE_ACTION_LOCK),
            Choice(name="归档并删除频道", value=ARCHIVE_ACTION_DELETE),
        ]
    )
    async def archive(
        self,
        interaction: discord.Interaction,
        case_id: int,
        action: Choice[str] | None = None,
        archive_channel: discord.TextChannel | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if not _admin_required(interaction):
            await interaction.response.send_message("无权限（需要服务器所有者或 Administrator 权限）。", ephemeral=True)
            return
        _, config_error = await self.cog.settings_service.validate_complete(interaction.guild)
        if config_error:
            await interaction.response.send_message(config_error, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.cog.archive_service.archive_case(
                interaction.guild,
                case_id,
                operator_id=interaction.user.id,
                action=action.value if action else ARCHIVE_ACTION_ONLY,
                archive_channel=archive_channel,
            )
            message = self.cog.archive_service.render_result_message(result)
        except Exception as exc:
            message = f"归档失败：{exc}"
        await interaction.edit_original_response(content=message[:1900])


class InspectionCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = InspectionDatabase(INSPECTION_DB_PATH)
        self.settings_service = InspectionSettingsService(bot, self.db)
        self.candidate_service = CandidateService(bot, self.db, self.settings_service)
        self.case_service = CaseService(bot, self.db, self.settings_service, self.candidate_service)
        self.vote_service = VoteService(bot, self.db, self.settings_service)
        self.archive_service = InspectionArchiveService(bot, self.db, self.settings_service)

        self.group = InspectionGroup(self)
        self._registered_guilds: list[discord.Object] = []
        command_guild_ids = getattr(getattr(bot, "config", None), "command_guild_ids", ())
        if command_guild_ids:
            self._registered_guilds = [discord.Object(id=guild_id) for guild_id in command_guild_ids]
            bot.tree.add_command(self.group, guilds=self._registered_guilds)
        else:
            bot.tree.add_command(self.group)

    async def cog_load(self) -> None:
        await self.db.connect()
        await self.db.init_schema()
        if not self.candidate_maintenance_loop.is_running():
            self.candidate_maintenance_loop.start()
        if not self.case_maintenance_loop.is_running():
            self.case_maintenance_loop.start()
        log.info("Inspection cog loaded")

    async def cog_unload(self) -> None:
        self.candidate_maintenance_loop.cancel()
        self.case_maintenance_loop.cancel()
        try:
            if self._registered_guilds:
                for guild in self._registered_guilds:
                    self.bot.tree.remove_command(self.group.name, guild=guild)
            else:
                self.bot.tree.remove_command(self.group.name)
        except Exception:
            pass
        await self.db.close()
        log.info("Inspection cog unloaded")

    @tasks.loop(hours=CANDIDATE_MAINTENANCE_INTERVAL_HOURS)
    async def candidate_maintenance_loop(self) -> None:
        try:
            await self.candidate_service.process_due_candidate_confirmations()
            await self.candidate_service.process_expired_candidate_confirmations()
        except Exception:
            log.exception("Inspection candidate maintenance tick failed")

    @candidate_maintenance_loop.before_loop
    async def before_candidate_maintenance_loop(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=CASE_MAINTENANCE_INTERVAL_MINUTES)
    async def case_maintenance_loop(self) -> None:
        try:
            await self.case_service.process_response_due_cases()
            await self.case_service.process_ban_due_cases()
            await self.vote_service.process_voting_due_cases()
        except Exception:
            log.exception("Inspection case maintenance tick failed")

    @candidate_maintenance_loop.error
    async def candidate_maintenance_loop_error(self, error: Exception) -> None:
        log.error("Inspection candidate maintenance loop failed", exc_info=(type(error), error, error.__traceback__))

    @case_maintenance_loop.error
    async def case_maintenance_loop_error(self, error: Exception) -> None:
        log.error("Inspection case maintenance loop failed", exc_info=(type(error), error, error.__traceback__))

    @case_maintenance_loop.before_loop
    async def before_case_maintenance_loop(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener("on_interaction")
    async def on_inspection_interaction(self, interaction: discord.Interaction) -> None:
        custom_id = interaction.data.get("custom_id") if isinstance(interaction.data, dict) else None
        if not isinstance(custom_id, str) or not custom_id.startswith("insp_"):
            return

        # 只接管监察模块自定义按钮。
        defer_ephemeral = interaction.guild is not None
        try:
            await interaction.response.defer(ephemeral=defer_ephemeral, thinking=True)
        except Exception:
            return

        try:
            if custom_id.startswith("insp_candidate_keep_"):
                session_id = custom_id.removeprefix("insp_candidate_keep_")
                message = await self.candidate_service.handle_candidate_button(interaction, session_id=session_id, keep=True)
            elif custom_id.startswith("insp_candidate_exit_"):
                session_id = custom_id.removeprefix("insp_candidate_exit_")
                message = await self.candidate_service.handle_candidate_button(interaction, session_id=session_id, keep=False)
            elif custom_id.startswith("insp_case_accept_"):
                case_id = int(custom_id.removeprefix("insp_case_accept_"))
                message = await self.case_service.handle_case_response(interaction, case_id=case_id, willing=True)
            elif custom_id.startswith("insp_case_decline_"):
                case_id = int(custom_id.removeprefix("insp_case_decline_"))
                message = await self.case_service.handle_case_response(interaction, case_id=case_id, willing=False)
            elif custom_id.startswith("insp_vote_yes_"):
                if interaction.guild is None:
                    message = "投票按钮只能在服务器内使用。"
                else:
                    _, config_error = await self.settings_service.validate_complete(interaction.guild)
                    if config_error:
                        message = config_error
                    else:
                        case_id = int(custom_id.removeprefix("insp_vote_yes_"))
                        message = await self.vote_service.handle_vote_button(interaction, case_id=case_id, vote=VOTE_YES)
            elif custom_id.startswith("insp_vote_no_"):
                if interaction.guild is None:
                    message = "投票按钮只能在服务器内使用。"
                else:
                    _, config_error = await self.settings_service.validate_complete(interaction.guild)
                    if config_error:
                        message = config_error
                    else:
                        case_id = int(custom_id.removeprefix("insp_vote_no_"))
                        message = await self.vote_service.handle_vote_button(interaction, case_id=case_id, vote=VOTE_NO)
            else:
                message = "未知监察按钮。"
        except Exception as exc:
            message = f"操作失败或按钮已过期：{exc}"

        try:
            await interaction.edit_original_response(content=message[:1900])
        except Exception:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(InspectionCog(bot))
