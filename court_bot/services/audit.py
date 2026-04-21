from __future__ import annotations

import discord

from ..constants import COLOR_GRAY


async def send_audit_log(
    *,
    bot: discord.Client,
    audit_channel_id: int | None,
    title: str,
    description: str,
    case_id: int | None = None,
    operator: discord.abc.User | None = None,
) -> None:
    """可选：向审计频道发送日志。"""

    if not audit_channel_id:
        return

    channel = bot.get_channel(audit_channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(audit_channel_id)
        except Exception:
            return

    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return

    embed = discord.Embed(title=title, description=description, color=COLOR_GRAY)
    if case_id is not None:
        embed.add_field(name="案件", value=f"#{case_id}", inline=True)
    if operator is not None:
        embed.add_field(name="操作者", value=f"{operator} ({operator.id})", inline=False)

    try:
        await channel.send(embed=embed)
    except Exception:
        pass
