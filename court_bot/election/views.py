from __future__ import annotations

import math
from typing import Any

import discord

from .constants import CUSTOM_ID_PREFIX, MAX_SELF_INTRO_LENGTH, REGISTRATION_SELECT_ALL_VALUE
from .embeds import build_vote_confirm_embed, build_vote_page_embed


def resolve_registration_selected_field_keys(fields: list[dict[str, Any]], selected_values: list[str]) -> list[str]:
    """Resolve the field keys selected from the registration select menus.

    Discord select menus can contain at most 25 options. When there are already
    25 election fields, the "全选" shortcut is rendered as a separate select, but
    both paths should produce the same normalized field-key list.
    """

    field_keys = [str(field["field_key"]) for field in fields]
    if REGISTRATION_SELECT_ALL_VALUE in selected_values:
        return field_keys
    valid_keys = set(field_keys)
    return list(dict.fromkeys(str(value) for value in selected_values if str(value) in valid_keys))


class RegistrationIntroModal(discord.ui.Modal):
    def __init__(self, *, cog, election_id: int, selected_field_keys: list[str], is_edit: bool = False):
        super().__init__(title="提交募选报名", timeout=300)
        self.cog = cog
        self.election_id = int(election_id)
        self.selected_field_keys = selected_field_keys
        self.is_edit = is_edit
        self.self_intro = discord.ui.TextInput(
            label="参选宣言（可选，不能艾特人或身份组）",
            style=discord.TextStyle.paragraph,
            max_length=MAX_SELF_INTRO_LENGTH,
            required=False,
        )
        self.add_item(self.self_intro)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_registration_submit(
            interaction,
            election_id=self.election_id,
            selected_field_keys=self.selected_field_keys,
            self_intro=str(self.self_intro.value or ""),
            is_edit=self.is_edit,
        )


