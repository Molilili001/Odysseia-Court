from __future__ import annotations

import discord

from .time_utils import format_time_pair


def review_embed(app: dict, review: dict, counts: dict[str, int]) -> discord.Embed:
    snapshot = review["snapshot"]
    labels = {"review_area_pending": "等待创建审核区", "reviewing": "审核中",
              "review_timeout": "审核超时", "review_publish_pending": "审核通过，等待发布",
              "review_rejected": "审核拒绝", "withdrawn": "已撤回", "voting": "审核通过，公众投票中"}
    status = labels.get(app["status"], app["status"])
    embed = discord.Embed(title=f"常态申请前置审核 #{app['id']}", color=0x57F287 if review["outcome"] == "yes" else 0xED4245 if review["outcome"] == "no" else 0x5865F2)
    embed.add_field(name="申请人", value=f"{app['display_name']}（<@{app['user_id']}>）", inline=False)
    embed.add_field(name="用户 ID", value=str(app["user_id"]), inline=True)
    embed.add_field(name="申请岗位", value=app["field_name"][:100], inline=True)
    embed.add_field(name="原始报名理由/宣言", value=app["self_intro"][:1024], inline=False)
    embed.add_field(name="当前审核状态", value=status, inline=False)
    embed.add_field(name="同意票 / 支持阈值", value=f"{counts['yes']} / {snapshot['approve_threshold']}", inline=True)
    embed.add_field(name="拒绝票 / 反对阈值", value=f"{counts['no']} / {snapshot['reject_threshold']}", inline=True)
    for name, key in (("提交时间", "submitted_at"), ("审核开始时间", "review_started_at"),
                      ("审核截止时间", "review_deadline_at")):
        embed.add_field(name=name, value=format_time_pair(app[key] if key == "submitted_at" else review[key]), inline=False)
    if review.get("dm_status") == "failed":
        embed.add_field(name="私信状态", value=f"发送失败：{str(review.get('dm_error') or '')[:200]}", inline=False)
    if review.get("archive_error"):
        embed.add_field(name="归档状态", value=f"等待自动重试：{str(review['archive_error'])[:200]}", inline=False)
    embed.set_footer(text=f"Continuous Review Application ID: {app['id']}")
    return embed
