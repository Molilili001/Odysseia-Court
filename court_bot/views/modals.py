from __future__ import annotations

import logging
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


log = logging.getLogger(__name__)


async def submit_case_application(
    *,
    bot,
    interaction: discord.Interaction,
    defendant: discord.Member,
    requested_visibility: str,
    rule_text: str,
    description: str,
    evidence_link: str | None = None,
    evidence_attachments: list[discord.Attachment | None] | None = None,
) -> None:
    """创建议诉申请并发送到审核频道。

    / 指令与入口按钮 Modal 都会走这里，保证两种提交方式进入同一套审核流程。
    """

    if interaction.guild is None:
        await interaction.response.send_message("请在服务器内使用该指令。", ephemeral=True)
        return

    settings = await bot.get_settings(interaction.guild.id)
    if not settings or not settings.get("review_channel_id"):
        await interaction.response.send_message(
            "本服务器尚未配置议诉系统的‘审核频道’等信息，请管理先运行：/议诉 设置",
            ephemeral=True,
        )
        return

    requested_visibility = requested_visibility if requested_visibility in (VIS_PRIVATE, VIS_PUBLIC) else VIS_PRIVATE
    rule_text = (rule_text or "").strip()
    description = (description or "").strip()
    evidence_link = (evidence_link or "").strip() or None
    evidence_attachments = [a for a in (evidence_attachments or []) if a is not None]

    # 重要：Modal 提交后必须尽快 ACK，否则容易出现 Unknown interaction（10062）
    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        # 创建议诉
        case_id = await bot.repo.create_case(
            guild_id=interaction.guild.id,
            complainant_id=interaction.user.id,
            defendant_id=defendant.id,
            requested_visibility=requested_visibility,
            rule_text=rule_text,
            description=description,
        )

        # 证据写入
        if evidence_link:
            await bot.repo.add_evidence(
                case_id=case_id,
                provider_id=interaction.user.id,
                ev_type="link",
                label="证据链接",
                url=evidence_link,
            )

        for att in evidence_attachments:
            await bot.repo.add_evidence(
                case_id=case_id,
                provider_id=interaction.user.id,
                ev_type="attachment",
                label=att.filename,
                url=att.url,
                content_type=att.content_type,
                size=att.size,
            )

        await bot.repo.log(
            case_id,
            "case_submitted",
            interaction.user.id,
            {
                "defendant_id": defendant.id,
                "requested_visibility": requested_visibility,
            },
        )

        # 发送到审核频道
        evidences = await bot.repo.list_evidence(case_id)
        case = await bot.repo.get_case(case_id)
        if not case:
            await interaction.edit_original_response(content="议诉创建失败：无法读取议诉记录。")
            return

        review_channel_id = int(settings["review_channel_id"])
        review_channel = bot.get_channel(review_channel_id)
        if review_channel is None:
            review_channel = await bot.fetch_channel(review_channel_id)

        from .review import ReviewView  # 避免循环引用

        view = ReviewView(bot=bot, case_id=case_id)
        # persistent view：注册
        bot.add_view(view)

        msg = await review_channel.send(embed=build_case_review_embed(case, evidences), view=view)
        await bot.repo.set_review_message(case_id, review_channel.id, msg.id)

        await send_audit_log(
            bot=bot,
            audit_channel_id=settings.get("audit_log_channel_id"),
            title="议诉提交",
            description=f"议诉 #{case_id} 已提交，等待审核。",
            case_id=case_id,
            operator=interaction.user,
        )

        supplement_hint = f"如需补充证据，可在任意频道使用 `/议诉 补充证据 议诉编号: {case_id}`。"
        await interaction.edit_original_response(
            content=f"已提交议诉申请，议诉编号：#{case_id}。请等待管理审核。\n{supplement_hint}"
        )

        try:
            await interaction.user.send(
                f"你的议诉申请已提交，议诉编号：#{case_id}。\n\n"
                f"如需补充证据，可在任意频道使用：`/议诉 补充证据 议诉编号: {case_id}`\n"
                "如果之后该议诉已创建频道，也可以在对应议诉频道内使用 `/议诉 补充证据`，无需填写编号。"
            )
        except Exception:
            pass
    except Exception as e:
        try:
            await interaction.edit_original_response(content=f"提交失败：{e}")
        except Exception:
            pass


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
        super().__init__(title="申请议诉", timeout=300)
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
            label="申请说明",
            placeholder="请简述事件经过、时间点、涉及内容……",
            style=discord.TextStyle.paragraph,
            max_length=4000,
            required=True,
        )

        self.add_item(self.rule_text)
        self.add_item(self.description)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await submit_case_application(
            bot=self.bot,
            interaction=interaction,
            defendant=self.defendant,
            requested_visibility=self.requested_visibility,
            rule_text=str(self.rule_text.value).strip(),
            description=str(self.description.value).strip(),
            evidence_link=self.evidence_link,
            evidence_attachments=self.evidence_attachments,
        )


class RejectCaseModal(discord.ui.Modal):
    def __init__(self, *, bot, case_id: int):
        super().__init__(title=f"驳回议诉 #{case_id}", timeout=300)
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
            await interaction.response.send_message("未找到该议诉。", ephemeral=True)
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
            title="议诉驳回",
            description=f"议诉 #{self.case_id} 已驳回。",
            case_id=self.case_id,
            operator=interaction.user,
        )

        # 尝试通知投诉人
        try:
            user = await self.bot.fetch_user(int(case["complainant_id"]))
            await user.send(f"你的议诉 #{self.case_id} 已被驳回。原因：{self.reason.value}")
        except Exception:
            pass

        await interaction.edit_original_response(content="已驳回并记录原因。")


