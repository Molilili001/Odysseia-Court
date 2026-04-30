from __future__ import annotations

from typing import Iterable

import discord

from .constants import (
    COLOR_BLUE,
    COLOR_GRAY,
    COLOR_GREEN,
    COLOR_ORANGE,
    COLOR_RED,
    COLOR_YELLOW,
    SIDE_COMPLAINANT,
    SIDE_DEFENDANT,
    STATUS_AWAITING_CONTINUE,
    STATUS_AWAITING_JUDGEMENT,
    STATUS_CLOSED,
    STATUS_IN_SESSION,
    STATUS_NEEDS_MORE_EVIDENCE,
    STATUS_REJECTED,
    STATUS_UNDER_REVIEW,
    STATUS_WITHDRAWN,
    VIS_PRIVATE,
    VIS_PUBLIC,
    TURN_MESSAGE_LIMIT,
    TURN_SPEAK_MINUTES,
    round_label,
    side_label,
)


def _truncate(text: str, limit: int) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def visibility_label(vis: str) -> str:
    if vis == VIS_PRIVATE:
        return "私密"
    if vis == VIS_PUBLIC:
        return "公开"
    return vis


def status_label(status: str) -> str:
    return {
        STATUS_UNDER_REVIEW: "待审核",
        STATUS_NEEDS_MORE_EVIDENCE: "待补充材料",
        STATUS_REJECTED: "已驳回",
        STATUS_IN_SESSION: "议诉中",
        STATUS_AWAITING_CONTINUE: "待决定是否继续议诉",
        STATUS_AWAITING_JUDGEMENT: "待裁决",
        STATUS_CLOSED: "已结案",
        STATUS_WITHDRAWN: "已撤诉",
    }.get(status, status)


def build_case_review_embed(case: dict, evidences: Iterable[dict]) -> discord.Embed:
    """管理审核频道使用的议诉卡片。"""

    embed = discord.Embed(
        title=f"议诉 #{case['id']}（{status_label(case['status'])}）",
        color=COLOR_YELLOW,
    )

    embed.add_field(name="状态", value=status_label(case.get("status", "")), inline=True)

    embed.add_field(
        name="投诉人",
        value=f"<@{case['complainant_id']}> (`{case['complainant_id']}`)",
        inline=False,
    )
    embed.add_field(
        name="被投诉人",
        value=f"<@{case['defendant_id']}> (`{case['defendant_id']}`)",
        inline=False,
    )

    embed.add_field(
        name="申请议诉模式",
        value=visibility_label(case.get("requested_visibility") or ""),
        inline=True,
    )
    if case.get("approved_visibility"):
        embed.add_field(
            name="最终议诉模式",
            value=visibility_label(case.get("approved_visibility") or ""),
            inline=True,
        )

    # 若已创建议诉频道，则提供链接指路
    space_id = case.get("court_thread_id") or case.get("court_channel_id")
    if space_id:
        embed.add_field(name="议诉频道", value=f"<#{int(space_id)}> ", inline=False)

    embed.add_field(
        name="违反规则",
        value=_truncate(case.get("rule_text", ""), 1024) or "（空）",
        inline=False,
    )

    desc = case.get("description", "")
    embed.add_field(name="申请说明", value=_truncate(desc, 1024) or "（空）", inline=False)

    # 证据
    lines: list[str] = []
    for ev in evidences:
        ev_type = ev.get("type")
        label = (ev.get("label") or "证据").strip()
        url = (ev.get("url") or "").strip()
        note = (ev.get("note") or "").strip()
        provider_id = ev.get("provider_id")

        if ev_type == "attachment":
            icon = "📎"
        elif ev_type == "link":
            icon = "🔗"
        else:
            icon = "🧾"

        if ev_type == "attachment" and url:
            main = f"{icon} **{label}**：{url}"
        elif ev_type == "link" and url:
            main = f"{icon} **{label}**：{url}"
        elif url:
            main = f"{icon} **{label}**：{url}"
        else:
            main = f"{icon} **{label}**"

        extras: list[str] = []
        if provider_id:
            extras.append(f"提供者：<@{int(provider_id)}>")
        if note:
            extras.append(f"说明：{note}")
        if extras:
            main += "（" + "；".join(extras) + "）"
        lines.append(f"- {main}")

    evidence_text = "\n".join(lines) if lines else "（无）"
    embed.add_field(name="证据（链接/附件）", value=_truncate(evidence_text, 1024), inline=False)

    if case.get("status_reason"):
        embed.add_field(name="备注/原因", value=_truncate(case["status_reason"], 1024), inline=False)

    return embed


