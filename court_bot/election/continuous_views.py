from __future__ import annotations

from typing import Any

import discord

from .constants import MAX_SELF_INTRO_LENGTH
from .continuous_constants import CONTINUOUS_CUSTOM_ID_PREFIX, CONT_MODE_APPROVAL, CONT_MODE_SUPPORT, CONT_VOTE_NO, CONT_VOTE_YES
from .text_utils import sanitize_public_text
from .time_utils import format_beijing


CONTINUOUS_APPLICATION_LIST_PAGE_SIZE = 10


def _application_list_total_pages(applications: list[dict[str, Any]], page_size: int) -> int:
    return max(1, (len(applications) + int(page_size) - 1) // int(page_size))


def _application_option_label(application: dict[str, Any]) -> str:
    fallback = str(int(application.get("user_id") or 0)) if application.get("user_id") else "未知用户"
    display_name = sanitize_public_text(application.get("display_name"), max_len=54, fallback=fallback)
    field_name = sanitize_public_text(application.get("field_name"), max_len=40, fallback="未选择岗位")
    label = f"{display_name}｜{field_name}"
    return label[:100] or fallback[:100]


def _application_option_description(application: dict[str, Any]) -> str:
    user_id = int(application.get("user_id") or 0)
    deadline = format_beijing(application.get("voting_end_at"), fallback="未设置")
    return f"ID: {user_id}｜截止 {deadline}"[:100]


class ContinuousApplicationModal(discord.ui.Modal):
    def __init__(self, *, service, config_id: int, field_key: str):
        super().__init__(title="提交常态申请", timeout=300)
        self.service = service
        self.config_id = int(config_id)
        self.field_key = str(field_key)
        self.self_intro = discord.ui.TextInput(
            label="报名宣言（不能艾特人或身份组）",
            style=discord.TextStyle.paragraph,
            max_length=MAX_SELF_INTRO_LENGTH,
            required=True,
        )
        self.add_item(self.self_intro)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.service.handle_application_submit(
            interaction,
            config_id=self.config_id,
            field_key=self.field_key,
            self_intro=str(self.self_intro.value or ""),
        )


class ContinuousFieldSelectView(discord.ui.View):
    def __init__(self, *, service, config: dict[str, Any], fields: list[dict[str, Any]]):
        super().__init__(timeout=300)
        self.service = service
        self.config = config
        self.fields = fields
        select = discord.ui.Select(
            placeholder="选择一个申请岗位",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=str(field.get("name"))[:100],
                    value=str(field.get("field_key")),
                )
                for field in fields[:25]
            ],
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", []) if isinstance(interaction.data, dict) else []
        field_key = str(values[0]) if values else ""
        if not field_key:
            await interaction.response.send_message("请选择一个岗位。", ephemeral=True)
            return
        await interaction.response.send_modal(
            ContinuousApplicationModal(service=self.service, config_id=int(self.config["id"]), field_key=field_key)
        )


class ContinuousEntryView(discord.ui.View):
    def __init__(self, *, service):
        super().__init__(timeout=None)
        self.service = service
        apply_button = discord.ui.Button(label="提交申请", style=discord.ButtonStyle.success, custom_id=f"{CONTINUOUS_CUSTOM_ID_PREFIX}apply")
        status_button = discord.ui.Button(label="我的申请", style=discord.ButtonStyle.secondary, custom_id=f"{CONTINUOUS_CUSTOM_ID_PREFIX}my_status")
        exit_button = discord.ui.Button(label="退出申请", style=discord.ButtonStyle.danger, custom_id=f"{CONTINUOUS_CUSTOM_ID_PREFIX}exit")
        list_button = discord.ui.Button(label="当前报名人名单", style=discord.ButtonStyle.primary, custom_id=f"{CONTINUOUS_CUSTOM_ID_PREFIX}current_applicants")
        apply_button.callback = self._dispatch
        status_button.callback = self._dispatch
        exit_button.callback = self._dispatch
        list_button.callback = self._dispatch
        self.add_item(apply_button)
        self.add_item(status_button)
        self.add_item(exit_button)
        self.add_item(list_button)

    async def _dispatch(self, interaction: discord.Interaction) -> None:
        custom_id = interaction.data.get("custom_id") if isinstance(interaction.data, dict) else ""
        if custom_id.endswith("apply"):
            await self.service.open_application_flow(interaction)
        elif custom_id.endswith("my_status"):
            await self.service.show_my_status_from_entry(interaction)
        elif custom_id.endswith("exit"):
            await self.service.request_exit_from_entry(interaction)
        elif custom_id.endswith("current_applicants"):
            await self.service.show_current_applications_from_entry(interaction)
        else:
            await interaction.response.send_message("未知常态申请入口按钮。", ephemeral=True)


