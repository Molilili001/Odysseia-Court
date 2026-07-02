from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import discord

from .constants import MAX_SELF_INTRO_LENGTH
from .continuous_constants import (
    CONT_APP_APPROVED,
    CONT_APP_APPROVED_WITHDRAWN,
    CONT_APP_CANCELLED,
    CONT_APP_REJECTED,
    CONT_APP_RETURNED,
    CONT_APP_VOTING,
    CONT_APP_WITHDRAWN,
    CONT_MODE_APPROVAL,
    CONT_MODE_SUPPORT,
    CONT_VOTE_LABELS,
    CONT_VOTE_SUPPORT,
)
from .continuous_database import ContinuousApplicationRepo
from .continuous_embeds import (
    build_continuous_application_embed,
    build_continuous_application_list_embed,
    build_continuous_application_lookup_embed,
    build_continuous_approved_list_embed,
    build_continuous_entry_embed,
    build_continuous_my_status_embed,
    build_continuous_public_event_embed,
    build_continuous_status_embed,
    build_continuous_supporter_list_embeds,
    build_continuous_vote_status_embed,
)
from .continuous_views import (
    CONTINUOUS_APPLICATION_LIST_PAGE_SIZE,
    ContinuousApplicationJumpView,
    ContinuousApplicationListView,
    ContinuousEntryView,
    ContinuousExitConfirmView,
    ContinuousFieldSelectView,
    ContinuousVoteView,
)
from .embeds import format_role_mentions
from .permissions import has_any_role
from .text_utils import contains_forbidden_mention, sanitize_public_text
from .time_utils import format_time_pair, parse_iso, to_utc_iso, utc_now, utc_now_iso

log = logging.getLogger(__name__)