def build_opening_post_embed(case: dict, evidences: Iterable[dict]) -> discord.Embed:
    """议诉频道首楼：申请说明 + 初始证据。"""

    embed = discord.Embed(
        title=f"📌 议诉 #{case['id']}｜申请说明与证据",
        color=COLOR_YELLOW,
    )

    embed.add_field(name="投诉人", value=f"<@{case['complainant_id']}>", inline=True)
    embed.add_field(name="被投诉人", value=f"<@{case['defendant_id']}>", inline=True)
    embed.add_field(
        name="议诉模式",
        value=visibility_label(case.get("approved_visibility") or case.get("requested_visibility") or ""),
        inline=True,
    )

    embed.add_field(
        name="违反规则",
        value=_truncate((case.get("rule_text") or "").strip(), 1024) or "（空）",
        inline=False,
    )

    desc = (case.get("description") or "").strip()
    embed.add_field(name="申请说明", value=_truncate(desc, 1024) or "（空）", inline=False)

    # 证据
    lines: list[str] = []
    for ev in evidences:
        ev_type = ev.get("type")
        label = (ev.get("label") or "证据").strip()
        url = (ev.get("url") or "").strip()
        note = (ev.get("note") or "").strip()
        provider_id = ev.get("provider_id")

        if ev_type == "attachment":
            icon = "📎"
        elif ev_type == "link":
            icon = "🔗"
        else:
            icon = "🧾"

        main = f"{icon} **{label}**：{url}" if url else f"{icon} **{label}**"
        extras: list[str] = []
        if provider_id:
            extras.append(f"提供者：<@{int(provider_id)}> ")
        if note:
            extras.append(f"说明：{note}")
        if extras:
            main += "（" + "；".join(extras) + "）"
        lines.append(f"- {main}")

    evidence_text = "\n".join(lines) if lines else "（无）"
    embed.add_field(name="证据（链接/附件）", value=_truncate(evidence_text, 1024), inline=False)

    embed.add_field(
        name="流程说明",
        value=(
            f"三轮回合制；双方默认禁言。轮到你时点击『获取本轮发言权』，"
            f"在 {TURN_SPEAK_MINUTES} 分钟内最多发送 {TURN_MESSAGE_LIMIT} 条消息（可含图片/文件）。"
        ),
        inline=False,
    )

    embed.timestamp = discord.utils.utcnow()
    return embed


def build_court_panel_embed(case: dict) -> discord.Embed:
    color = COLOR_ORANGE if case.get("status") == STATUS_IN_SESSION else COLOR_GRAY

    embed = discord.Embed(
        title=f"议诉 #{case['id']}｜议诉控制面板",
        color=color,
    )

    embed.add_field(name="状态", value=status_label(case.get("status", "")), inline=True)
    embed.add_field(
        name="议诉模式",
        value=visibility_label(case.get("approved_visibility") or case.get("requested_visibility") or ""),
        inline=True,
    )

    if case.get("status_reason"):
        embed.add_field(name="结果/原因", value=_truncate(str(case["status_reason"]), 1024), inline=False)

    status = case.get("status")
    r = int(case.get("current_round") or 1)
    side = case.get("current_side")

    if status == STATUS_IN_SESSION:
        if r <= 3:
            embed.add_field(name="当前轮次", value=f"第 {r} / 3 轮（{round_label(r)}）", inline=False)
        else:
            embed.add_field(name="当前轮次", value=f"第 {r} 轮（{round_label(r)}）", inline=False)
        if side == SIDE_COMPLAINANT:
            who = f"<@{case['complainant_id']}>"
        elif side == SIDE_DEFENDANT:
            who = f"<@{case['defendant_id']}>"
        else:
            who = "（未知）"
        embed.add_field(name="当前应发言方", value=who, inline=False)

    if status == STATUS_AWAITING_CONTINUE:
        prev_round = max(1, r - 1)
        embed.add_field(
            name="当前进度",
            value=f"已完成第 {prev_round} 轮，等待双方选择是否继续进入第 {r} 轮。",
            inline=False,
        )

    embed.add_field(
        name="说明",
        value=(
            f"- 双方默认禁言；轮到你时点击『获取本轮发言权』，"
            f"在 {TURN_SPEAK_MINUTES} 分钟内最多发送 {TURN_MESSAGE_LIMIT} 条消息（可含图片/文件）。\n"
            "- 发言完毕点击『结束本轮发言』，或等待超时/条数上限自动结束。\n"
            "- 管理可在需要时强制结束/推进、处理补证等。"
        ),
        inline=False,
    )

    return embed


