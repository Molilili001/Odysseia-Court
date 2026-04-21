from __future__ import annotations

import discord


class ArchiveView(discord.ui.View):
    """结案后用于“归档并删除”的管理按钮。"""

    def __init__(self, *, bot, case_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.case_id = case_id

        self.btn_archive_delete = discord.ui.Button(
            label="归档并删除",
            style=discord.ButtonStyle.danger,
            custom_id=f"court_archive_delete_{case_id}",
            row=0,
        )
        self.btn_archive_delete.callback = self._on_archive_delete
        self.add_item(self.btn_archive_delete)

    async def _on_archive_delete(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内操作。", ephemeral=True)
            return

        if not await self.bot.is_admin(interaction.user, interaction.guild):
            await interaction.response.send_message("无权限。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            await self.bot.archive_and_delete_case(case_id=self.case_id, operator=interaction.user)
        except Exception as e:
            await interaction.edit_original_response(content=f"归档失败：{e}")
            return

        await interaction.edit_original_response(content="已归档并删除该案件频道。")
