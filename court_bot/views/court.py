from __future__ import annotations

from datetime import datetime

import discord

from ..constants import (
    SIDE_COMPLAINANT,
    SIDE_DEFENDANT,
    STATUS_AWAITING_CONTINUE,
    STATUS_AWAITING_JUDGEMENT,
    STATUS_IN_SESSION,
    TURN_MESSAGE_LIMIT,
    TURN_SPEAK_MINUTES,
)
from ..services.audit import send_audit_log
from .modals import WithdrawCaseModal


class CourtView(discord.ui.View):
    """庭审控制面板（案件频道内）。

    自主发言模式：
    - 双方默认禁言
    - 轮到谁，谁点击“获取本轮发言权”后才可在 10 分钟内最多发 10 条消息（可含图片/文件）
    - 可手动结束或超时/超条数自动结束
    """

    def __init__(self, *, bot, case_id: int, timeout: float | None = None):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.case_id = case_id

        self.btn_claim = discord.ui.Button(
            label="获取本轮发言权",
            style=discord.ButtonStyle.primary,
            custom_id=f"court_turn_claim_{case_id}",
            row=0,
        )
        self.btn_end = discord.ui.Button(
            label="结束本轮发言",
            style=discord.ButtonStyle.secondary,
            custom_id=f"court_turn_end_{case_id}",
            row=0,
        )

        self.btn_withdraw = discord.ui.Button(
            label="撤诉（投诉人）",
            style=discord.ButtonStyle.danger,
            custom_id=f"court_panel_withdraw_{case_id}",
            row=1,
        )
        self.btn_force = discord.ui.Button(
            label="管理强制结束/推进",
            style=discord.ButtonStyle.secondary,
            custom_id=f"court_turn_force_{case_id}",
            row=1,
        )

        self.btn_claim.callback = self._on_claim
        self.btn_end.callback = self._on_end

        self.btn_withdraw.callback = self._on_withdraw
        self.btn_force.callback = self._on_force

        self.add_item(self.btn_claim)
        self.add_item(self.btn_end)
        self.add_item(self.btn_withdraw)
        self.add_item(self.btn_force)

    async def _on_claim(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("请在服务器内操作。", ephemeral=True)
            return

        case = await self.bot.repo.get_case(self.case_id)
        if not case:
            await interaction.response.send_message("案件不存在。", ephemeral=True)
            return

        if case.get("status") != STATUS_IN_SESSION:
            await interaction.response.send_message("当前案件不在庭审中。", ephemeral=True)
            return

        current_side = case.get("current_side") or SIDE_COMPLAINANT
        expected_id = int(case["complainant_id"]) if current_side == SIDE_COMPLAINANT else int(case["defendant_id"])
        if interaction.user.id != expected_id:
            await interaction.response.send_message("当前不是你发言的回合。", ephemeral=True)
            return

        st = await self.bot.repo.get_turn_state(self.case_id)
        if st:
            speaker_id = int(st.get("speaker_id") or 0)
            expires_at = st.get("expires_at")
            ts_text = ""
            if expires_at:
                try:
                    ts = int(datetime.fromisoformat(expires_at).timestamp())
                    ts_text = f"（截止 <t:{ts}:R>）"
                except Exception:
                    ts_text = ""

            if speaker_id == interaction.user.id:
                await interaction.response.send_message(f"你已拥有本轮发言权{ts_text}。", ephemeral=True)
            else:
                await interaction.response.send_message(f"当前由 <@{speaker_id}> 发言{ts_text}，请稍候。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            st = await self.bot.begin_speaking_turn(case_id=self.case_id, speaker=interaction.user)
        except Exception as e:
            await interaction.edit_original_response(content=f"无法开始本轮发言：{e}")
            return

        expires_at = st.get("expires_at") if st else None
        ts_text = ""
        if expires_at:
            try:
                ts = int(datetime.fromisoformat(expires_at).timestamp())
                ts_text = f"截止 <t:{ts}:R>"
            except Exception:
                ts_text = ""

        await interaction.edit_original_response(
            content=(
                f"已授予你本轮发言权：{TURN_SPEAK_MINUTES} 分钟内最多 {TURN_MESSAGE_LIMIT} 条消息。{ts_text}\n"
                "请直接在本频道发送文字/图片/文件；发完点击『结束本轮发言』。"
            )
        )

    async def _on_end(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内操作。", ephemeral=True)
            return

        case = await self.bot.repo.get_case(self.case_id)
        if not case:
            await interaction.response.send_message("案件不存在。", ephemeral=True)
            return

        if case.get("status") != STATUS_IN_SESSION:
            await interaction.response.send_message("当前案件不在庭审中。", ephemeral=True)
            return

        st = await self.bot.repo.get_turn_state(self.case_id)
        if not st:
            await interaction.response.send_message("当前没有正在进行的发言回合。", ephemeral=True)
            return

        speaker_id = int(st.get("speaker_id") or 0)
        is_admin = interaction.guild is not None and await self.bot.is_admin(interaction.user, interaction.guild)
        if not (is_admin or interaction.user.id == speaker_id):
            await interaction.response.send_message("无权限（仅当前发言者或管理可结束）。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            updated = await self.bot.end_speaking_turn(
                case_id=self.case_id,
                operator=interaction.user,
                reason="手动结束本轮发言",
            )
        except Exception as e:
            await interaction.edit_original_response(content=f"结束失败：{e}")
            return

        await interaction.edit_original_response(content="已结束本轮发言并推进到下一回合。")

        # 审计（可选）
        settings = await self.bot.get_settings(interaction.guild.id)
        await send_audit_log(
            bot=self.bot,
            audit_channel_id=settings.get("audit_log_channel_id") if settings else None,
            title="结束本轮发言",
            description=f"案件 #{self.case_id} 已结束本轮发言。",
            case_id=self.case_id,
            operator=interaction.user,
        )

    async def _on_withdraw(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(WithdrawCaseModal(bot=self.bot, case_id=self.case_id))

    async def _on_force(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not await self.bot.is_admin(interaction.user, interaction.guild):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        st = await self.bot.repo.get_turn_state(self.case_id)
        try:
            if st:
                await self.bot.end_speaking_turn(
                    case_id=self.case_id,
                    operator=interaction.user,
                    reason="管理强制结束本轮发言",
                )
                await interaction.edit_original_response(content="已强制结束当前发言并推进回合。")
            else:
                updated_case = await self.bot.repo.advance_turn(self.case_id)
                await self.bot.repo.log(self.case_id, "admin_force_advance", interaction.user.id)
                await self.bot.refresh_court_panel(updated_case)

                # 系统提示（带按钮，方便下一位直接获取发言权）
                try:
                    space = await self.bot.get_case_space(updated_case)
                    if space is not None:
                        if updated_case.get("status") == STATUS_AWAITING_CONTINUE:
                            await space.send("【系统】管理已强制推进回合，进入‘是否继续辩诉’投票阶段。")
                        elif updated_case.get("status") == STATUS_AWAITING_JUDGEMENT:
                            await space.send("【系统】管理已强制推进并结束辩诉，进入裁决。")
                        else:
                            next_side = updated_case.get("current_side") or SIDE_COMPLAINANT
                            next_expected_id = (
                                int(updated_case["complainant_id"])
                                if next_side == SIDE_COMPLAINANT
                                else int(updated_case["defendant_id"])
                            )
                            await space.send(
                                content=f"【系统】管理已强制推进到下一回合。下一位发言者：<@{next_expected_id}>。",
                                view=CourtView(bot=self.bot, case_id=self.case_id),
                            )
                except Exception:
                    pass

                if updated_case.get("status") == STATUS_AWAITING_CONTINUE:
                    await self.bot.enter_continue_panel(updated_case)
                if updated_case.get("status") == STATUS_AWAITING_JUDGEMENT:
                    await self.bot.enter_judgement(updated_case)
                await interaction.edit_original_response(content="已强制推进到下一回合。")
        except Exception as e:
            await interaction.edit_original_response(content=f"操作失败：{e}")