def build_statement_embed(
    *,
    case_id: int,
    side: str,
    round_number: int,
    author: discord.abc.User,
    content: str,
) -> discord.Embed:
    if side == SIDE_COMPLAINANT:
        color = COLOR_BLUE
        who = "投诉人"
        badge = "🟦"
    elif side == SIDE_DEFENDANT:
        color = COLOR_RED
        who = "被投诉人"
        badge = "🟥"
    else:
        color = COLOR_GRAY
        who = side
        badge = "⬜"

    embed = discord.Embed(
        title=f"议诉 #{case_id}｜{badge}{who}｜第 {round_number} 轮（{round_label(round_number)}）",
        description=(
            ("**陈述内容：**\n\n" + _truncate((content or "").strip(), 3800)).strip()
            or "（空）"
        ),
        color=color,
    )

    try:
        embed.set_author(name=str(author), icon_url=getattr(author.display_avatar, "url", discord.Embed.Empty))
        embed.set_thumbnail(url=getattr(author.display_avatar, "url", discord.Embed.Empty))
    except Exception:
        embed.set_author(name=str(author))

    embed.add_field(name="发言者", value=getattr(author, "mention", str(author)), inline=True)
    embed.add_field(name="身份", value=who, inline=True)
    embed.add_field(name="轮次", value=f"第 {round_number} 轮", inline=True)

    embed.timestamp = discord.utils.utcnow()
    return embed


def build_judgement_result_embed(case: dict, decision: str, penalty: str, reason: str | None = None) -> discord.Embed:
    # decision: 成立 / 不成立 / 证据不足 等
    if decision in ("成立", "投诉成立"):
        color = COLOR_ORANGE
    elif decision in ("不成立", "投诉不成立"):
        color = COLOR_GREEN
    else:
        color = COLOR_GRAY

    embed = discord.Embed(
        title=f"⚖️ 议诉 #{case['id']}｜裁决结果",
        description=f"**裁决：{decision}**",
        color=color,
    )
    embed.add_field(name="处罚/处置", value=penalty or "无", inline=False)
    if reason:
        embed.add_field(name="说明", value=_truncate(reason, 1024), inline=False)
    embed.add_field(name="投诉人", value=f"<@{case['complainant_id']}>", inline=True)
    embed.add_field(name="被投诉人", value=f"<@{case['defendant_id']}>", inline=True)
    return embed


def build_continue_panel_embed(case: dict, state: dict | None) -> discord.Embed:
    """三辩结束后，双方决定是否继续议诉的面板。"""

    embed = discord.Embed(
        title=f"议诉 #{case['id']}｜是否继续议诉？",
        color=COLOR_YELLOW,
    )

    r = int(case.get("current_round") or 1)
    side = case.get("current_side")
    if side == SIDE_COMPLAINANT:
        next_who = f"<@{case['complainant_id']}>"
    elif side == SIDE_DEFENDANT:
        next_who = f"<@{case['defendant_id']}>"
    else:
        next_who = "（未知）"

    embed.description = "双方都选择『希望继续议诉』才会进入下一轮；任意一方选择『希望结束议诉』将进入裁决。"
    embed.add_field(name="若继续", value=f"将进入第 {r} 轮（{round_label(r)}），由 {next_who} 先发言。", inline=False)

    def fmt(choice: str | None) -> str:
        if choice == "continue":
            return "继续"
        if choice == "end":
            return "结束"
        return "未选择"

    c_choice = state.get("complainant_choice") if state else None
    d_choice = state.get("defendant_choice") if state else None

    embed.add_field(name="投诉人", value=fmt(c_choice), inline=True)
    embed.add_field(name="被投诉人", value=fmt(d_choice), inline=True)
    embed.timestamp = discord.utils.utcnow()
    return embed
