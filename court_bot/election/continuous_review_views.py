from __future__ import annotations

import discord


class ContinuousReviewReasonModal(discord.ui.Modal):
    def __init__(self, service, app_id: int, choice: str):
        super().__init__(title="同意常态申请" if choice == "yes" else "拒绝常态申请", timeout=300)
        self.service = service
        self.app_id = app_id
        self.choice = choice
        self.reason = discord.ui.TextInput(label="审核理由" if choice == "yes" else "拒绝理由",
            style=discord.TextStyle.paragraph, required=choice == "no", max_length=500)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.service.cast_vote(interaction, self.app_id, self.choice, str(self.reason.value or ""))


class ContinuousReviewView(discord.ui.View):
    def __init__(self, service, *, disabled: bool = False):
        super().__init__(timeout=None)
        self.service = service
        for label, choice, style in (("同意", "yes", discord.ButtonStyle.success),
                                     ("拒绝", "no", discord.ButtonStyle.danger),
                                     ("撤销我的投票", "remove", discord.ButtonStyle.secondary)):
            button = discord.ui.Button(label=label, style=style, custom_id=f"pe:cr:{choice}", disabled=disabled)
            button.callback = self._dispatch
            self.add_item(button)

    async def _dispatch(self, interaction: discord.Interaction) -> None:
        action = str(interaction.data.get("custom_id", "")).removeprefix("pe:cr:")
        await self.service.review_button(interaction, action)