class NeedMoreEvidenceModal(discord.ui.Modal):
    def __init__(self, *, bot, case_id: int):
        super().__init__(title=f"要求补充材料｜议诉 #{case_id}", timeout=300)
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
            await interaction.response.send_message("未找到该议诉。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        await self.bot.repo.set_status(self.case_id, STATUS_NEEDS_MORE_EVIDENCE, str(self.note.value).strip())
        await self.bot.repo.log(self.case_id, "case_need_more_evidence", interaction.user.id, {"note": str(self.note.value)})

        # 刷新审核面板的 Embed（保留按钮，方便后续继续通过/驳回）
        try:
            updated_case = await self.bot.repo.get_case(self.case_id)
            if updated_case:
                await self.bot.refresh_review_message(updated_case, keep_review_actions=True)
        except Exception:
            pass

        await send_audit_log(
            bot=self.bot,
            audit_channel_id=settings.get("audit_log_channel_id") if settings else None,
            title="要求补充材料",
            description=f"议诉 #{self.case_id} 已要求补充材料。",
            case_id=self.case_id,
            operator=interaction.user,
        )

        try:
            user = await self.bot.fetch_user(int(case["complainant_id"]))
            await user.send(
                "你的议诉 #{case_id} 需要补充材料：\n{note}\n\n"
                "你可以在任意频道使用 `/议诉 补充证据 议诉编号: {case_id}` 补充证据。\n"
                "如果该议诉已创建频道，也可以在对应议诉频道内使用 `/议诉 补充证据`，无需填写编号。"
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
        title = f"议诉 #{case_id}｜{round_part}{side_label(side)}陈述"
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
            await interaction.response.send_message("未找到该议诉。", ephemeral=True)
            return

        if case.get("status") != STATUS_IN_SESSION:
            await interaction.response.send_message("当前议诉不在进行中。", ephemeral=True)
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
            await interaction.edit_original_response(content="无法找到议诉频道/帖子。")
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


async def _notify_evidence_added(
    *,
    bot,
    case: dict,
    case_id: int,
    interaction: discord.Interaction,
    note: str | None,
) -> list[str]:
    """补充证据后的可见通知。

    - 审核阶段：刷新审核频道原卡片；若无法刷新，则补发一条通知到审核频道。
    - 已创建议诉频道：同时在议诉频道提示有新证据。
    """

    warnings: list[str] = []
    updated_case = await bot.repo.get_case(case_id) or case

    settings = None
    if interaction.guild is not None:
        try:
            settings = await bot.get_settings(interaction.guild.id)
        except Exception:
            settings = None

    evidences = await bot.repo.list_evidence(case_id)
    status = str(updated_case.get("status") or "")
    review_notified = False

    if status in (STATUS_UNDER_REVIEW, STATUS_NEEDS_MORE_EVIDENCE):
        if updated_case.get("review_channel_id") and updated_case.get("review_message_id"):
            try:
                review_notified = bool(await bot.refresh_review_message(updated_case, keep_review_actions=True))
            except Exception:
                log.exception("Failed to refresh review message after evidence added (case %s)", case_id)

        if not review_notified and settings and settings.get("review_channel_id"):
            try:
                review_channel = await bot.get_channel_or_thread(int(settings["review_channel_id"]))
                if isinstance(review_channel, discord.TextChannel):
                    await review_channel.send(
                        content=f"【系统】议诉 #{case_id} 收到新的补充证据，请管理查看。",
                        embed=build_case_review_embed(updated_case, evidences),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    review_notified = True
            except Exception:
                log.exception("Failed to send review evidence notification (case %s)", case_id)

        if not review_notified:
            warnings.append("未能更新审核频道提示，请确认 Bot 在审核频道有查看、发言和嵌入链接权限。")

    space = await bot.get_case_space(updated_case)
    if space is not None:
        try:
            await space.send(
                embed=discord.Embed(
                    title=f"议诉 #{case_id}｜新增证据",
                    description=(note or "（无说明）"),
                    color=0x5865F2,
                ).set_footer(text=f"提交者：{interaction.user.id}")
            )
        except Exception:
            warnings.append("证据已记录，但未能发送到议诉频道。")

    return warnings


class AddEvidenceModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        bot,
        case_id: int,
        pending_link: str | None,
        pending_attachments: list[discord.Attachment],
    ):
        super().__init__(title=f"补充证据｜议诉 #{case_id}", timeout=300)
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
            await interaction.response.send_message("未找到该议诉。", ephemeral=True)
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

        warnings = await _notify_evidence_added(
            bot=self.bot,
            case=case,
            case_id=self.case_id,
            interaction=interaction,
            note=note,
        )

        msg = "已补充证据，管理会在审核频道看到更新。"
        if warnings:
            msg += "\n" + "\n".join(f"- {w}" for w in warnings)
        await interaction.edit_original_response(content=msg)




class WithdrawCaseModal(discord.ui.Modal):
    def __init__(self, *, bot, case_id: int):
        super().__init__(title=f"撤诉确认｜议诉 #{case_id}", timeout=300)
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
            await interaction.response.send_message("未找到该议诉。", ephemeral=True)
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
            self.bot.forget_case_runtime_state(self.case_id)
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
                                reason=f"议诉 #{self.case_id} 撤诉后收回发言权限",
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
                    content=f"【系统】议诉 #{self.case_id} 已撤诉。管理可点击下方按钮归档并删除该议诉频道。",
                    view=ArchiveView(bot=self.bot, case_id=self.case_id),
                )
            except Exception:
                await space.send(f"【系统】议诉 #{self.case_id} 已撤诉。")

        await interaction.edit_original_response(content="已撤诉。")
