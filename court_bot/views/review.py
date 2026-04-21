from __future__ import annotations

import discord

from ..constants import (
    STATUS_NEEDS_MORE_EVIDENCE,
    STATUS_REJECTED,
    STATUS_UNDER_REVIEW,
    VIS_PRIVATE,
    VIS_PUBLIC,
)
from ..embeds import build_case_review_embed
from ..services.audit import send_audit_log
from .modals import NeedMoreEvidenceModal, RejectCaseModal


class ReviewView(discord.ui.View):
    """管理审核频道：通过/驳回/补证。"""

    def __init__(self, *, bot, case_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.case_id = case_id

        # 动态按钮（带 case_id 的 custom_id），用于 persistent view
        self.btn_approve_req = discord.ui.Button(
            label="通过并开庭（按申请）",
            style=discord.ButtonStyle.success,
            custom_id=f"court_review_approve_req_{case_id}",
            row=0,
        )
        self.btn_approve_private = discord.ui.Button(
            label="通过并开庭（私密）",
            style=discord.ButtonStyle.success,
            custom_id=f"court_review_approve_private_{case_id}",
            row=0,
        )
        self.btn_approve_public = discord.ui.Button(
            label="通过并开庭（公开）",
            style=discord.ButtonStyle.success,
            custom_id=f"court_review_approve_public_{case_id}",
            row=0,
        )
        self.btn_reject = discord.ui.Button(
            label="驳回",
            style=discord.ButtonStyle.danger,
            custom_id=f"court_review_reject_{case_id}",
            row=1,
        )
        self.btn_need_more = discord.ui.Button(
            label="要求补充材料",
            style=discord.ButtonStyle.secondary,
            custom_id=f"court_review_need_more_{case_id}",
            row=1,
        )

        self.btn_approve_req.callback = self._on_approve_requested
        self.btn_approve_private.callback = self._on_approve_private
        self.btn_approve_public.callback = self._on_approve_public
        self.btn_reject.callback = self._on_reject
        self.btn_need_more.callback = self._on_need_more

        self.add_item(self.btn_approve_req)
        self.add_item(self.btn_approve_private)
        self.add_item(self.btn_approve_public)
        self.add_item(self.btn_reject)
        self.add_item(self.btn_need_more)

    def disable_all(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def _refresh_review_message(self, interaction: discord.Interaction) -> None:
        case = await self.bot.repo.get_case(self.case_id)
        if not case:
            return
        evidences = await self.bot.repo.list_evidence(self.case_id)
        try:
            await interaction.message.edit(embed=build_case_review_embed(case, evidences), view=self)
        except Exception:
            pass

    async def _on_approve_requested(self, interaction: discord.Interaction) -> None:
        case = await self.bot.repo.get_case(self.case_id)
        if not case:
            await interaction.response.send_message("案件不存在。", ephemeral=True)
            return
        await self._approve(interaction, case.get("requested_visibility"))

    async def _on_approve_private(self, interaction: discord.Interaction) -> None:
        await self._approve(interaction, VIS_PRIVATE)

    async def _on_approve_public(self, interaction: discord.Interaction) -> None:
        await self._approve(interaction, VIS_PUBLIC)

    async def _approve(self, interaction: discord.Interaction, approved_visibility: str | None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内操作。", ephemeral=True)
            return

        if not await self.bot.is_admin(interaction.user, interaction.guild):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return

        settings = await self.bot.get_settings(interaction.guild.id)
        if not settings:
            await interaction.response.send_message("本服务器尚未配置类脑大法庭，请先运行：/类脑大法庭 设置", ephemeral=True)
            return

        case = await self.bot.repo.get_case(self.case_id)
        if not case:
            await interaction.response.send_message("案件不存在。", ephemeral=True)
            return

        if case.get("status") not in (STATUS_UNDER_REVIEW, STATUS_NEEDS_MORE_EVIDENCE):
            await interaction.response.send_message("该案件当前状态不允许开庭。", ephemeral=True)
            return

        approved_visibility = approved_visibility or case.get("requested_visibility")
        if approved_visibility not in (VIS_PRIVATE, VIS_PUBLIC):
            approved_visibility = VIS_PRIVATE

        await interaction.response.send_message("正在创建庭审空间，请稍候...", ephemeral=True)

        await self.bot.repo.approve_case(self.case_id, approved_visibility)
        await self.bot.repo.log(self.case_id, "case_approved", interaction.user.id, {"approved_visibility": approved_visibility})

        # 创建案件空间并发控制面板
        try:
            created_space = await self.bot.create_court_space(case_id=self.case_id, approved_visibility=approved_visibility)
        except Exception as e:
            # 出错时尽量回滚为待审核，避免卡死
            await self.bot.repo.set_status(self.case_id, STATUS_UNDER_REVIEW, f"开庭失败：{e}")
            await interaction.followup.send(f"开庭失败：{e}", ephemeral=True)
            return

        # 更新审核面板：移除按钮 + 写出最新状态 + 提供案件空间链接
        updated_case = await self.bot.repo.get_case(self.case_id)
        evidences = await self.bot.repo.list_evidence(self.case_id)
        if updated_case:
            try:
                await interaction.message.edit(embed=build_case_review_embed(updated_case, evidences), view=None)
            except Exception:
                pass

        # 给操作者一个直达链接（ephemeral）
        try:
            if created_space is not None and hasattr(created_space, "mention"):
                await interaction.edit_original_response(content=f"已开庭：{created_space.mention}")
            else:
                await interaction.edit_original_response(content="已开庭。")
        except Exception:
            pass

        await send_audit_log(
            bot=self.bot,
            audit_channel_id=settings.get("audit_log_channel_id"),
            title="案件开庭",
            description=f"案件 #{self.case_id} 已开庭（{approved_visibility}）。",
            case_id=self.case_id,
            operator=interaction.user,
        )

        # 说明：审核按钮已移除（view=None），无需 disable_all

    async def _on_reject(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not await self.bot.is_admin(interaction.user, interaction.guild):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return
        await interaction.response.send_modal(RejectCaseModal(bot=self.bot, case_id=self.case_id))

    async def _on_need_more(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not await self.bot.is_admin(interaction.user, interaction.guild):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return
        await interaction.response.send_modal(NeedMoreEvidenceModal(bot=self.bot, case_id=self.case_id))
