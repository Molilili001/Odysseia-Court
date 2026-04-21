from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import discord

from ..constants import (
    SIDE_COMPLAINANT,
    SIDE_DEFENDANT,
    STATUS_AWAITING_CONTINUE,
    STATUS_AWAITING_JUDGEMENT,
    STATUS_IN_SESSION,
    STATUS_NEEDS_MORE_EVIDENCE,
    STATUS_REJECTED,
    STATUS_UNDER_REVIEW,
    STATUS_WITHDRAWN,
    VIS_PRIVATE,
    VIS_PUBLIC,
    round_label,
    side_label,
)
from ..embeds import build_case_review_embed, build_statement_embed
from ..services.audit import send_audit_log


class ApplyCourtModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        bot,
        defendant: discord.Member,
        requested_visibility: str,
        evidence_link: str | None,
        evidence_attachments: list[discord.Attachment],
    ):
        super().__init__(title="类脑大法庭｜申请开庭", timeout=300)
        self.bot = bot
        self.defendant = defendant
        self.requested_visibility = requested_visibility
        self.evidence_link = evidence_link
        self.evidence_attachments = [a for a in evidence_attachments if a is not None]

        self.rule_text = discord.ui.TextInput(
            label="违反规则（Rule）",
            placeholder="例如：Rule 3：禁止人身攻击……",
            style=discord.TextStyle.short,
            max_length=300,
            required=True,
        )
        self.description = discord.ui.TextInput(
            label="案件说明",
            placeholder="请简述事件经过、时间点、涉及内容……",
            style=discord.TextStyle.paragraph,
            max_length=4000,
            required=True,
        )

        self.add_item(self.rule_text)
        self.add_item(self.description)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用该指令。", ephemeral=True)
            return

        settings = await self.bot.get_settings(interaction.guild.id)
        if not settings or not settings.get("review_channel_id"):
            await interaction.response.send_message(
                "本服务器尚未配置类脑大法庭的‘审核频道’等信息，请管理先运行：/类脑大法庭 设置",
                ephemeral=True,
            )
            return

        # 重要：Modal 提交后必须尽快 ACK，否则容易出现 Unknown interaction（10062）
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            # 创建案件
            case_id = await self.bot.repo.create_case(
                guild_id=interaction.guild.id,
                complainant_id=interaction.user.id,
                defendant_id=self.defendant.id,
                requested_visibility=self.requested_visibility,
                rule_text=str(self.rule_text.value).strip(),
                description=str(self.description.value).strip(),
            )

            # 证据写入
            if self.evidence_link:
                await self.bot.repo.add_evidence(
                    case_id=case_id,
                    provider_id=interaction.user.id,
                    ev_type="link",
                    label="证据链接",
                    url=self.evidence_link.strip(),
                )

            for att in self.evidence_attachments:
                await self.bot.repo.add_evidence(
                    case_id=case_id,
                    provider_id=interaction.user.id,
                    ev_type="attachment",
                    label=att.filename,
                    url=att.url,
                    content_type=att.content_type,
                    size=att.size,
                )

            await self.bot.repo.log(
                case_id,
                "case_submitted",
                interaction.user.id,
                {
                    "defendant_id": self.defendant.id,
                    "requested_visibility": self.requested_visibility,
                },
            )

            # 发送到审核频道
            evidences = await self.bot.repo.list_evidence(case_id)
            case = await self.bot.repo.get_case(case_id)
            if not case:
                await interaction.edit_original_response(content="案件创建失败：无法读取案件记录。")
                return

            review_channel_id = int(settings["review_channel_id"])
            review_channel = self.bot.get_channel(review_channel_id)
            if review_channel is None:
                review_channel = await self.bot.fetch_channel(review_channel_id)

            from .review import ReviewView  # 避免循环引用

            view = ReviewView(bot=self.bot, case_id=case_id)
            # persistent view：注册
            self.bot.add_view(view)

            msg = await review_channel.send(embed=build_case_review_embed(case, evidences), view=view)
            await self.bot.repo.set_review_message(case_id, review_channel.id, msg.id)

            await send_audit_log(
                bot=self.bot,
                audit_channel_id=settings.get("audit_log_channel_id"),
                title="案件提交",
                description=f"案件 #{case_id} 已提交，等待审核。",
                case_id=case_id,
                operator=interaction.user,
            )

            await interaction.edit_original_response(content=f"已提交申请，案件编号：#{case_id}。请等待管理审核。")
        except Exception as e:
            try:
                await interaction.edit_original_response(content=f"提交失败：{e}")
            except Exception:
                pass
            return


