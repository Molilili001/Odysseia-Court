from __future__ import annotations

import discord

from ..constants import COLOR_BLUE, VIS_PRIVATE, VIS_PUBLIC
from .modals import submit_case_application


DEFAULT_ENTRY_DESCRIPTION = (
    "如需发起议诉申请，请点击下方『提交议诉申请』按钮。\n\n"
    "请准备好被投诉人、申请议诉模式、违反规则与申请说明；管理审核通过后会创建议诉频道。"
)
ATTACHMENT_REMINDER = (
    "📎 **附件提醒**：Discord 按钮表单无法上传文件。"
    "首次申请如需提交图片、视频或文件，请使用 `/议诉 申请`，"
    "并在命令参数中填写证据附件；已提交后需要补充附件时，"
    "可在任意频道使用 `/议诉 补充证据` 并填写议诉编号。"
)


def build_entry_embed(description: str | None = None) -> discord.Embed:
    """构建议诉区入口面板。"""

    normalized_description = (description or DEFAULT_ENTRY_DESCRIPTION).strip()
    embed = discord.Embed(
        title="议诉申请入口",
        description=normalized_description,
        color=COLOR_BLUE,
    )
    embed.add_field(name="如何提交", value="点击下方按钮后按表单提示填写申请内容。", inline=False)
    embed.add_field(name="附件提醒", value=ATTACHMENT_REMINDER, inline=False)
    embed.set_footer(text="提交后将进入管理审核；请勿重复提交同一议诉。")
    return embed


class EntryApplyModal(discord.ui.Modal):
    """入口面板按钮使用的申请表单（不含附件上传）。"""

    def __init__(self, *, bot):
        super().__init__(title="提交议诉申请", timeout=300)
        self.bot = bot

        self.defendant_id = discord.ui.TextInput(
            label="被投诉人 ID 或 @提及",
            placeholder="例如：123456789012345678 或 @某人",
            style=discord.TextStyle.short,
            max_length=120,
            required=True,
        )
        self.visibility = discord.ui.TextInput(
            label="议诉模式（私密/公开）",
            placeholder="填写：私密 或 公开；留空默认私密",
            style=discord.TextStyle.short,
            max_length=10,
            required=False,
        )
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
        self.evidence_link = discord.ui.TextInput(
            label="证据链接（可选；附件请用 /议诉 申请）",
            placeholder="可填写消息链接、图床/网盘链接等；需要上传文件请改用 /议诉 申请",
            style=discord.TextStyle.short,
            max_length=1000,
            required=False,
        )

        self.add_item(self.defendant_id)
        self.add_item(self.visibility)
        self.add_item(self.rule_text)
        self.add_item(self.description)
        self.add_item(self.evidence_link)

    @staticmethod
    def _extract_user_id(raw: str) -> int | None:
        digits = "".join(ch for ch in (raw or "") if ch.isdigit())
        if not digits:
            return None
        try:
            return int(digits)
        except ValueError:
            return None

    @staticmethod
    def _parse_visibility(raw: str | None) -> str:
        text = (raw or "").strip().lower()
        if text in ("公开", "public", "公", "open"):
            return VIS_PUBLIC
        return VIS_PRIVATE

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return

        user_id = self._extract_user_id(str(self.defendant_id.value))
        if user_id is None:
            await interaction.response.send_message("无法识别被投诉人，请填写用户 ID 或 @提及。", ephemeral=True)
            return

        try:
            defendant = interaction.guild.get_member(user_id) or await interaction.guild.fetch_member(user_id)
        except Exception:
            await interaction.response.send_message("无法在本服务器找到该被投诉人，请确认 ID/@提及正确。", ephemeral=True)
            return

        await submit_case_application(
            bot=self.bot,
            interaction=interaction,
            defendant=defendant,
            requested_visibility=self._parse_visibility(str(self.visibility.value)),
            rule_text=str(self.rule_text.value).strip(),
            description=str(self.description.value).strip(),
            evidence_link=str(self.evidence_link.value).strip(),
            evidence_attachments=[],
        )


class EntryView(discord.ui.View):
    """议诉区入口持久按钮。"""

    def __init__(self, *, bot):
        super().__init__(timeout=None)
        self.bot = bot

        self.btn_apply = discord.ui.Button(
            label="提交议诉申请",
            style=discord.ButtonStyle.primary,
            custom_id="court_entry_apply",
        )
        self.btn_apply.callback = self._on_apply
        self.add_item(self.btn_apply)

    async def _on_apply(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return

        settings = await self.bot.get_settings(interaction.guild.id)
        if not settings or not settings.get("review_channel_id"):
            await interaction.response.send_message(
                "本服务器尚未配置议诉系统，请先由管理执行：/议诉 设置",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(EntryApplyModal(bot=self.bot))