class ContinuousApplicationListView(discord.ui.View):
    def __init__(
        self,
        *,
        service,
        config: dict[str, Any],
        applications: list[dict[str, Any]],
        page: int = 0,
        requester_id: int,
        page_size: int = CONTINUOUS_APPLICATION_LIST_PAGE_SIZE,
    ):
        super().__init__(timeout=300)
        self.service = service
        self.config = config
        self.config_id = int(config["id"])
        self.page_size = int(page_size)
        self.total_pages = _application_list_total_pages(applications, self.page_size)
        self.page = min(max(0, int(page)), self.total_pages - 1)
        self.requester_id = int(requester_id)
        start = self.page * self.page_size
        rows = applications[start : start + self.page_size]
        if rows:
            select = discord.ui.Select(
                placeholder="选择报名人，查看对应投票链接",
                min_values=1,
                max_values=1,
                options=[
                    discord.SelectOption(
                        label=_application_option_label(app),
                        description=_application_option_description(app),
                        value=str(int(app.get("id") or 0)),
                    )
                    for app in rows
                ],
            )
            select.callback = self._select_application
            self.add_item(select)

        if self.total_pages > 1:
            previous_button = discord.ui.Button(label="上一页", style=discord.ButtonStyle.secondary, disabled=self.page <= 0)
            next_button = discord.ui.Button(label="下一页", style=discord.ButtonStyle.secondary, disabled=self.page >= self.total_pages - 1)
            previous_button.callback = self._previous_page
            next_button.callback = self._next_page
            self.add_item(previous_button)
            self.add_item(next_button)

    async def _ensure_owner(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.requester_id:
            return True
        await interaction.response.send_message("这不是你的报名人名单面板。", ephemeral=True)
        return False

    async def _select_application(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_owner(interaction):
            return
        values = interaction.data.get("values", []) if isinstance(interaction.data, dict) else []
        application_id = int(values[0]) if values else 0
        if not application_id:
            await interaction.response.send_message("请选择一个报名人。", ephemeral=True)
            return
        await self.service.show_application_detail_from_list(
            interaction,
            config_id=self.config_id,
            application_id=application_id,
        )

    async def _previous_page(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_owner(interaction):
            return
        await self.service.show_current_applications_page(
            interaction,
            config_id=self.config_id,
            page=self.page - 1,
            requester_id=self.requester_id,
        )

    async def _next_page(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_owner(interaction):
            return
        await self.service.show_current_applications_page(
            interaction,
            config_id=self.config_id,
            page=self.page + 1,
            requester_id=self.requester_id,
        )


class ContinuousApplicationJumpView(discord.ui.View):
    def __init__(self, *, jump_url: str | None):
        super().__init__(timeout=300)
        if jump_url:
            self.add_item(discord.ui.Button(label="跳转投票消息", style=discord.ButtonStyle.link, url=jump_url))


class ContinuousVoteView(discord.ui.View):
    def __init__(self, *, service, mode: str = CONT_MODE_APPROVAL):
        super().__init__(timeout=None)
        self.service = service
        self.mode = str(mode or CONT_MODE_APPROVAL)
        if self.mode == CONT_MODE_SUPPORT:
            support = discord.ui.Button(label="支持", style=discord.ButtonStyle.success, custom_id=f"{CONTINUOUS_CUSTOM_ID_PREFIX}support_add")
            withdraw = discord.ui.Button(label="撤回支持", style=discord.ButtonStyle.danger, custom_id=f"{CONTINUOUS_CUSTOM_ID_PREFIX}support_remove")
            mine = discord.ui.Button(label="我的支持", style=discord.ButtonStyle.secondary, custom_id=f"{CONTINUOUS_CUSTOM_ID_PREFIX}support_mine")
            support.callback = self._dispatch
            withdraw.callback = self._dispatch
            mine.callback = self._dispatch
            self.add_item(support)
            self.add_item(withdraw)
            self.add_item(mine)
        else:
            yes = discord.ui.Button(label="同意", style=discord.ButtonStyle.success, custom_id=f"{CONTINUOUS_CUSTOM_ID_PREFIX}vote_yes")
            no = discord.ui.Button(label="反对", style=discord.ButtonStyle.danger, custom_id=f"{CONTINUOUS_CUSTOM_ID_PREFIX}vote_no")
            mine = discord.ui.Button(label="我的投票", style=discord.ButtonStyle.secondary, custom_id=f"{CONTINUOUS_CUSTOM_ID_PREFIX}vote_mine")
            yes.callback = self._dispatch
            no.callback = self._dispatch
            mine.callback = self._dispatch
            self.add_item(yes)
            self.add_item(no)
            self.add_item(mine)

    async def _dispatch(self, interaction: discord.Interaction) -> None:
        custom_id = interaction.data.get("custom_id") if isinstance(interaction.data, dict) else ""
        if custom_id.endswith("vote_yes"):
            await self.service.cast_vote_from_panel(interaction, choice=CONT_VOTE_YES)
        elif custom_id.endswith("vote_no"):
            await self.service.cast_vote_from_panel(interaction, choice=CONT_VOTE_NO)
        elif custom_id.endswith("vote_mine"):
            await self.service.show_my_vote_from_panel(interaction)
        elif custom_id.endswith("support_add"):
            await self.service.support_from_panel(interaction)
        elif custom_id.endswith("support_remove"):
            await self.service.withdraw_support_from_panel(interaction)
        elif custom_id.endswith("support_mine"):
            await self.service.show_my_vote_from_panel(interaction)
        else:
            await interaction.response.send_message("未知常态投票按钮。", ephemeral=True)


class ContinuousExitConfirmView(discord.ui.View):
    def __init__(self, *, service, config_id: int, application_id: int, mode: str, user_id: int):
        super().__init__(timeout=300)
        self.service = service
        self.config_id = int(config_id)
        self.application_id = int(application_id)
        self.mode = str(mode)
        self.user_id = int(user_id)
        confirm = discord.ui.Button(label="确认退出", style=discord.ButtonStyle.danger)
        cancel = discord.ui.Button(label="取消", style=discord.ButtonStyle.secondary)
        confirm.callback = self._confirm
        cancel.callback = self._cancel
        self.add_item(confirm)
        self.add_item(cancel)

    async def _ensure_owner(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) != self.user_id:
            await interaction.response.send_message("这不是你的确认面板。", ephemeral=True)
            return False
        return True

    async def _confirm(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_owner(interaction):
            return
        await self.service.confirm_exit(
            interaction,
            config_id=self.config_id,
            application_id=self.application_id,
            mode=self.mode,
        )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_owner(interaction):
            return
        await interaction.response.edit_message(content="已取消退出操作。", embed=None, view=None)
