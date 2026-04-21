from __future__ import annotations

import discord
from discord import app_commands
from discord.app_commands import Choice, locale_str
from discord.ext import commands

from ..constants import VIS_PRIVATE, VIS_PUBLIC
from ..views.modals import AddEvidenceModal, ApplyCourtModal


class CourtGroup(app_commands.Group):
    def __init__(self, bot: commands.Bot):
        super().__init__(
            # 注意：Slash Command 的“基础名字”建议保持英文小写以保证兼容性；
            # 通过 localizations 让客户端显示中文（也能覆盖英文客户端）。
            name=locale_str(
                "court",
                zh_CN="类脑大法庭",
                zh_TW="類腦大法庭",
                en_US="类脑大法庭",
                en_GB="类脑大法庭",
            ),
            description=locale_str(
                "Court case system",
                zh_CN="类脑大法庭案件系统",
                zh_TW="類腦大法庭案件系統",
                en_US="类脑大法庭案件系统",
                en_GB="类脑大法庭案件系统",
            ),
        )
        self.bot = bot

    @app_commands.command(
        name=locale_str(
            "apply",
            zh_CN="申请开庭",
            zh_TW="申請開庭",
            en_US="申请开庭",
            en_GB="申请开庭",
        ),
        description=locale_str(
            "Submit a complaint and apply for a trial",
            zh_CN="提交投诉并申请开庭",
            zh_TW="提交投訴並申請開庭",
            en_US="提交投诉并申请开庭",
            en_GB="提交投诉并申请开庭",
        ),
    )
    @app_commands.rename(
        defendant=locale_str("defendant", zh_CN="被投诉人", zh_TW="被投訴人", en_US="被投诉人", en_GB="被投诉人"),
        visibility=locale_str("visibility", zh_CN="开庭模式", zh_TW="開庭模式", en_US="开庭模式", en_GB="开庭模式"),
        evidence_link=locale_str("evidence_link", zh_CN="证据链接", zh_TW="證據連結", en_US="证据链接", en_GB="证据链接"),
        evidence1=locale_str("evidence1", zh_CN="证据附件1", zh_TW="證據附件1", en_US="证据附件1", en_GB="证据附件1"),
        evidence2=locale_str("evidence2", zh_CN="证据附件2", zh_TW="證據附件2", en_US="证据附件2", en_GB="证据附件2"),
        evidence3=locale_str("evidence3", zh_CN="证据附件3", zh_TW="證據附件3", en_US="证据附件3", en_GB="证据附件3"),
    )
    @app_commands.describe(
        defendant="被投诉人",
        visibility="开庭模式（公开/私密）",
        evidence_link="证据链接（可选）",
        evidence1="证据附件 1（可选）",
        evidence2="证据附件 2（可选）",
        evidence3="证据附件 3（可选）",
    )
    @app_commands.choices(
        visibility=[
            Choice(name="私密", value=VIS_PRIVATE),
            Choice(name="公开", value=VIS_PUBLIC),
        ]
    )
    async def apply(
        self,
        interaction: discord.Interaction,
        defendant: discord.Member,
        visibility: Choice[str],
        evidence_link: str | None = None,
        evidence1: discord.Attachment | None = None,
        evidence2: discord.Attachment | None = None,
        evidence3: discord.Attachment | None = None,
    ) -> None:
        """用户提交投诉并申请开庭。"""

        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return

        settings = await self.bot.get_settings(interaction.guild.id)
        if not settings or not settings.get("review_channel_id"):
            await interaction.response.send_message(
                "本服务器尚未配置类脑大法庭，请先由管理执行：/类脑大法庭 设置",
                ephemeral=True,
            )
            return

        # 立即弹出 Modal 收集长文本（规则/说明）
        modal = ApplyCourtModal(
            bot=self.bot,
            defendant=defendant,
            requested_visibility=visibility.value,
            evidence_link=evidence_link,
            evidence_attachments=[evidence1, evidence2, evidence3],
        )
        await interaction.response.send_modal(modal)

    @app_commands.command(
        name=locale_str(
            "evidence",
            zh_CN="补充证据",
            zh_TW="補充證據",
            en_US="补充证据",
            en_GB="补充证据",
        ),
        description=locale_str(
            "Add evidence to a case",
            zh_CN="为案件补充证据（支持附件）",
            zh_TW="為案件補充證據（支援附件）",
            en_US="为案件补充证据（支持附件）",
            en_GB="为案件补充证据（支持附件）",
        ),
    )
    @app_commands.rename(
        case_id=locale_str("case_id", zh_CN="案件编号", zh_TW="案件編號", en_US="案件编号", en_GB="案件编号"),
        evidence_link=locale_str("evidence_link", zh_CN="证据链接", zh_TW="證據連結", en_US="证据链接", en_GB="证据链接"),
        evidence1=locale_str("evidence1", zh_CN="证据附件1", zh_TW="證據附件1", en_US="证据附件1", en_GB="证据附件1"),
        evidence2=locale_str("evidence2", zh_CN="证据附件2", zh_TW="證據附件2", en_US="证据附件2", en_GB="证据附件2"),
        evidence3=locale_str("evidence3", zh_CN="证据附件3", zh_TW="證據附件3", en_US="证据附件3", en_GB="证据附件3"),
    )
    @app_commands.describe(
        case_id="案件编号（可选：若在案件频道/帖子内使用可不填）",
        evidence_link="证据链接（可选）",
        evidence1="证据附件 1（可选）",
        evidence2="证据附件 2（可选）",
        evidence3="证据附件 3（可选）",
    )
    async def evidence(
        self,
        interaction: discord.Interaction,
        case_id: int | None = None,
        evidence_link: str | None = None,
        evidence1: discord.Attachment | None = None,
        evidence2: discord.Attachment | None = None,
        evidence3: discord.Attachment | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return

        # 允许在案件频道/帖子内不填 case_id：通过 channel/thread 反查
        target_case = None
        if case_id is not None:
            target_case = await self.bot.repo.get_case(case_id)
        else:
            target_case = await self.bot.repo.find_case_by_space_id(interaction.guild.id, interaction.channel_id)

        if not target_case:
            await interaction.response.send_message("无法定位案件。请在案件频道/帖子内使用，或填写 case_id。", ephemeral=True)
            return

        # 打开 modal 让用户填写“证据说明”
        modal = AddEvidenceModal(
            bot=self.bot,
            case_id=int(target_case["id"]),
            pending_link=evidence_link,
            pending_attachments=[evidence1, evidence2, evidence3],
        )
        await interaction.response.send_modal(modal)


    @app_commands.command(
        name=locale_str(
            "setup",
            zh_CN="设置",
            zh_TW="設定",
            en_US="设置",
            en_GB="设置",
        ),
        description=locale_str(
            "Configure court bot for this guild",
            zh_CN="配置本服务器类脑大法庭（频道/身份组等）",
            zh_TW="設定本伺服器類腦大法庭（頻道/身分組等）",
            en_US="配置本服务器类脑大法庭（频道/身份组等）",
            en_GB="配置本服务器类脑大法庭（频道/身份组等）",
        ),
    )
    @app_commands.rename(
        admin_role1=locale_str("admin_role1", zh_CN="管理身份组1", zh_TW="管理身分組1", en_US="管理身份组1", en_GB="管理身份组1"),
        admin_role2=locale_str("admin_role2", zh_CN="管理身份组2", zh_TW="管理身分組2", en_US="管理身份组2", en_GB="管理身份组2"),
        admin_role3=locale_str("admin_role3", zh_CN="管理身份组3", zh_TW="管理身分組3", en_US="管理身份组3", en_GB="管理身份组3"),
        review_channel=locale_str("review_channel", zh_CN="审核频道", zh_TW="審核頻道", en_US="审核频道", en_GB="审核频道"),
        court_category=locale_str("court_category", zh_CN="庭审分类", zh_TW="庭審分類", en_US="庭审分类", en_GB="庭审分类"),
        judge_panel_channel=locale_str("judge_panel_channel", zh_CN="裁决频道", zh_TW="裁決頻道", en_US="裁决频道", en_GB="裁决频道"),
        audience_role=locale_str("audience_role", zh_CN="观众身份组", zh_TW="觀眾身分組", en_US="观众身份组", en_GB="观众身份组"),
        archive_channel=locale_str("archive_channel", zh_CN="归档频道", zh_TW="歸檔頻道", en_US="归档频道", en_GB="归档频道"),
        audit_log_channel=locale_str("audit_log_channel", zh_CN="审计频道", zh_TW="審計頻道", en_US="审计频道", en_GB="审计频道"),
    )
    @app_commands.describe(
        admin_role1="管理身份组 1（必填）",
        admin_role2="管理身份组 2（可选）",
        admin_role3="管理身份组 3（可选）",
        review_channel="管理审核频道",
        court_category="庭审案件分类（Category）",
        judge_panel_channel="裁决面板频道（管理私密）",
        audience_role="公开案件观众身份组（只读，可选）",
        archive_channel="归档频道（仅管理可见）",
        audit_log_channel="审计日志频道（可选）",
    )
    async def setup(
        self,
        interaction: discord.Interaction,
        admin_role1: discord.Role,
        review_channel: discord.TextChannel,
        court_category: discord.CategoryChannel,
        judge_panel_channel: discord.TextChannel,
        archive_channel: discord.TextChannel,
        audience_role: discord.Role | None = None,
        admin_role2: discord.Role | None = None,
        admin_role3: discord.Role | None = None,
        audit_log_channel: discord.TextChannel | None = None,
    ) -> None:
        """由管理配置服务器级设置，写入 SQLite。

        说明：为了避免在 .env 里硬编码频道，本命令用于在服务器内完成初始化。
        """

        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("无法读取成员权限信息。", ephemeral=True)
            return

        # 初始化阶段不能依赖“已配置的管理身份组”，因此这里用 Discord 原生权限兜底
        if not (
            interaction.user.guild_permissions.administrator
            or interaction.user.guild_permissions.manage_guild
        ):
            await interaction.response.send_message("无权限（需要 管理服务器 或 管理员 权限）。", ephemeral=True)
            return

        admin_role_ids = [admin_role1.id]
        if admin_role2:
            admin_role_ids.append(admin_role2.id)
        if admin_role3:
            admin_role_ids.append(admin_role3.id)

        await self.bot.settings_repo.upsert_settings(
            guild_id=interaction.guild.id,
            admin_role_ids=admin_role_ids,
            review_channel_id=review_channel.id,
            court_category_id=court_category.id,
            judge_panel_channel_id=judge_panel_channel.id,
            audit_log_channel_id=audit_log_channel.id if audit_log_channel else None,
            audience_role_id=audience_role.id if audience_role else None,
            archive_channel_id=archive_channel.id,
        )

        # 刷新缓存
        await self.bot.get_settings(interaction.guild.id, refresh=True)

        audit_text = audit_log_channel.mention if audit_log_channel else "（未设置）"
        audience_text = audience_role.mention if audience_role else "（未设置）"
        admin_roles_text = admin_role1.mention
        if admin_role2:
            admin_roles_text += f"、{admin_role2.mention}"
        if admin_role3:
            admin_roles_text += f"、{admin_role3.mention}"

        await interaction.response.send_message(
            "已保存类脑大法庭设置：\n"
            f"- 管理身份组：{admin_roles_text}\n"
            f"- 审核频道：{review_channel.mention}\n"
            f"- 庭审分类：{court_category.name}\n"
            f"- 裁决频道：{judge_panel_channel.mention}\n"
            f"- 归档频道：{archive_channel.mention}\n"
            f"- 观众身份组：{audience_text}\n"
            f"- 审计频道：{audit_text}",
            ephemeral=True,
        )

    @app_commands.command(
        name=locale_str(
            "show_settings",
            zh_CN="查看设置",
            zh_TW="查看設定",
            en_US="查看设置",
            en_GB="查看设置",
        ),
        description=locale_str(
            "Show current settings",
            zh_CN="查看本服务器类脑大法庭设置",
            zh_TW="查看本伺服器類腦大法庭設定",
            en_US="查看本服务器类脑大法庭设置",
            en_GB="查看本服务器类脑大法庭设置",
        ),
    )
    async def show_settings(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return

        settings = await self.bot.get_settings(interaction.guild.id)
        if not settings:
            await interaction.response.send_message("本服务器尚未配置类脑大法庭。请先执行：/类脑大法庭 设置", ephemeral=True)
            return

        admin_roles = "、".join(f"<@&{rid}>" for rid in (settings.get("admin_role_ids") or [])) or "（未设置）"
        audit_ch = settings.get("audit_log_channel_id")
        audit = f"<#{audit_ch}>" if audit_ch else "（未设置）"
        archive_ch = settings.get("archive_channel_id")
        archive = f"<#{archive_ch}>" if archive_ch else "（未设置）"
        audience_role_id = settings.get("audience_role_id")
        audience = f"<@&{audience_role_id}>" if audience_role_id else "（未设置）"

        await interaction.response.send_message(
            "当前类脑大法庭设置：\n"
            f"- 管理身份组：{admin_roles}\n"
            f"- 审核频道：<#{settings.get('review_channel_id')}>\n"
            f"- 庭审分类（ID）：`{settings.get('court_category_id')}`\n"
            f"- 裁决频道：<#{settings.get('judge_panel_channel_id')}>\n"
            f"- 归档频道：{archive}\n"
            f"- 观众身份组：{audience}\n"
            f"- 审计频道：{audit}",
            ephemeral=True,
        )


class CourtCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

        # 若设置了 COMMAND_GUILD_ID，则只注册为该 Guild 的命令（更新更快）。
        # 同时在启动阶段会把“全局命令列表同步为空”，用于清理旧的全局英文指令，避免出现两组指令。
        self.group = CourtGroup(bot)
        if getattr(bot, "config", None) and bot.config.command_guild_id:
            guild = discord.Object(id=bot.config.command_guild_id)
            bot.tree.add_command(self.group, guild=guild)
        else:
            bot.tree.add_command(self.group)


async def setup(bot):
    await bot.add_cog(CourtCog(bot))