class ContinuousApplicationService:
    def __init__(self, bot, repo: ContinuousApplicationRepo, audit_repo=None):
        self.bot = bot
        self.repo = repo
        self.audit_repo = audit_repo

    async def ensure_schema(self) -> None:
        await self.repo.ensure_schema()

    def entry_view(self) -> ContinuousEntryView:
        return ContinuousEntryView(service=self)

    def vote_view(self, mode: str = CONT_MODE_APPROVAL) -> ContinuousVoteView:
        return ContinuousVoteView(service=self, mode=mode)

    @staticmethod
    def _application_jump_url(config: dict[str, Any], application: dict[str, Any]) -> str | None:
        guild_id = int(application.get("guild_id") or config.get("guild_id") or 0)
        channel_id = int(application.get("vote_channel_id") or config.get("voting_channel_id") or 0)
        message_id = int(application.get("vote_message_id") or 0)
        if not guild_id or not channel_id or not message_id:
            return None
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

    def _config_mode(self, config: dict[str, Any] | None) -> str:
        return str((config or {}).get("mode") or CONT_MODE_APPROVAL)

    async def _log(self, guild_id: int, operator_id: int | None, action: str, detail: dict[str, Any] | None = None) -> None:
        if self.audit_repo is None:
            return
        await self.audit_repo.log(None, int(guild_id), int(operator_id) if operator_id else None, action, detail or {})

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

    async def _config_from_entry_interaction(self, interaction: discord.Interaction) -> dict[str, Any]:
        if interaction.guild is None or interaction.message is None:
            raise ValueError("无法定位常态申请入口。")
        config = await self.repo.find_config_by_entry_message(interaction.guild.id, interaction.message.id)
        if not config:
            raise ValueError("无法根据当前消息定位常态申请配置。")
        return config

    async def _application_from_vote_interaction(self, interaction: discord.Interaction) -> dict[str, Any]:
        if interaction.guild is None or interaction.message is None:
            raise ValueError("无法定位常态申请投票。")
        application = await self.repo.find_application_by_vote_message(interaction.guild.id, interaction.message.id)
        if not application:
            raise ValueError("无法根据当前消息定位申请。")
        return application

    async def send_entry(self, config: dict[str, Any], *, channel: discord.TextChannel | None = None) -> discord.Message:
        if channel is None:
            channel = await self._get_text_channel(int(config.get("entry_channel_id") or 0))
        if channel is None:
            raise ValueError("无法读取入口频道。")
        fields = await self.repo.list_fields(int(config["id"]))
        msg = await channel.send(
            embed=build_continuous_entry_embed(config, fields),
            view=self.entry_view(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.repo.set_entry_message(int(config["id"]), int(msg.id), int(channel.id))
        return msg

    async def refresh_entry(self, config: dict[str, Any], *, reason: str | None = None, operator_id: int | None = None) -> bool:
        channel = await self._get_text_channel(int(config.get("entry_channel_id") or 0))
        message_id = int(config.get("entry_message_id") or 0)
        if channel is None or not message_id:
            return False
        try:
            message = await channel.fetch_message(message_id)
            fresh = await self.repo.get_config(int(config["id"])) or config
            fields = await self.repo.list_fields(int(fresh["id"]))
            await message.edit(
                embed=build_continuous_entry_embed(fresh, fields),
                view=self.entry_view(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self._log(int(fresh["guild_id"]), operator_id, "continuous_entry_refreshed", {"config_id": int(fresh["id"]), "reason": reason})
            return True
        except Exception:
            log.exception("Failed to refresh continuous entry for config %s", config.get("id"))
            return False

    async def _application_block_reason(self, member: discord.Member, config: dict[str, Any]) -> str | None:
        active = await self.repo.get_active_application(int(config["id"]), int(member.id))
        if active:
            return f"你已有进行中的申请（Application ID: {active['id']}），不能重复申请。"
        approved = await self.repo.get_approved_application(int(config["id"]), int(member.id))
        if approved:
            return "你已经在该常态申请中通过，不能重复申请；如需退出通过名单，请点击入口里的『退出申请』。"
        cooldown_until = await self.repo.get_active_cooldown(int(config["id"]), int(member.id), utc_now_iso())
        if cooldown_until:
            return f"你仍在冷却期内，冷却结束：{format_time_pair(cooldown_until)}。"
        role_ids = self.repo.decode_role_ids(config.get("allowed_application_role_ids"))
        if not has_any_role(member, role_ids):
            return f"你没有申请资格；{format_role_mentions(role_ids, action='申请')}。"
        return None

    async def open_application_flow(self, interaction: discord.Interaction) -> None:
        try:
            config = await self._config_from_entry_interaction(interaction)
        except Exception as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        block = await self._application_block_reason(interaction.user, config)
        if block:
            await interaction.response.send_message(block, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        fields = await self.repo.list_fields(int(config["id"]))
        if not fields:
            await interaction.response.send_message("该常态申请尚未配置岗位，请联系管理员。", ephemeral=True)
            return
        if len(fields) == 1:
            from .continuous_views import ContinuousApplicationModal

            await interaction.response.send_modal(
                ContinuousApplicationModal(service=self, config_id=int(config["id"]), field_key=str(fields[0]["field_key"]))
            )
            return
        await interaction.response.send_message(
            "请选择本次申请的岗位：",
            view=ContinuousFieldSelectView(service=self, config=config, fields=fields),
            ephemeral=True,
        )

    async def handle_application_submit(self, interaction: discord.Interaction, *, config_id: int, field_key: str, self_intro: str) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        config = await self.repo.get_config(int(config_id))
        if not config or int(config["guild_id"]) != int(interaction.guild.id):
            await interaction.response.send_message("未找到常态申请配置。", ephemeral=True)
            return
        block = await self._application_block_reason(interaction.user, config)
        if block:
            await interaction.response.send_message(block, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        fields = await self.repo.list_fields(int(config["id"]))
        field = next((row for row in fields if str(row.get("field_key")) == str(field_key)), None)
        if not field:
            await interaction.response.send_message("该岗位不存在或已变更，请重新打开入口。", ephemeral=True)
            return
        if contains_forbidden_mention(self_intro):
            await interaction.response.send_message("报名宣言不能包含用户提及、身份组提及、@everyone 或 @here。", ephemeral=True)
            return
        if len(self_intro or "") > MAX_SELF_INTRO_LENGTH:
            await interaction.response.send_message(f"报名宣言最多 {MAX_SELF_INTRO_LENGTH} 字。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        voting_end_at = to_utc_iso(utc_now() + timedelta(minutes=int(config.get("voting_duration_minutes") or 0)))
        try:
            application_id = await self.repo.create_application(
                config=config,
                user_id=int(interaction.user.id),
                display_name=sanitize_public_text(interaction.user.display_name, max_len=100, fallback=str(interaction.user.id)),
                username=sanitize_public_text(getattr(interaction.user, "name", ""), max_len=100, fallback=""),
                field_key=str(field["field_key"]),
                field_name=sanitize_public_text(field.get("name"), max_len=80, fallback=str(field["field_key"])),
                self_intro=str(self_intro or "").strip(),
                voting_end_at=voting_end_at,
            )
        except ValueError as exc:
            await interaction.edit_original_response(content=str(exc))
            return
        application = await self.repo.get_application(application_id)
        if not application:
            await interaction.edit_original_response(content="申请创建失败，请稍后重试。")
            return
        channel = await self._get_text_channel(int(config.get("voting_channel_id") or 0))
        if channel is None:
            await self.repo.set_application_status(application_id, CONT_APP_CANCELLED, reason="无法读取投票频道", expected_status=CONT_APP_VOTING)
            await interaction.edit_original_response(content="申请已记录，但无法读取投票频道；本次申请已取消，请联系管理员。")
            return
        try:
            msg = await channel.send(
                embed=build_continuous_application_embed(config, application),
                view=self.vote_view(self._config_mode(config)),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:
            await self.repo.set_application_status(application_id, CONT_APP_CANCELLED, reason=f"发送投票消息失败：{str(exc)[:200]}", expected_status=CONT_APP_VOTING)
            await interaction.edit_original_response(content=f"发送投票消息失败：{exc}")
            return
        await self.repo.set_application_vote_message(application_id, int(channel.id), int(msg.id))
        await self._log(interaction.guild.id, interaction.user.id, "continuous_application_submitted", {"config_id": int(config["id"]), "application_id": application_id})
        await interaction.edit_original_response(content=f"申请已提交，投票已发布到 {channel.mention}。")

    async def show_my_status_from_entry(self, interaction: discord.Interaction) -> None:
        try:
            config = await self._config_from_entry_interaction(interaction)
        except Exception as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        latest = await self.repo.get_latest_user_application(int(config["id"]), int(interaction.user.id))
        cooldown_until = await self.repo.get_active_cooldown(int(config["id"]), int(interaction.user.id), utc_now_iso())
        await interaction.response.send_message(
            embed=build_continuous_my_status_embed(config, latest, cooldown_until=cooldown_until),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _current_application_list_payload(
        self,
        config: dict[str, Any],
        *,
        page: int,
        requester_id: int,
    ) -> tuple[discord.Embed, discord.ui.View | None]:
        applications = await self.repo.list_open_applications(int(config["id"]))
        embed = build_continuous_application_list_embed(
            config,
            applications,
            page=page,
            page_size=CONTINUOUS_APPLICATION_LIST_PAGE_SIZE,
        )
        view = (
            ContinuousApplicationListView(
                service=self,
                config=config,
                applications=applications,
                page=page,
                requester_id=int(requester_id),
                page_size=CONTINUOUS_APPLICATION_LIST_PAGE_SIZE,
            )
            if applications
            else None
        )
        return embed, view

    async def finalize_due_applications_for_config(self, config_id: int) -> int:
        due = await self.repo.list_due_applications_for_config(int(config_id), utc_now_iso())
        completed = 0
        for application in due:
            try:
                await self.finalize_application(application, operator_id=None)
                completed += 1
            except Exception:
                log.exception("Failed to finalize continuous application %s", application.get("id"))
        return completed

    async def show_current_applications_from_entry(self, interaction: discord.Interaction) -> None:
        try:
            config = await self._config_from_entry_interaction(interaction)
        except Exception as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.finalize_due_applications_for_config(int(config["id"]))
        fresh = await self.repo.get_config(int(config["id"])) or config
        embed, view = await self._current_application_list_payload(
            fresh,
            page=0,
            requester_id=int(interaction.user.id),
        )
        await interaction.edit_original_response(
            content=None,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def show_current_applications_page(
        self,
        interaction: discord.Interaction,
        *,
        config_id: int,
        page: int,
        requester_id: int,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        config = await self.repo.get_config(int(config_id))
        if not config or int(config.get("guild_id") or 0) != int(interaction.guild.id):
            await interaction.response.send_message("未找到常态申请配置。", ephemeral=True)
            return
        await interaction.response.defer(thinking=False)
        await self.finalize_due_applications_for_config(int(config["id"]))
        embed, view = await self._current_application_list_payload(
            config,
            page=page,
            requester_id=int(requester_id),
        )
        await interaction.edit_original_response(
            content=None,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def show_application_detail_from_list(self, interaction: discord.Interaction, *, config_id: int, application_id: int) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        config = await self.repo.get_config(int(config_id))
        application = await self.repo.get_application(int(application_id))
        if not config or int(config.get("guild_id") or 0) != int(interaction.guild.id):
            await interaction.followup.send("未找到常态申请配置。", ephemeral=True)
            return
        if not application or int(application.get("config_id") or 0) != int(config["id"]) or int(application.get("guild_id") or 0) != int(interaction.guild.id):
            await interaction.followup.send("未找到该报名记录。", ephemeral=True)
            return

        vote_end = parse_iso(application.get("voting_end_at"))
        if application.get("status") == CONT_APP_VOTING and vote_end is not None and utc_now() >= vote_end:
            await self.finalize_application(application, operator_id=None)
            application = await self.repo.get_application(int(application_id)) or application

        jump_url = self._application_jump_url(config, application)
        embed = build_continuous_application_lookup_embed(config, application, jump_url=jump_url)
        view = ContinuousApplicationJumpView(jump_url=jump_url) if jump_url else None
        await interaction.followup.send(
            embed=embed,
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def request_exit_from_entry(self, interaction: discord.Interaction) -> None:
        try:
            config = await self._config_from_entry_interaction(interaction)
        except Exception as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        active = await self.repo.get_active_application(int(config["id"]), int(interaction.user.id))
        if active:
            vote_end = parse_iso(active.get("voting_end_at"))
            if vote_end is not None and utc_now() >= vote_end:
                await interaction.response.defer(ephemeral=True, thinking=True)
                await self.finalize_application(active, operator_id=None)
                await interaction.edit_original_response(content="该申请投票已到期，已尝试结算，不能再按主动退出处理。")
                return
            content = "确认退出当前申请并进入冷却期？退出后本轮投票会立即终止。"
            await interaction.response.send_message(
                content,
                view=ContinuousExitConfirmView(service=self, config_id=int(config["id"]), application_id=int(active["id"]), mode="active", user_id=int(interaction.user.id)),
                ephemeral=True,
            )
            return
        approved = await self.repo.get_approved_application(int(config["id"]), int(interaction.user.id))
        if approved:
            content = "确认从通过名单中移除自己并进入冷却期？"
            await interaction.response.send_message(
                content,
                view=ContinuousExitConfirmView(service=self, config_id=int(config["id"]), application_id=int(approved["id"]), mode="approved", user_id=int(interaction.user.id)),
                ephemeral=True,
            )
            return
        await interaction.response.send_message("当前没有可退出的进行中申请或通过记录。", ephemeral=True)

    def _cooldown_until(self, config: dict[str, Any]) -> str:
        return to_utc_iso(utc_now() + timedelta(minutes=int(config.get("cooldown_minutes") or 0)))

    async def confirm_exit(self, interaction: discord.Interaction, *, config_id: int, application_id: int, mode: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        config = await self.repo.get_config(int(config_id))
        application = await self.repo.get_application(int(application_id))
        if not config or not application or int(application.get("user_id") or 0) != int(interaction.user.id):
            await interaction.response.edit_message(content="未找到可退出的申请。", embed=None, view=None)
            return
        cooldown_until = self._cooldown_until(config)
        if mode == "active":
            fresh = await self.repo.get_application(int(application_id)) or application
            if fresh.get("status") != CONT_APP_VOTING:
                await interaction.response.edit_message(content="该申请已不在投票中，无法按进行中申请退出。", embed=None, view=None)
                return
            result = {"event": "withdrawn", "cooldown_until": cooldown_until}
            changed = await self.repo.set_application_status(
                int(application_id),
                CONT_APP_WITHDRAWN,
                reason="申请人主动退出",
                cooldown_until=cooldown_until,
                result=result,
                expected_status=CONT_APP_VOTING,
                require_not_expired=True,
            )
            if not changed:
                fresh = await self.repo.get_application(int(application_id)) or application
                if fresh.get("status") == CONT_APP_VOTING:
                    await self.finalize_application(fresh, operator_id=None)
                    await interaction.response.edit_message(content="该申请投票已到期，已尝试结算，不能再按主动退出处理。", embed=None, view=None)
                    return
                await interaction.response.edit_message(content="该申请已不在投票中，无法按进行中申请退出。", embed=None, view=None)
                return
            updated = await self.repo.get_application(int(application_id)) or fresh
            await self._edit_vote_message(config, updated)
            await self._publish_event(config, updated, "申请人主动退出，本轮投票终止。", result=None)
            await self._log(interaction.guild.id, interaction.user.id, "continuous_application_withdrawn", {"config_id": int(config_id), "application_id": int(application_id)})
            await interaction.response.edit_message(content=f"已退出当前申请。冷却结束：{format_time_pair(cooldown_until)}。", embed=None, view=None)
            return
        if mode == "approved":
            fresh = await self.repo.get_application(int(application_id)) or application
            if fresh.get("status") != CONT_APP_APPROVED:
                await interaction.response.edit_message(content="该记录已不在通过名单中。", embed=None, view=None)
                return
            result = ContinuousApplicationRepo.decode_result(fresh.get("result_json"))
            result["event"] = "approved_withdrawn"
            changed = await self.repo.set_application_status(
                int(application_id),
                CONT_APP_APPROVED_WITHDRAWN,
                reason="成员主动退出通过名单",
                cooldown_until=cooldown_until,
                result=result,
                expected_status=CONT_APP_APPROVED,
            )
            if not changed:
                await interaction.response.edit_message(content="该记录已不在通过名单中。", embed=None, view=None)
                return
            updated = await self.repo.get_application(int(application_id)) or fresh
            await self._publish_event(config, updated, "成员已主动退出通过名单。", result=None)
            await self._log(interaction.guild.id, interaction.user.id, "continuous_approved_withdrawn", {"config_id": int(config_id), "application_id": int(application_id)})
            await interaction.response.edit_message(content=f"已退出通过名单。冷却结束：{format_time_pair(cooldown_until)}。", embed=None, view=None)
            return
        await interaction.response.edit_message(content="未知退出类型。", embed=None, view=None)

    async def cast_vote_from_panel(self, interaction: discord.Interaction, *, choice: str) -> None:
        try:
            application = await self._application_from_vote_interaction(interaction)
        except Exception as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        config = await self.repo.get_config(int(application["config_id"]))
        if not config:
            await interaction.response.send_message("未找到常态申请配置。", ephemeral=True)
            return
        if self._config_mode(config) == CONT_MODE_SUPPORT:
            await interaction.response.send_message("该申请为支持票收集模式，请使用支持按钮。", ephemeral=True)
            return
        if application.get("status") != CONT_APP_VOTING:
            await interaction.response.send_message("该申请投票已经结束。", ephemeral=True)
            return
        vote_end = parse_iso(application.get("voting_end_at"))
        if vote_end is not None and utc_now() >= vote_end:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.finalize_application(application, operator_id=None)
            await interaction.edit_original_response(content="该申请投票已到期，已尝试结算。")
            return
        voter_roles = self.repo.decode_role_ids(config.get("allowed_voter_role_ids"))
        if not has_any_role(interaction.user, voter_roles):
            await interaction.response.send_message(
                f"你没有投票资格；{format_role_mentions(voter_roles, action='投票')}。",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        old_record = await self.repo.get_vote_record(int(application["id"]), int(interaction.user.id))
        try:
            record = await self.repo.upsert_vote_record(application=application, voter_id=int(interaction.user.id), choice=choice)
        except ValueError as exc:
            message = str(exc)
            if "到期" in message:
                await interaction.response.defer(ephemeral=True, thinking=True)
                await self.finalize_application(application, operator_id=None)
                await interaction.edit_original_response(content="该申请投票已到期，已尝试结算。")
                return
            await interaction.response.send_message(message, ephemeral=True)
            return
        old_choice = str(old_record.get("choice") or "") if old_record else ""
        label = CONT_VOTE_LABELS.get(choice, choice)
        if old_record and old_choice != choice:
            content = f"已将你的投票改为：{label}。"
        elif old_record:
            content = f"你的投票仍为：{label}。"
        else:
            content = f"已投票：{label}。投票结束前可再次点击按钮改票。"
        await self._log(interaction.guild.id, interaction.user.id, "continuous_vote_cast", {"application_id": int(application["id"]), "choice": record.get("choice")})
        await interaction.response.send_message(content, ephemeral=True)

    async def support_from_panel(self, interaction: discord.Interaction) -> None:
        try:
            application = await self._application_from_vote_interaction(interaction)
        except Exception as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        config = await self.repo.get_config(int(application["config_id"]))
        if not config:
            await interaction.response.send_message("未找到常态申请配置。", ephemeral=True)
            return
        if self._config_mode(config) != CONT_MODE_SUPPORT:
            await interaction.response.send_message("该申请不是支持票收集模式。", ephemeral=True)
            return
        if application.get("status") != CONT_APP_VOTING:
            await interaction.response.send_message("该申请收集已经结束。", ephemeral=True)
            return
        vote_end = parse_iso(application.get("voting_end_at"))
        if vote_end is not None and utc_now() >= vote_end:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.finalize_application(application, operator_id=None)
            await interaction.edit_original_response(content="该申请收集已到期，已尝试结算。")
            return
        voter_roles = self.repo.decode_role_ids(config.get("allowed_voter_role_ids"))
        if not has_any_role(interaction.user, voter_roles):
            await interaction.response.send_message(
                f"你没有支持资格；{format_role_mentions(voter_roles, action='支持')}。",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        old_record = await self.repo.get_vote_record(int(application["id"]), int(interaction.user.id))
        try:
            record = await self.repo.upsert_vote_record(application=application, voter_id=int(interaction.user.id), choice=CONT_VOTE_SUPPORT)
        except ValueError as exc:
            message = str(exc)
            if "到期" in message:
                await self.finalize_application(application, operator_id=None)
                await interaction.edit_original_response(content="该申请收集已到期，已尝试结算。")
                return
            await interaction.edit_original_response(content=message)
            return
        finalized = await self.finalize_application(application, operator_id=None, reject_when_unmet=False)
        await self._log(interaction.guild.id, interaction.user.id, "continuous_support_cast", {"application_id": int(application["id"]), "choice": record.get("choice")})
        if finalized and finalized.get("passed"):
            await interaction.edit_original_response(content="已支持。支持票已达到目标，申请已通过。")
        elif old_record and str(old_record.get("choice") or "") == CONT_VOTE_SUPPORT:
            await interaction.edit_original_response(content="你已经支持过该申请。")
        else:
            await interaction.edit_original_response(content="已支持。收集期间可点击“撤回支持”。")

    async def withdraw_support_from_panel(self, interaction: discord.Interaction) -> None:
        try:
            application = await self._application_from_vote_interaction(interaction)
        except Exception as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        config = await self.repo.get_config(int(application["config_id"]))
        if not config:
            await interaction.response.send_message("未找到常态申请配置。", ephemeral=True)
            return
        if self._config_mode(config) != CONT_MODE_SUPPORT:
            await interaction.response.send_message("该申请不是支持票收集模式。", ephemeral=True)
            return
        if application.get("status") != CONT_APP_VOTING:
            await interaction.response.send_message("该申请收集已经结束。", ephemeral=True)
            return
        voter_roles = self.repo.decode_role_ids(config.get("allowed_voter_role_ids"))
        if not has_any_role(interaction.user, voter_roles):
            await interaction.response.send_message(
                f"你没有支持资格；{format_role_mentions(voter_roles, action='支持')}。",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            changed = await self.repo.delete_vote_record(application=application, voter_id=int(interaction.user.id), choice=CONT_VOTE_SUPPORT)
        except ValueError as exc:
            message = str(exc)
            if "到期" in message:
                await interaction.response.defer(ephemeral=True, thinking=True)
                await self.finalize_application(application, operator_id=None)
                await interaction.edit_original_response(content="该申请收集已到期，已尝试结算。")
                return
            await interaction.response.send_message(message, ephemeral=True)
            return
        if changed:
            await self._log(interaction.guild.id, interaction.user.id, "continuous_support_withdrawn", {"application_id": int(application["id"])})
            await interaction.response.send_message("已撤回支持。", ephemeral=True)
        else:
            await interaction.response.send_message("你尚未支持该申请。", ephemeral=True)

    async def show_my_vote_from_panel(self, interaction: discord.Interaction) -> None:
        try:
            application = await self._application_from_vote_interaction(interaction)
        except Exception as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        config = await self.repo.get_config(int(application["config_id"]))
        if not config:
            await interaction.response.send_message("未找到常态申请配置。", ephemeral=True)
            return
        record = await self.repo.get_vote_record(int(application["id"]), int(interaction.user.id))
        await interaction.response.send_message(
            embed=build_continuous_vote_status_embed(config, application, record),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def finalize_due_applications(self) -> int:
        due = await self.repo.list_due_applications(utc_now_iso())
        completed = 0
        for application in due:
            try:
                await self.finalize_application(application, operator_id=None)
                completed += 1
            except Exception:
                log.exception("Failed to finalize continuous application %s", application.get("id"))
        return completed

    async def finalize_application(self, application: dict[str, Any], *, operator_id: int | None = None, reject_when_unmet: bool = True) -> dict[str, Any] | None:
        fresh = await self.repo.get_application(int(application["id"]))
        if not fresh or fresh.get("status") != CONT_APP_VOTING:
            return None
        config = await self.repo.get_config(int(fresh["config_id"]))
        if not config:
            return None
        finalized = await self.repo.finalize_voting_application(
            int(fresh["id"]),
            min_total_votes=int(config.get("min_total_votes") or 0),
            approval_threshold_percent=float(config.get("approval_threshold_percent") or 0),
            cooldown_until_if_rejected=self._cooldown_until(config),
            mode=self._config_mode(config),
            support_target_votes=int(config.get("support_target_votes") or 0) if config.get("support_target_votes") is not None else None,
            reject_when_unmet=reject_when_unmet,
        )
        if finalized is None:
            return None
        updated, result = finalized
        await self._edit_vote_message(config, updated, result=result)
        event = f"{'通过' if result['passed'] else '未通过'}：<@{int(updated.get('user_id') or 0)}> 申请「{sanitize_public_text(updated.get('field_name'), max_len=80)}」。"
        await self._publish_result_event(config, updated, event, result=result)
        await self._log(int(config["guild_id"]), operator_id, "continuous_application_finalized", {"config_id": int(config["id"]), "application_id": int(updated["id"]), "result": result})
        return result

    async def _edit_vote_message(self, config: dict[str, Any], application: dict[str, Any], *, result: dict[str, Any] | None = None) -> bool:
        channel = await self._get_text_channel(int(application.get("vote_channel_id") or config.get("voting_channel_id") or 0))
        message_id = int(application.get("vote_message_id") or 0)
        if channel is None or not message_id:
            return False
        try:
            message = await channel.fetch_message(message_id)
            final_result = result if result is not None else ContinuousApplicationRepo.decode_result(application.get("result_json"))
            display_result = final_result if final_result.get("total_votes") is not None else None
            view = self.vote_view(self._config_mode(config)) if str(application.get("status") or "") == CONT_APP_VOTING else None
            await message.edit(
                embed=build_continuous_application_embed(config, application, result=display_result),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        except Exception:
            log.exception("Failed to edit continuous vote message for application %s", application.get("id"))
            return False

    async def _refresh_vote_displays(self, config: dict[str, Any]) -> list[str]:
        applications = await self.repo.list_open_applications(int(config["id"]))
        if not applications:
            return ["投票面板：当前没有进行中的申请。"]

        success = 0
        failed: list[int] = []
        for application in applications:
            ok = await self._edit_vote_message(config, application)
            if ok:
                success += 1
            else:
                failed.append(int(application.get("id") or 0))

        lines = [f"投票面板：进行中 {len(applications)}，成功 {success}，失败 {len(failed)}。"]
        if failed:
            lines.append("未刷新申请 ID：" + "、".join(str(app_id) for app_id in failed[:20]))
        return lines

    async def refresh_display_messages(
        self,
        guild_id: int,
        config: dict[str, Any],
        *,
        scope: str = "auto",
        operator_id: int | None = None,
    ) -> str:
        scope = str(scope or "auto")
        valid_scopes = {"auto", "all", "entry", "vote"}
        if scope not in valid_scopes:
            raise ValueError("未知刷新范围。")

        scopes = ["entry", "vote"] if scope in ("auto", "all") else [scope]
        fresh = await self.repo.get_config(int(config["id"])) or config
        lines = [f"刷新展示｜常态申请 #{fresh['id']}《{sanitize_public_text(fresh.get('name'), max_len=120)}》", f"范围：{scope}"]
        if "entry" in scopes:
            ok = await self.refresh_entry(fresh, reason="manual_refresh_display", operator_id=operator_id)
            lines.append("入口面板：" + ("成功。" if ok else "未刷新（未记录入口消息或无法编辑）。"))
        if "vote" in scopes:
            lines.extend(await self._refresh_vote_displays(fresh))
        await self._log(int(guild_id), operator_id, "continuous_display_refreshed", {"config_id": int(fresh["id"]), "scope": scope, "scopes": scopes})
        return "\n".join(lines)

    async def _publish_event(self, config: dict[str, Any], application: dict[str, Any], event: str, *, result: dict[str, Any] | None = None) -> bool:
        channel = await self._get_text_channel(int(config.get("public_channel_id") or 0))
        if channel is None:
            return False
        try:
            await channel.send(
                embed=build_continuous_public_event_embed(config, application, event=event, result=result),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        except Exception:
            log.exception("Failed to publish continuous event for application %s", application.get("id"))
            return False

    async def _publish_result_event(self, config: dict[str, Any], application: dict[str, Any], event: str, *, result: dict[str, Any]) -> bool:
        published = await self._publish_event(config, application, event, result=result)
        if self._config_mode(config) != CONT_MODE_SUPPORT or not result.get("passed"):
            return published
        channel = await self._get_text_channel(int(config.get("public_channel_id") or 0))
        if channel is None:
            return published
        supporters = await self.repo.list_vote_records(int(application["id"]), choice=CONT_VOTE_SUPPORT)
        try:
            for embed in build_continuous_supporter_list_embeds(config, application, supporters):
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            return True
        except Exception:
            log.exception("Failed to publish continuous supporters for application %s", application.get("id"))
            return published

    async def build_status_embed(self, guild_id: int, *, config_id: int | None = None) -> discord.Embed:
        if config_id is not None:
            configs = [await self.repo.resolve_config(int(guild_id), int(config_id))]
        else:
            configs = await self.repo.list_configs(int(guild_id), include_archived=False, limit=25)
        counts: dict[int, tuple[int, int]] = {}
        warnings: list[str] = []
        check_application_messages = config_id is not None
        for config in configs:
            cid = int(config["id"])
            counts[cid] = (await self.repo.count_open_by_config(cid), await self.repo.count_approved_by_config(cid))
            entry_warning = await self._message_warning(
                label=f"配置 #{cid} 入口",
                channel_id=int(config.get("entry_channel_id") or 0),
                message_id=int(config.get("entry_message_id") or 0),
            )
            if entry_warning:
                warnings.append(entry_warning)
            if not check_application_messages:
                continue
            for application in (await self.repo.list_open_applications(cid))[:10]:
                vote_warning = await self._message_warning(
                    label=f"申请 #{application['id']} 投票",
                    channel_id=int(application.get("vote_channel_id") or config.get("voting_channel_id") or 0),
                    message_id=int(application.get("vote_message_id") or 0),
                )
                if vote_warning:
                    warnings.append(vote_warning)
        embed = build_continuous_status_embed(configs, counts)
        if warnings:
            embed.add_field(name="展示消息检查", value="\n".join(warnings)[:1024], inline=False)
        return embed

    async def _message_warning(self, *, label: str, channel_id: int, message_id: int) -> str | None:
        if not message_id:
            return f"{label}：未记录消息 ID。"
        channel = await self._get_text_channel(channel_id)
        if channel is None:
            return f"{label}：频道不可读。"
        try:
            await channel.fetch_message(int(message_id))
            return None
        except discord.NotFound:
            return f"{label}：消息不存在，可能已被删除。"
        except Exception as exc:
            return f"{label}：检查失败（{str(exc)[:80]}）。"

    async def build_approved_list_embed(self, guild_id: int, *, config_id: int | None = None, field_name: str | None = None) -> discord.Embed:
        config = await self.repo.resolve_config(int(guild_id), int(config_id)) if config_id is not None else None
        rows = await self.repo.list_approved_applications(
            guild_id=int(guild_id),
            config_id=int(config["id"]) if config else None,
            field_name=sanitize_public_text(field_name, max_len=80, fallback="").strip() or None,
        )
        return build_continuous_approved_list_embed(rows, config=config, field_name=field_name)

    async def manual_return_application(self, *, guild: discord.Guild, application_id: int, operator_id: int, reason: str | None = None) -> None:
        application = await self.repo.get_application(int(application_id))
        if not application or int(application.get("guild_id") or 0) != int(guild.id):
            raise ValueError("未找到该申请，或该申请不属于当前服务器。")
        if application.get("status") != CONT_APP_VOTING:
            raise ValueError("只有投票中的申请可以打回。")
        config = await self.repo.get_config(int(application["config_id"]))
        if not config:
            raise ValueError("未找到常态申请配置。")
        changed = await self.repo.set_application_status(
            int(application["id"]),
            CONT_APP_RETURNED,
            reason=reason or "管理员打回，需修改后重新提交",
            expected_status=CONT_APP_VOTING,
        )
        if not changed:
            raise ValueError("该申请已不在投票中，无法打回。")
        updated = await self.repo.get_application(int(application["id"])) or application
        await self._edit_vote_message(config, updated)
        await self._publish_event(config, updated, "管理员已打回该申请，申请人可修改后重新提交。", result=None)
        await self._log(guild.id, operator_id, "continuous_application_returned", {"application_id": int(application_id), "reason": reason})

    async def manual_reject_application(self, *, guild: discord.Guild, application_id: int, operator_id: int, reason: str | None = None) -> None:
        application = await self.repo.get_application(int(application_id))
        if not application or int(application.get("guild_id") or 0) != int(guild.id):
            raise ValueError("未找到该申请，或该申请不属于当前服务器。")
        if application.get("status") != CONT_APP_VOTING:
            raise ValueError("只有投票中的申请可以拒绝。")
        config = await self.repo.get_config(int(application["config_id"]))
        if not config:
            raise ValueError("未找到常态申请配置。")
        cooldown_until = self._cooldown_until(config)
        changed = await self.repo.set_application_status(
            int(application["id"]),
            CONT_APP_REJECTED,
            reason=reason or "管理员拒绝",
            cooldown_until=cooldown_until,
            expected_status=CONT_APP_VOTING,
        )
        if not changed:
            raise ValueError("该申请已不在投票中，无法拒绝。")
        updated = await self.repo.get_application(int(application["id"])) or application
        await self._edit_vote_message(config, updated)
        await self._publish_event(config, updated, "管理员已拒绝该申请，申请人进入冷却期。", result=None)
        await self._log(guild.id, operator_id, "continuous_application_rejected", {"application_id": int(application_id), "reason": reason, "cooldown_until": cooldown_until})

    async def manual_remove_approved(
        self,
        *,
        guild: discord.Guild,
        config_id: int,
        user_id: int,
        operator_id: int,
        field_name: str | None = None,
        reason: str | None = None,
    ) -> None:
        config = await self.repo.resolve_config(int(guild.id), int(config_id))
        application = await self.repo.find_approved_application(config_id=int(config["id"]), user_id=int(user_id), field_name=field_name)
        if not application:
            raise ValueError("未找到该成员的通过记录。")
        result = ContinuousApplicationRepo.decode_result(application.get("result_json"))
        result["event"] = "admin_removed_approved"
        cooldown_until = self._cooldown_until(config)
        changed = await self.repo.set_application_status(
            int(application["id"]),
            CONT_APP_APPROVED_WITHDRAWN,
            reason=reason or "管理员移除通过名单",
            cooldown_until=cooldown_until,
            result=result,
            expected_status=CONT_APP_APPROVED,
        )
        if not changed:
            raise ValueError("该记录已不在通过名单中。")
        updated = await self.repo.get_application(int(application["id"])) or application
        await self._publish_event(config, updated, "管理员已将该成员移出通过名单。", result=None)
        await self._log(guild.id, operator_id, "continuous_approved_removed", {"config_id": int(config["id"]), "application_id": int(application["id"]), "user_id": int(user_id), "reason": reason})
