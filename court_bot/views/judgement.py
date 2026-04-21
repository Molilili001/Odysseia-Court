from __future__ import annotations

import discord

from ..embeds import build_judgement_result_embed
from ..services.audit import send_audit_log
from ..constants import STATUS_AWAITING_JUDGEMENT, STATUS_CLOSED


class JudgementReasonModal(discord.ui.Modal):
    def __init__(self, *, parent_view: "JudgementView", decision: str):
        super().__init__(title=f"裁决｜{decision}｜填写说明", timeout=600)
        self.parent_view = parent_view
        self.decision = decision

        self.reason = discord.ui.TextInput(
            label="说明（必填）",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=True,
        )
        self.penalty = discord.ui.TextInput(
            label="处罚/处置（可选）",
            style=discord.TextStyle.short,
            max_length=200,
            required=False,
            placeholder="例如：口头警告 / 删除内容 / 禁言 3 天（手动执行）",
        )

        self.add_item(self.reason)
        self.add_item(self.penalty)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # 重要：避免 Unknown interaction（10062）
        await interaction.response.defer(ephemeral=True, thinking=True)

        await self.parent_view._publish(
            interaction,
            decision=self.decision,
            penalty=str(self.penalty.value or "无").strip() or "无",
            reason=str(self.reason.value or "").strip(),
        )


class JudgementView(discord.ui.View):
    """裁决面板（方案 A：单击发布）。仅管理可见的频道内使用。"""

    def __init__(self, *, bot, case_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.case_id = case_id

        # 方案A：单击 -> 弹出 Modal 填写说明 -> 发布
        self.btn_ok = discord.ui.Button(
            label="成立并说明",
            style=discord.ButtonStyle.danger,
            custom_id=f"court_judge_ok_reason_{case_id}",
            row=0,
        )
        self.btn_no = discord.ui.Button(
            label="不成立并说明",
            style=discord.ButtonStyle.success,
            custom_id=f"court_judge_no_reason_{case_id}",
            row=0,
        )

        self.btn_ok.callback = self._on_ok
        self.btn_no.callback = self._on_no

        self.add_item(self.btn_ok)
        self.add_item(self.btn_no)

    def disable_all(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def _publish(self, interaction: discord.Interaction, decision: str, penalty: str, reason: str) -> None:
        if interaction.guild is None or not await self.bot.is_admin(interaction.user, interaction.guild):
            await interaction.edit_original_response(content="无权限。")
            return

        settings = await self.bot.get_settings(interaction.guild.id)

        case = await self.bot.repo.get_case(self.case_id)
        if not case:
            await interaction.edit_original_response(content="案件不存在。")
            return

        if case.get("status") != STATUS_AWAITING_JUDGEMENT:
            await interaction.edit_original_response(content="该案件当前不在待裁决状态。")
            return

        await interaction.edit_original_response(content="正在发布裁决...")

        space = await self.bot.get_case_space(case)
        if space is None:
            return

        result_embed = build_judgement_result_embed(case, decision=decision, penalty=penalty, reason=reason)
        msg = await space.send(embed=result_embed)

        await self.bot.repo.create_judgement(
            case_id=self.case_id,
            decision=decision,
            penalty=penalty,
            operator_id=interaction.user.id,
            published_message_id=msg.id,
        )

        await self.bot.repo.log(
            self.case_id,
            "judgement_reason",
            interaction.user.id,
            {"decision": decision, "penalty": penalty, "reason": reason},
        )

        await self.bot.repo.set_status(self.case_id, STATUS_CLOSED, f"{decision}｜{penalty}")
        await self.bot.repo.log(self.case_id, "judgement_published", interaction.user.id, {"decision": decision, "penalty": penalty})

        # 刷新案件帖/频道内的“庭审控制面板”，让结果也体现在案件空间里
        updated_case = await self.bot.repo.get_case(self.case_id)
        if updated_case:
            await self.bot.refresh_court_panel(updated_case)
            await self.bot.refresh_review_message(updated_case)

        # 如果是 Forum 帖子线程，结案后自动锁定/归档（不影响阅读，但避免后续干扰）
        if isinstance(space, discord.Thread):
            try:
                await space.edit(archived=True, locked=True, reason=f"案件 #{self.case_id} 已结案")
            except Exception:
                pass

        await send_audit_log(
            bot=self.bot,
            audit_channel_id=settings.get("audit_log_channel_id") if settings else None,
            title="裁决发布",
            description=f"案件 #{self.case_id} 已发布裁决：{decision}｜{penalty}\n说明：{reason}",
            case_id=self.case_id,
            operator=interaction.user,
        )

        # 禁用按钮（不显示是谁点的）
        self.disable_all()
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

        try:
            await interaction.edit_original_response(content="裁决已发布。")
        except Exception:
            pass

    async def _on_ok(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(JudgementReasonModal(parent_view=self, decision="成立"))

    async def _on_no(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(JudgementReasonModal(parent_view=self, decision="不成立"))