class RejectCaseModal(discord.ui.Modal):
    def __init__(self, *, bot, case_id: int):
        super().__init__(title=f"驳回案件 #{case_id}", timeout=300)
        self.bot = bot
        self.case_id = case_id

        self.reason = discord.ui.TextInput(
            label="驳回原因",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=True,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内操作。", ephemeral=True)
            return

        if not await self.bot.is_admin(interaction.user, interaction.guild):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return

        settings = await self.bot.get_settings(interaction.guild.id)

        case = await self.bot.repo.get_case(self.case_id)
        if not case:
            await interaction.response.send_message("案件不存在。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        await self.bot.repo.set_status(self.case_id, STATUS_REJECTED, str(self.reason.value).strip())
        await self.bot.repo.log(self.case_id, "case_rejected", interaction.user.id, {"reason": str(self.reason.value)})

        # 更新审核面板：移除按钮 + 展示最新状态
        try:
            updated_case = await self.bot.repo.get_case(self.case_id)
            if updated_case and updated_case.get("review_channel_id") and updated_case.get("review_message_id"):
                evidences = await self.bot.repo.list_evidence(self.case_id)
                review_channel = self.bot.get_channel(int(updated_case["review_channel_id"]))
                if review_channel is None:
                    review_channel = await self.bot.fetch_channel(int(updated_case["review_channel_id"]))
                msg = await review_channel.fetch_message(int(updated_case["review_message_id"]))
                await msg.edit(embed=build_case_review_embed(updated_case, evidences), view=None)
        except Exception:
            pass

        await send_audit_log(
            bot=self.bot,
            audit_channel_id=settings.get("audit_log_channel_id") if settings else None,
            title="案件驳回",
            description=f"案件 #{self.case_id} 已驳回。",
            case_id=self.case_id,
            operator=interaction.user,
        )

        # 尝试通知投诉人
        try:
            user = await self.bot.fetch_user(int(case["complainant_id"]))
            await user.send(f"你的类脑大法庭案件 #{self.case_id} 已被驳回。原因：{self.reason.value}")
        except Exception:
            pass

        await interaction.edit_original_response(content="已驳回并记录原因。")


class NeedMoreEvidenceModal(discord.ui.Modal):
    def __init__(self, *, bot, case_id: int):
        super().__init__(title=f"要求补充材料｜案件 #{case_id}", timeout=300)
        self.bot = bot
        self.case_id = case_id

        self.note = discord.ui.TextInput(
            label="需要补充的内容",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=True,
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内操作。", ephemeral=True)
            return

        if not await self.bot.is_admin(interaction.user, interaction.guild):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return

        settings = await self.bot.get_settings(interaction.guild.id)

        case = await self.bot.repo.get_case(self.case_id)
        if not case:
            await interaction.response.send_message("案件不存在。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        await self.bot.repo.set_status(self.case_id, STATUS_NEEDS_MORE_EVIDENCE, str(self.note.value).strip())
        await self.bot.repo.log(self.case_id, "case_need_more_evidence", interaction.user.id, {"note": str(self.note.value)})

        # 刷新审核面板的 Embed（保留按钮，方便后续继续通过/驳回）
        try:
            updated_case = await self.bot.repo.get_case(self.case_id)
            if updated_case and updated_case.get("review_channel_id") and updated_case.get("review_message_id"):
                evidences = await self.bot.repo.list_evidence(self.case_id)
                review_channel = self.bot.get_channel(int(updated_case["review_channel_id"]))
                if review_channel is None:
                    review_channel = await self.bot.fetch_channel(int(updated_case["review_channel_id"]))
                msg = await review_channel.fetch_message(int(updated_case["review_message_id"]))
                await msg.edit(embed=build_case_review_embed(updated_case, evidences))
        except Exception:
            pass

        await send_audit_log(
            bot=self.bot,
            audit_channel_id=settings.get("audit_log_channel_id") if settings else None,
            title="要求补充材料",
            description=f"案件 #{self.case_id} 已要求补充材料。",
            case_id=self.case_id,
            operator=interaction.user,
        )

        try:
            user = await self.bot.fetch_user(int(case["complainant_id"]))
            await user.send(
                "你的类脑大法庭案件 #{case_id} 需要补充材料：\n{note}\n\n"
                "你可以在案件频道/帖子内使用 `/类脑大法庭 补充证据` 继续提交。"
                .format(case_id=self.case_id, note=str(self.note.value))
            )
        except Exception:
            pass

        await interaction.edit_original_response(content="已标记为待补充，并通知投诉人（若可 DM）。")


class StatementModal(discord.ui.Modal):
    def __init__(self, *, bot, case_id: int, round_number: int, side: str):
        if round_number <= 3:
            round_part = f"{round_number}/3 轮（{round_label(round_number)}）"
        else:
            round_part = f"第 {round_number} 轮（{round_label(round_number)}）"
        title = f"案件 #{case_id}｜{round_part}{side_label(side)}陈述"
        super().__init__(title=title, timeout=600)
        self.bot = bot
        self.case_id = case_id
        self.round_number = round_number
        self.side = side

        self.content = discord.ui.TextInput(
            label="本轮陈述内容",
            style=discord.TextStyle.paragraph,
            max_length=4000,
            required=True,
        )
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        case = await self.bot.repo.get_case(self.case_id)
        if not case:
            await interaction.response.send_message("案件不存在。", ephemeral=True)
            return

        if case.get("status") != STATUS_IN_SESSION:
            await interaction.response.send_message("当前案件不在庭审中。", ephemeral=True)
            return

        # 二次校验：是否轮到此人
        expected_user_id = case["complainant_id"] if self.side == SIDE_COMPLAINANT else case["defendant_id"]
        if interaction.user.id != int(expected_user_id):
            await interaction.response.send_message("当前不是你发言的回合。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # 发布陈述 embed
        space = await self.bot.get_case_space(case)
        if space is None:
            await interaction.edit_original_response(content="无法找到案件频道/帖子。")
            return

        embed = build_statement_embed(
            case_id=self.case_id,
            side=self.side,
            round_number=self.round_number,
            author=interaction.user,
            content=str(self.content.value),
        )
        msg = await space.send(embed=embed)

        await self.bot.repo.add_statement(
            case_id=self.case_id,
            round_number=self.round_number,
            side=self.side,
            content=str(self.content.value),
            submitted_by=interaction.user.id,
            message_id=msg.id,
        )

        await self.bot.repo.log(
            self.case_id,
            "statement_submitted",
            interaction.user.id,
            {"round": self.round_number, "side": self.side, "message_id": msg.id},
        )

        # 推进回合
        updated_case = await self.bot.repo.advance_turn(self.case_id)

        # 更新控制面板
        await self.bot.refresh_court_panel(updated_case)

        if updated_case.get("status") == STATUS_AWAITING_CONTINUE:
            await self.bot.enter_continue_panel(updated_case)
        elif updated_case.get("status") == STATUS_AWAITING_JUDGEMENT:
            await self.bot.enter_judgement(updated_case)

        await interaction.edit_original_response(content="已提交本轮陈述。")


class AddEvidenceModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        bot,
        case_id: int,
        pending_link: str | None,
        pending_attachments: list[discord.Attachment],
    ):
        super().__init__(title=f"补充证据｜案件 #{case_id}", timeout=300)
        self.bot = bot
        self.case_id = case_id
        self.pending_link = pending_link
        self.pending_attachments = [a for a in pending_attachments if a is not None]

        self.note = discord.ui.TextInput(
            label="证据说明（可选）",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=False,
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        case = await self.bot.repo.get_case(self.case_id)
        if not case:
            await interaction.response.send_message("案件不存在。", ephemeral=True)
            return

        # 权限：当事人或管理
        is_party = interaction.user.id in (int(case["complainant_id"]), int(case["defendant_id"]))
        is_admin = interaction.guild is not None and await self.bot.is_admin(interaction.user, interaction.guild)
        if not (is_party or is_admin):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        note = str(self.note.value).strip() if self.note.value else None

        if self.pending_link:
            await self.bot.repo.add_evidence(
                case_id=self.case_id,
                provider_id=interaction.user.id,
                ev_type="link",
                label="补充证据链接",
                url=self.pending_link.strip(),
                note=note,
            )

        for att in self.pending_attachments:
            await self.bot.repo.add_evidence(
                case_id=self.case_id,
                provider_id=interaction.user.id,
                ev_type="attachment",
                label=att.filename,
                url=att.url,
                content_type=att.content_type,
                size=att.size,
                note=note,
            )

        await self.bot.repo.log(self.case_id, "evidence_added", interaction.user.id, {"note": note})

        space = await self.bot.get_case_space(case)
        if space is not None:
            await space.send(
                embed=discord.Embed(
                    title=f"案件 #{self.case_id}｜新增证据",
                    description=(note or "（无说明）"),
                    color=0x5865F2,
                ).set_footer(text=f"提交者：{interaction.user.id}")
            )

        await interaction.edit_original_response(content="已补充证据。")




class WithdrawCaseModal(discord.ui.Modal):
    def __init__(self, *, bot, case_id: int):
        super().__init__(title=f"撤诉确认｜案件 #{case_id}", timeout=300)
        self.bot = bot
        self.case_id = case_id

        self.confirm = discord.ui.TextInput(
            label="请输入：我确认撤诉",
            style=discord.TextStyle.short,
            max_length=20,
            required=True,
        )
        self.add_item(self.confirm)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        case = await self.bot.repo.get_case(self.case_id)
        if not case:
            await interaction.response.send_message("案件不存在。", ephemeral=True)
            return

        is_admin = interaction.guild is not None and await self.bot.is_admin(interaction.user, interaction.guild)
        if interaction.user.id != int(case["complainant_id"]) and not is_admin:
            await interaction.response.send_message("只有投诉人或管理可以撤诉。", ephemeral=True)
            return

        if str(self.confirm.value).strip() != "我确认撤诉":
            await interaction.response.send_message("口令不正确，撤诉已取消。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        await self.bot.repo.set_status(self.case_id, STATUS_WITHDRAWN, "投诉人撤诉")
        await self.bot.repo.log(self.case_id, "case_withdrawn", interaction.user.id)

        # 清理可能残留的发言权状态
        try:
            await self.bot.repo.clear_turn_state(self.case_id)
        except Exception:
            pass

        space = await self.bot.get_case_space(case)
        if space is not None:
            try:
                # 撤回双方发言权限（防止撤诉时仍残留 send_messages=True）
                if isinstance(space, discord.TextChannel) and interaction.guild is not None:
                    for uid in (int(case["complainant_id"]), int(case["defendant_id"])):
                        member = interaction.guild.get_member(uid)
                        if member is None:
                            try:
                                member = await interaction.guild.fetch_member(uid)
                            except Exception:
                                member = None
                        if member is not None:
                            await space.set_permissions(
                                member,
                                overwrite=discord.PermissionOverwrite(
                                    view_channel=True,
                                    send_messages=False,
                                    attach_files=False,
                                    read_message_history=True,
                                    use_application_commands=True,
                                ),
                                reason=f"案件 #{self.case_id} 撤诉后收回发言权限",
                            )
            except Exception:
                pass

            try:
                updated_case = await self.bot.repo.get_case(self.case_id)
                if updated_case:
                    await self.bot.refresh_court_panel(updated_case)
                    await self.bot.refresh_review_message(updated_case)
            except Exception:
                pass

            try:
                from .archive import ArchiveView

                await space.send(
                    content=f"【系统】案件 #{self.case_id} 已撤诉。管理可点击下方按钮归档并删除该案件频道。",
                    view=ArchiveView(bot=self.bot, case_id=self.case_id),
                )
            except Exception:
                await space.send(f"【系统】案件 #{self.case_id} 已撤诉。")

        await interaction.edit_original_response(content="已撤诉。")
