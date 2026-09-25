from __future__ import annotations

import discord


class ApplicationReasonModal(discord.ui.Modal):
    def __init__(self, service):
        super().__init__(title="申请监察组", timeout=300)
        self.service = service
        self.reason = discord.ui.TextInput(label="申请理由", style=discord.TextStyle.paragraph, max_length=400, required=True)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await self.service.submit(interaction, str(self.reason.value or ""))


class ReviewReasonModal(discord.ui.Modal):
    def __init__(self, service, choice: str, app_id: int):
        super().__init__(title=f"监察组申请：{choice}", timeout=300)
        self.service = service
        self.choice = choice
        self.app_id = app_id
        self.reason = discord.ui.TextInput(label="审核理由" if choice == "同意" else "拒绝理由",
                                           style=discord.TextStyle.paragraph, required=choice == "拒绝",
                                           max_length=400)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await self.service.cast_vote(interaction, self.app_id, self.choice, str(self.reason.value or ""))


class MemberApplicationEntryView(discord.ui.View):
    def __init__(self, service):
        super().__init__(timeout=None)
        self.service = service
        for label, action, style in (("申请监察组", "apply", discord.ButtonStyle.success),
                                     ("我的申请", "mine", discord.ButtonStyle.secondary),
                                     ("撤回申请", "withdraw", discord.ButtonStyle.danger)):
            button = discord.ui.Button(label=label, style=style, custom_id=f"insp_member_entry_{action}")
            button.callback = self._dispatch
            self.add_item(button)

    async def _dispatch(self, interaction: discord.Interaction):
        action = str(interaction.data.get("custom_id", "")).removeprefix("insp_member_entry_")
        if action == "apply":
            await self.service.open_application(interaction)
        elif action == "mine":
            await self.service.show_mine(interaction)
        elif action == "withdraw":
            await self.service.withdraw(interaction)


class MemberApplicationReviewView(discord.ui.View):
    def __init__(self, service, *, disabled: bool = False):
        super().__init__(timeout=None)
        self.service = service
        for label, action, style in (("同意", "approve", discord.ButtonStyle.success),
                                     ("拒绝", "reject", discord.ButtonStyle.danger),
                                     ("撤销投票", "remove", discord.ButtonStyle.secondary)):
            button = discord.ui.Button(label=label, style=style, custom_id=f"insp_member_review_{action}", disabled=disabled)
            button.callback = self._dispatch
            self.add_item(button)

    async def _dispatch(self, interaction: discord.Interaction):
        action = str(interaction.data.get("custom_id", "")).removeprefix("insp_member_review_")
        await self.service.review_button(interaction, action)
