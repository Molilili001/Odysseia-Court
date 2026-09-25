from __future__ import annotations

import discord


def entry_embed(settings: dict) -> discord.Embed:
    prerequisite = settings.get("prerequisite_role_id")
    inspector = settings.get("inspector_role_id")
    prerequisite_text = f"<@&{prerequisite}>" if prerequisite else "尚未配置"
    inspector_text = f"<@&{inspector}>" if inspector else "尚未配置"
    embed = discord.Embed(
        title="【监察组正式成员申请】",
        description=(
            f"• **申请岗位：** {inspector_text}（审核通过后授予）\n"
            f"• **申请资格：** 须持有 {prerequisite_text}\n"
            "• **审核方式：** 由现任监察组成员投票审核\n"
            "• **申请理由：** 最多 400 字，提交后不可修改\n\n"
            "点击下方按钮提交申请、查看申请状态或撤回审核中的申请。"
        ),
        color=0x5865F2,
    )
    embed.set_footer(text="申请资格以提交时的设置为准 · 审核结果将通过私信通知")
    return embed


def review_embed(app: dict, votes: list[dict], counts: dict[str, int]) -> discord.Embed:
    snapshot = app["snapshot"]
    status = {"reviewing": "审核中", "approved": "已通过", "rejected": "已拒绝", "withdrawn": "申请人已撤回"}.get(app["status"], app["status"])
    embed = discord.Embed(title=f"监察组申请 #{app['id']}", color=0x57F287 if status == "已通过" else 0xED4245 if status == "已拒绝" else 0x5865F2)
    embed.add_field(name="申请人", value=f"{app['display_name']}（<@{app['user_id']}>）", inline=False)
    embed.add_field(name="用户 ID", value=str(app["user_id"]), inline=True)
    embed.add_field(name="状态", value=status, inline=True)
    embed.add_field(name="申请理由", value=app["reason"][:1024], inline=False)
    embed.add_field(name="票数", value=f"同意 {counts['同意']}/{snapshot['approve_threshold']}｜拒绝 {counts['拒绝']}/{snapshot['reject_threshold']}", inline=False)
    opinions = []
    for vote in votes:
        line = f"{vote['reviewer_name']}：{vote['choice']}"
        if vote["reason"]:
            line += f"\n理由：{vote['reason']}"
        opinions.append(line)
    embed.add_field(name="当前有效审核意见", value="\n\n".join(opinions)[:1024] or "暂无", inline=False)
    if app["status"] == "approved":
        role_status = {"success": "✅ 身份组发放成功", "failed": "⚠️ 身份组发放失败，等待自动重试"}.get(app["role_grant_status"], "⏳ 等待发放身份组")
        embed.add_field(name="身份组发放", value=role_status, inline=False)
    if app["dm_status"] == "failed":
        embed.add_field(name="私信", value=f"发送失败：{str(app.get('dm_error') or '')[:200]}", inline=False)
    embed.set_footer(text=f"Inspection Member Application ID: {app['id']}")
    return embed