class FieldSelectView(discord.ui.View):
    def __init__(self, *, cog, election: dict[str, Any], fields: list[dict[str, Any]], is_edit: bool = False):
        super().__init__(timeout=300)
        self.cog = cog
        self.election = election
        self.is_edit = is_edit
        self.fields = list(fields)

        all_option = discord.SelectOption(
            label="全选",
            value=REGISTRATION_SELECT_ALL_VALUE,
            description="报名参选所有岗位",
        )
        field_options = [
            discord.SelectOption(
                label=str(field["name"])[:100],
                value=str(field["field_key"]),
                description=f"当选人数：{int(field['winner_count'])}"[:100],
            )
            for field in self.fields[:25]
        ]

        # A single Discord select can only have 25 options. For <=24 fields, put
        # "全选" in the same menu. For 25 fields, keep all 25 individual fields in
        # the normal menu and render "全选" as a separate shortcut select so the
        # last field is not hidden.
        if len(self.fields) <= 24:
            options = [all_option, *field_options]
            placeholder = "选择你愿意参选的岗位（可多选）"
        else:
            all_select = discord.ui.Select(
                placeholder="快捷选择",
                min_values=1,
                max_values=1,
                options=[all_option],
            )
            all_select.callback = self._on_select
            self.add_item(all_select)
            options = field_options
            placeholder = "选择你愿意参选的岗位（可多选）"

        self.select = discord.ui.Select(
            placeholder=placeholder,
            min_values=1,
            max_values=len(options),
            options=options,
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if interaction.guild is not None and not await self.cog.can_user_register(interaction.user, self.election):
            await interaction.response.send_message("你没有本次募选报名资格，或报名资格已变更。", ephemeral=True)
            return
        selected_values = [str(value) for value in interaction.data.get("values", [])] if isinstance(interaction.data, dict) else []
        selected_field_keys = resolve_registration_selected_field_keys(self.fields, selected_values)
        if not selected_field_keys:
            await interaction.response.send_message("请选择至少一个岗位。", ephemeral=True)
            return
        await interaction.response.send_modal(
            RegistrationIntroModal(
                cog=self.cog,
                election_id=int(self.election["id"]),
                selected_field_keys=selected_field_keys,
                is_edit=self.is_edit,
            )
        )


class RegistrationEntryView(discord.ui.View):
    def __init__(self, *, cog):
        super().__init__(timeout=None)
        self.cog = cog
        self.add_item(discord.ui.Button(label="报名", style=discord.ButtonStyle.success, custom_id=f"{CUSTOM_ID_PREFIX}register"))
        self.add_item(discord.ui.Button(label="我的报名", style=discord.ButtonStyle.secondary, custom_id=f"{CUSTOM_ID_PREFIX}my_registration"))
        self.add_item(discord.ui.Button(label="编辑报名", style=discord.ButtonStyle.primary, custom_id=f"{CUSTOM_ID_PREFIX}edit_registration"))
        self.add_item(discord.ui.Button(label="撤回报名", style=discord.ButtonStyle.danger, custom_id=f"{CUSTOM_ID_PREFIX}withdraw_registration"))
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.callback = self._dispatch

    async def _dispatch(self, interaction: discord.Interaction) -> None:
        custom_id = interaction.data.get("custom_id") if isinstance(interaction.data, dict) else ""
        if custom_id.endswith("register"):
            await self.cog.open_registration_flow(interaction, is_edit=False)
        elif custom_id.endswith("my_registration"):
            await self.cog.show_my_registration(interaction)
        elif custom_id.endswith("edit_registration"):
            await self.cog.open_registration_flow(interaction, is_edit=True)
        elif custom_id.endswith("withdraw_registration"):
            await self.cog.withdraw_registration(interaction)
        else:
            await interaction.response.send_message("未知报名入口按钮。", ephemeral=True)


class VoteEntryView(discord.ui.View):
    def __init__(self, *, cog):
        super().__init__(timeout=None)
        self.cog = cog
        button = discord.ui.Button(label="开始投票", style=discord.ButtonStyle.primary, custom_id=f"{CUSTOM_ID_PREFIX}start_vote")
        button.callback = self._on_start_vote
        self.add_item(button)

    async def _on_start_vote(self, interaction: discord.Interaction) -> None:
        await self.cog.start_vote_interaction(interaction)


class VoteSelectionState:
    def __init__(self, *, selected: set[int] | None = None, page: int = 0):
        self.selected = selected or set()
        self.page = page


class VoteSelectionView(discord.ui.View):
    def __init__(self, *, cog, election: dict[str, Any], candidates: list[dict[str, Any]], voter_id: int, state: VoteSelectionState | None = None):
        super().__init__(timeout=900)
        self.cog = cog
        self.election = election
        self.candidates = candidates
        self.voter_id = int(voter_id)
        self.state = state or VoteSelectionState()
        self.page_size = 25
        self.max_selectable = min(int(election.get("vote_max_selections") or 1), len(candidates))
        self.total_pages = max(1, math.ceil(len(candidates) / self.page_size))
        self._build_items()

    def page_candidates(self) -> list[dict[str, Any]]:
        start = self.state.page * self.page_size
        return self.candidates[start : start + self.page_size]

    def _page_candidates(self) -> list[dict[str, Any]]:
        return self.page_candidates()

    def _build_items(self) -> None:
        self.clear_items()
        page_regs = self._page_candidates()
        if page_regs:
            select = discord.ui.Select(
                placeholder="选择/取消本页候选人",
                min_values=0,
                max_values=len(page_regs),
                options=[
                    discord.SelectOption(
                        label=str(reg.get("display_name"))[:100],
                        value=str(int(reg.get("user_id") or 0)),
                        description=f"用户ID：{int(reg.get('user_id') or 0)}"[:100],
                        default=int(reg.get("user_id") or 0) in self.state.selected,
                    )
                    for reg in page_regs
                ],
            )
            select.callback = self._on_select
            self.add_item(select)
        prev_btn = discord.ui.Button(label="上一页", style=discord.ButtonStyle.secondary, disabled=self.state.page <= 0, row=1)
        next_btn = discord.ui.Button(label="下一页", style=discord.ButtonStyle.secondary, disabled=self.state.page >= self.total_pages - 1, row=1)
        clear_btn = discord.ui.Button(label="清空选择", style=discord.ButtonStyle.danger, row=1)
        done_btn = discord.ui.Button(label="完成选择", style=discord.ButtonStyle.success, row=1)
        prev_btn.callback = self._prev
        next_btn.callback = self._next
        clear_btn.callback = self._clear
        done_btn.callback = self._done
        self.add_item(prev_btn)
        self.add_item(next_btn)
        self.add_item(clear_btn)
        self.add_item(done_btn)

    async def _ensure_owner(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) != self.voter_id:
            await interaction.response.send_message("这不是你的投票界面。", ephemeral=True)
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self._build_items()
        await interaction.response.edit_message(
            embed=build_vote_page_embed(
                self.election,
                self.state.page,
                self.total_pages,
                len(self.state.selected),
                self.max_selectable,
                self.page_candidates(),
            ),
            view=self,
        )

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_owner(interaction):
            return
        current_page_ids = {int(reg.get("user_id") or 0) for reg in self._page_candidates()}
        chosen = {int(v) for v in interaction.data.get("values", []) if int(v) in current_page_ids} if isinstance(interaction.data, dict) else set()
        next_selected = (self.state.selected - current_page_ids) | chosen
        if len(next_selected) > self.max_selectable:
            await interaction.response.send_message(f"选择数超过上限，最多可选 {self.max_selectable} 人；本次选择未保存，请减少选择后再试。", ephemeral=True)
            return
        self.state.selected = next_selected
        await self._refresh(interaction)

    async def _prev(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_owner(interaction):
            return
        self.state.page = max(0, self.state.page - 1)
        await self._refresh(interaction)

    async def _next(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_owner(interaction):
            return
        self.state.page = min(self.total_pages - 1, self.state.page + 1)
        await self._refresh(interaction)

    async def _clear(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_owner(interaction):
            return
        self.state.selected.clear()
        await self._refresh(interaction)

    async def _done(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_owner(interaction):
            return
        if not self.state.selected:
            await interaction.response.send_message("请至少选择一名候选人。", ephemeral=True)
            return
        selected_regs = [reg for reg in self.candidates if int(reg.get("user_id") or 0) in self.state.selected]
        await interaction.response.edit_message(
            embed=build_vote_confirm_embed(self.election, selected_regs),
            view=VoteConfirmView(cog=self.cog, election=self.election, selected_user_ids=sorted(self.state.selected), voter_id=self.voter_id, candidates=self.candidates, state=self.state),
        )


class VoteConfirmView(discord.ui.View):
    def __init__(self, *, cog, election: dict[str, Any], selected_user_ids: list[int], voter_id: int, candidates: list[dict[str, Any]], state: VoteSelectionState):
        super().__init__(timeout=900)
        self.cog = cog
        self.election = election
        self.selected_user_ids = selected_user_ids
        self.voter_id = int(voter_id)
        self.candidates = candidates
        self.state = state
        confirm = discord.ui.Button(label="确认投票", style=discord.ButtonStyle.success)
        back = discord.ui.Button(label="返回修改", style=discord.ButtonStyle.secondary)
        cancel = discord.ui.Button(label="取消", style=discord.ButtonStyle.danger)
        confirm.callback = self._confirm
        back.callback = self._back
        cancel.callback = self._cancel
        self.add_item(confirm)
        self.add_item(back)
        self.add_item(cancel)

    async def _ensure_owner(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) != self.voter_id:
            await interaction.response.send_message("这不是你的投票界面。", ephemeral=True)
            return False
        return True

    async def _confirm(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_owner(interaction):
            return
        await self.cog.confirm_vote(interaction, election=self.election, selected_user_ids=self.selected_user_ids)

    async def _back(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_owner(interaction):
            return
        view = VoteSelectionView(cog=self.cog, election=self.election, candidates=self.candidates, voter_id=self.voter_id, state=self.state)
        await interaction.response.edit_message(
            embed=build_vote_page_embed(self.election, self.state.page, view.total_pages, len(self.state.selected), view.max_selectable, view.page_candidates()),
            view=view,
        )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_owner(interaction):
            return
        await interaction.response.edit_message(content="已取消投票操作。", embed=None, view=None)
