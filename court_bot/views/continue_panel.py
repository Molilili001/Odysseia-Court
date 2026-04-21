from __future__ import annotations

import discord

from ..constants import (
    SIDE_COMPLAINANT,
    SIDE_DEFENDANT,
    STATUS_AWAITING_CONTINUE,
    STATUS_AWAITING_JUDGEMENT,
    STATUS_IN_SESSION,
)
from ..embeds import build_continue_panel_embed


class ContinueView(discord.ui.View):
    """三辩结束后的“是否继续辩诉”面板。

    - 投诉人、被投诉人均可点击
    - 双方都点“继续”才继续
    - 任意一方点“结束”则直接结束并进入裁决
    """

    def __init__(self, *, bot, case_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.case_id = case_id

        self.btn_continue = discord.ui.Button(
            label="希望继续辩诉",
            style=discord.ButtonStyle.primary,
            custom_id=f"court_continue_yes_{case_id}",
            row=0,
        )
        self.btn_end = discord.ui.Button(
            label="希望结束辩诉",
            style=discord.ButtonStyle.danger,
            custom_id=f"court_continue_no_{case_id}",
            row=0,
        )

        self.btn_force_end = discord.ui.Button(
            label="管理强制结束",
            style=discord.ButtonStyle.secondary,
            custom_id=f"court_continue_force_end_{case_id}",
            row=1,
        )

        self.btn_continue.callback = self._on_continue
        self.btn_end.callback = self._on_end
        self.btn_force_end.callback = self._on_force_end

        self.add_item(self.btn_continue)
        self.add_item(self.btn_end)
        self.add_item(self.btn_force_end)

    async def _on_continue(self, interaction: discord.Interaction) -> None:
        await self._handle(interaction, choice="continue")

    async def _on_end(self, interaction: discord.Interaction) -> None:
        await self._handle(interaction, choice="end")

    async def _on_force_end(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not await self.bot.is_admin(interaction.user, interaction.guild):
            await interaction.response.send_message("无权限（仅管理可强制结束）。", ephemeral=True)
            return

        case = await self.bot.repo.get_case(self.case_id)
        if not case:
            await interaction.response.send_message("案件不存在。", ephemeral=True)
            return

        if case.get("status") != STATUS_AWAITING_CONTINUE:
            await interaction.response.send_message("当前案件不在‘待决定是否继续辩诉’状态。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        state = await self.bot.repo.get_continue_state(self.case_id)
        await self.bot.repo.clear_continue_state(self.case_id)
        await self.bot.repo.set_status(self.case_id, STATUS_AWAITING_JUDGEMENT, "管理强制结束辩诉")
        updated_case = await self.bot.repo.get_case(self.case_id)

        # 更新面板消息：移除按钮
        try:
            if updated_case:
                await interaction.message.edit(embed=build_continue_panel_embed(updated_case, state or {}), view=None)
            else:
                await interaction.message.edit(view=None)
        except Exception:
            pass

        if updated_case:
            await self.bot.refresh_court_panel(updated_case)
            await self.bot.refresh_review_message(updated_case)
            try:
                space = await self.bot.get_case_space(updated_case)
                if space is not None:
                    await space.send("【系统】管理强制结束辩诉，进入裁决。")
            except Exception:
                pass

            await self.bot.enter_judgement(updated_case)

        await interaction.edit_original_response(content="已强制结束辩诉，已进入裁决。")

    async def _handle(self, interaction: discord.Interaction, *, choice: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内操作。", ephemeral=True)
            return

        case = await self.bot.repo.get_case(self.case_id)
        if not case:
            await interaction.response.send_message("案件不存在。", ephemeral=True)
            return

        if case.get("status") != STATUS_AWAITING_CONTINUE:
            await interaction.response.send_message("当前案件不在‘待决定是否继续辩诉’状态。", ephemeral=True)
            return

        complainant_id = int(case["complainant_id"])
        defendant_id = int(case["defendant_id"])

        if interaction.user.id == complainant_id:
            side = SIDE_COMPLAINANT
        elif interaction.user.id == defendant_id:
            side = SIDE_DEFENDANT
        else:
            await interaction.response.send_message("只有双方当事人可以操作该面板。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # 写入/更新 continue_state
        state = await self.bot.repo.set_continue_choice(case_id=self.case_id, side=side, choice=choice)
        if int(state.get("panel_message_id") or 0) == 0:
            # 兜底：补写面板 message_id
            await self.bot.repo.upsert_continue_state(
                case_id=self.case_id,
                panel_message_id=interaction.message.id,
                complainant_choice=state.get("complainant_choice"),
                defendant_choice=state.get("defendant_choice"),
            )
            state = await self.bot.repo.get_continue_state(self.case_id) or state

        await self.bot.repo.log(self.case_id, "continue_choice", interaction.user.id, {"choice": choice, "side": side})

        # 任意一方选择结束：直接进入裁决
        if choice == "end" or state.get("complainant_choice") == "end" or state.get("defendant_choice") == "end":
            await self.bot.repo.clear_continue_state(self.case_id)
            await self.bot.repo.set_status(self.case_id, STATUS_AWAITING_JUDGEMENT, "辩诉结束")
            updated_case = await self.bot.repo.get_case(self.case_id)

            # 更新面板消息：移除按钮
            try:
                if updated_case:
                    await interaction.message.edit(embed=build_continue_panel_embed(updated_case, state), view=None)
                else:
                    await interaction.message.edit(view=None)
            except Exception:
                pass

            if updated_case:
                await self.bot.refresh_court_panel(updated_case)
                await self.bot.refresh_review_message(updated_case)
                # 在案件空间提示
                try:
                    space = await self.bot.get_case_space(updated_case)
                    if space is not None:
                        await space.send("【系统】辩诉已结束，进入裁决。")
                except Exception:
                    pass

                await self.bot.enter_judgement(updated_case)

            await interaction.edit_original_response(content="已选择结束辩诉，已进入裁决。")
            return

        # 双方都选择继续：恢复庭审
        if state.get("complainant_choice") == "continue" and state.get("defendant_choice") == "continue":
            await self.bot.repo.clear_continue_state(self.case_id)
            await self.bot.repo.set_status(self.case_id, STATUS_IN_SESSION, None)
            updated_case = await self.bot.repo.get_case(self.case_id)

            try:
                if updated_case:
                    await interaction.message.edit(embed=build_continue_panel_embed(updated_case, state), view=None)
                else:
                    await interaction.message.edit(view=None)
            except Exception:
                pass

            if updated_case:
                await self.bot.refresh_court_panel(updated_case)
                await self.bot.refresh_review_message(updated_case)
                try:
                    space = await self.bot.get_case_space(updated_case)
                    if space is not None:
                        from .court import CourtView

                        await space.send(
                            content=f"【系统】双方同意继续辩诉，进入第 {updated_case.get('current_round')} 轮。",
                            view=CourtView(bot=self.bot, case_id=self.case_id),
                        )
                except Exception:
                    pass

            await interaction.edit_original_response(content="双方已同意继续辩诉。")
            return

        # 否则：更新面板展示当前选择
        try:
            await interaction.message.edit(embed=build_continue_panel_embed(case, state), view=self)
        except Exception:
            pass

        await interaction.edit_original_response(content="已记录你的选择，等待对方选择。")
