from __future__ import annotations

from typing import Any

import discord

from .constants import COLOR_BLUE, COLOR_GOLD, COLOR_GRAY, COLOR_GREEN, COLOR_ORANGE, COLOR_RED, COLOR_YELLOW
from .continuous_constants import (
    CONT_APP_APPROVED,
    CONT_APP_APPROVED_WITHDRAWN,
    CONT_APP_CANCELLED,
    CONT_APP_REJECTED,
    CONT_APP_VOTING,
    CONT_APP_WITHDRAWN,
    CONT_APPLICATION_STATUS_LABELS,
    CONT_MODE_APPROVAL,
    CONT_MODE_LABELS,
    CONT_MODE_SUPPORT,
    CONT_VOTE_LABELS,
    CONT_VOTE_SUPPORT,
)
from .continuous_database import ContinuousApplicationRepo
from .embeds import format_role_mentions
from .text_utils import sanitize_public_text, split_lines_for_embed
from .time_utils import format_time_pair, human_duration


def _format_percent(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if number.is_integer():
        return f"{int(number)}%"
    return f"{number:.2f}%"


def _format_user(app: dict[str, Any]) -> str:
    user_id = int(app.get("user_id") or 0)
    return f"<@{user_id}>" if user_id else "未知用户"


def _config_mode(config: dict[str, Any]) -> str:
    return str(config.get("mode") or CONT_MODE_APPROVAL)


def _result_mode(result: dict[str, Any] | None, config: dict[str, Any]) -> str:
    return str((result or {}).get("mode") or _config_mode(config))


def build_continuous_entry_embed(config: dict[str, Any], fields: list[dict[str, Any]]) -> discord.Embed:
    application_roles = ContinuousApplicationRepo.decode_role_ids(config.get("allowed_application_role_ids"))
    voter_roles = ContinuousApplicationRepo.decode_role_ids(config.get("allowed_voter_role_ids"))
    field_lines = [f"{idx}. {sanitize_public_text(field.get('name'), max_len=80)}" for idx, field in enumerate(fields, start=1)]
    embed = discord.Embed(
        title=f"常态申请入口｜{sanitize_public_text(config.get('name'), max_len=120)}",
        color=COLOR_BLUE,
        description="点击下方按钮提交申请、查看状态或退出申请/通过名单。",
    )
    embed.add_field(name="可申请岗位", value=("\n".join(field_lines) or "未配置")[:1024], inline=False)
    embed.add_field(name="申请资格", value=format_role_mentions(application_roles, action="申请")[:1024], inline=False)
    mode = _config_mode(config)
    embed.add_field(name="模式", value=CONT_MODE_LABELS.get(mode, mode), inline=True)
    embed.add_field(name="投票资格" if mode == CONT_MODE_APPROVAL else "支持资格", value=format_role_mentions(voter_roles, action="投票" if mode == CONT_MODE_APPROVAL else "支持")[:1024], inline=False)
    if mode == CONT_MODE_SUPPORT:
        embed.add_field(name="通过规则", value=f"支持票达到 {int(config.get('support_target_votes') or 0)} 票即通过；到期未达标则未通过。", inline=False)
    else:
        embed.add_field(name="通过规则", value=f"总票数至少 {int(config.get('min_total_votes') or 0)} 票；同意比例不少于 {_format_percent(config.get('approval_threshold_percent'))}", inline=False)
    embed.add_field(name="投票时长", value=human_duration(int(config.get("voting_duration_minutes") or 0)), inline=True)
    embed.add_field(name="冷却期", value=human_duration(int(config.get("cooldown_minutes") or 0)), inline=True)
    embed.set_footer(text=f"Continuous Config ID: {config['id']}")
    return embed


def build_continuous_application_embed(
    config: dict[str, Any],
    application: dict[str, Any],
    *,
    result: dict[str, Any] | None = None,
) -> discord.Embed:
    status = str(application.get("status") or CONT_APP_VOTING)
    if status == CONT_APP_APPROVED:
        color = COLOR_GREEN
    elif status == CONT_APP_REJECTED:
        color = COLOR_RED
    elif status in (CONT_APP_WITHDRAWN, CONT_APP_APPROVED_WITHDRAWN, CONT_APP_CANCELLED):
        color = COLOR_GRAY
    else:
        color = COLOR_ORANGE
    mode = _config_mode(config)
    title = f"常态申请{'投票' if mode == CONT_MODE_APPROVAL else '支持收集'}｜{sanitize_public_text(config.get('name'), max_len=120)}"
    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="申请人", value=_format_user(application), inline=False)
    embed.add_field(name="申请岗位", value=sanitize_public_text(application.get("field_name"), max_len=80), inline=True)
    embed.add_field(name="当前状态", value=CONT_APPLICATION_STATUS_LABELS.get(status, status), inline=True)
    embed.add_field(name="投票结束", value=format_time_pair(application.get("voting_end_at")), inline=False)
    intro = sanitize_public_text(application.get("self_intro"), max_len=1000, fallback="未填写")
    embed.add_field(name="报名宣言", value=intro or "未填写", inline=False)
    if result:
        result_mode = _result_mode(result, config)
        passed = bool(result.get("passed"))
        embed.add_field(
            name="最终结果",
            value="通过" if passed else "未通过",
            inline=True,
        )
        if result_mode == CONT_MODE_SUPPORT and passed:
            embed.add_field(name="支持票", value=str(int(result.get("support_votes") or 0)), inline=True)
            embed.add_field(name="目标票数", value=str(int(result.get("support_target_votes") or 0)), inline=True)
        elif result_mode != CONT_MODE_SUPPORT:
            ratio = _format_percent(result.get("approval_ratio_percent"))
            embed.add_field(name="总票数", value=str(int(result.get("total_votes") or 0)), inline=True)
            embed.add_field(name="同意比例", value=ratio, inline=True)
            embed.add_field(
                name="票数",
                value=f"同意：{int(result.get('yes_votes') or 0)}\n反对：{int(result.get('no_votes') or 0)}",
                inline=True,
            )
            embed.add_field(
                name="通过门槛",
                value=f"最低 {int(result.get('min_total_votes') or 0)} 票；同意比例 {_format_percent(result.get('approval_threshold_percent'))}",
                inline=True,
            )
    reason = str(application.get("status_reason") or "").strip()
    if reason:
        embed.add_field(name="状态说明", value=sanitize_public_text(reason, max_len=1000), inline=False)
    embed.set_footer(text=f"Continuous Config ID: {config['id']}｜Application ID: {application['id']}｜投票期间不显示实时票数")
    return embed


def build_continuous_public_event_embed(config: dict[str, Any], application: dict[str, Any], *, event: str, result: dict[str, Any] | None = None) -> discord.Embed:
    status = str(application.get("status") or "")
    color = {
        CONT_APP_APPROVED: COLOR_GREEN,
        CONT_APP_REJECTED: COLOR_RED,
        CONT_APP_WITHDRAWN: COLOR_YELLOW,
        CONT_APP_APPROVED_WITHDRAWN: COLOR_GRAY,
        CONT_APP_CANCELLED: COLOR_GRAY,
    }.get(status, COLOR_GOLD)
    embed = discord.Embed(
        title=f"常态申请公示｜{sanitize_public_text(config.get('name'), max_len=120)}",
        color=color,
        description=event,
    )
    embed.add_field(name="成员", value=f"<@{int(application.get('user_id') or 0)}>", inline=True)
    embed.add_field(name="岗位", value=sanitize_public_text(application.get("field_name"), max_len=80), inline=True)
    embed.add_field(name="状态", value=CONT_APPLICATION_STATUS_LABELS.get(status, status), inline=True)
    if result:
        if _result_mode(result, config) == CONT_MODE_SUPPORT:
            if result.get("passed"):
                embed.add_field(
                    name="结果统计",
                    value=f"支持票：{int(result.get('support_votes') or 0)}\n目标票数：{int(result.get('support_target_votes') or 0)}",
                    inline=False,
                )
        else:
            embed.add_field(
                name="结果统计",
                value=(
                    f"总票数：{int(result.get('total_votes') or 0)}\n"
                    f"同意：{int(result.get('yes_votes') or 0)}\n"
                    f"反对：{int(result.get('no_votes') or 0)}\n"
                    f"同意比例：{_format_percent(result.get('approval_ratio_percent'))}"
                ),
                inline=False,
            )
    cooldown_until = application.get("cooldown_until")
    if cooldown_until:
        embed.add_field(name="冷却结束", value=format_time_pair(cooldown_until), inline=False)
    embed.set_footer(text=f"Continuous Config ID: {config['id']}｜Application ID: {application['id']}")
    return embed


def build_continuous_my_status_embed(
    config: dict[str, Any],
    application: dict[str, Any] | None,
    *,
    cooldown_until: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"我的常态申请｜{sanitize_public_text(config.get('name'), max_len=120)}",
        color=COLOR_BLUE,
    )
    if application is None:
        embed.description = "当前没有你的申请记录。"
    else:
        status = str(application.get("status") or "")
        embed.description = f"最近申请状态：{CONT_APPLICATION_STATUS_LABELS.get(status, status)}"
        embed.add_field(name="申请岗位", value=sanitize_public_text(application.get("field_name"), max_len=80), inline=True)
        embed.add_field(name="提交时间", value=format_time_pair(application.get("submitted_at")), inline=False)
        embed.add_field(name="投票结束", value=format_time_pair(application.get("voting_end_at")), inline=False)
        result = ContinuousApplicationRepo.decode_result(application.get("result_json"))
        if result:
            if _result_mode(result, config) == CONT_MODE_SUPPORT:
                if result.get("passed"):
                    embed.add_field(
                        name="结果统计",
                        value=f"支持票：{int(result.get('support_votes') or 0)}\n目标票数：{int(result.get('support_target_votes') or 0)}",
                        inline=False,
                    )
            else:
                embed.add_field(
                    name="结果统计",
                    value=(
                        f"总票数：{int(result.get('total_votes') or 0)}\n"
                        f"同意：{int(result.get('yes_votes') or 0)}\n"
                        f"反对：{int(result.get('no_votes') or 0)}\n"
                        f"同意比例：{_format_percent(result.get('approval_ratio_percent'))}"
                    ),
                    inline=False,
                )
    if cooldown_until:
        embed.add_field(name="冷却结束", value=format_time_pair(cooldown_until), inline=False)
    embed.set_footer(text=f"Continuous Config ID: {config['id']}")
    return embed


def build_continuous_vote_status_embed(config: dict[str, Any], application: dict[str, Any], vote_record: dict[str, Any] | None) -> discord.Embed:
    mode = _config_mode(config)
    embed = discord.Embed(
        title=f"我的{'投票' if mode == CONT_MODE_APPROVAL else '支持'}｜{sanitize_public_text(config.get('name'), max_len=120)}",
        color=COLOR_BLUE,
        description="以下内容仅你本人可见；收集期间不会公开实时票数。" if mode == CONT_MODE_SUPPORT else "以下内容仅你本人可见；投票期间不会公开实时票数。",
    )
    embed.add_field(name="申请人", value=f"<@{int(application.get('user_id') or 0)}>", inline=True)
    embed.add_field(name="申请岗位", value=sanitize_public_text(application.get("field_name"), max_len=80), inline=True)
    if vote_record:
        choice = str(vote_record.get("choice") or "")
        embed.add_field(name="你的选择", value="已支持" if choice == CONT_VOTE_SUPPORT else CONT_VOTE_LABELS.get(choice, choice), inline=True)
        embed.add_field(name="最后修改", value=format_time_pair(vote_record.get("updated_at")), inline=False)
    else:
        embed.add_field(name="你的选择", value="尚未支持" if mode == CONT_MODE_SUPPORT else "尚未投票", inline=True)
    embed.set_footer(text=f"Application ID: {application['id']}")
    return embed


def build_continuous_approved_list_embed(
    rows: list[dict[str, Any]],
    *,
    config: dict[str, Any] | None = None,
    field_name: str | None = None,
) -> discord.Embed:
    title = "常态申请通过名单"
    if config is not None:
        title += f"｜{sanitize_public_text(config.get('name'), max_len=80)}"
    if field_name:
        title += f"｜{sanitize_public_text(field_name, max_len=40)}"
    embed = discord.Embed(title=title, color=COLOR_GREEN)
    if not rows:
        embed.description = "当前没有符合条件的通过记录。"
        return embed
    lines = []
    for idx, row in enumerate(rows, start=1):
        config_name = sanitize_public_text(row.get("config_name") or (config or {}).get("name"), max_len=50, fallback="")
        prefix = f"{config_name}｜" if config is None and config_name else ""
        lines.append(
            f"{idx}. {prefix}{sanitize_public_text(row.get('field_name'), max_len=40)}｜"
            f"<@{int(row.get('user_id') or 0)}>｜通过时间：{format_time_pair(row.get('closed_at'))}"
        )
    chunks = split_lines_for_embed(lines, max_chars=3800)
    embed.description = chunks[0]
    if len(chunks) > 1:
        embed.set_footer(text="名单过长，后续内容已省略。")
    return embed


def build_continuous_supporter_list_embeds(config: dict[str, Any], application: dict[str, Any], supporters: list[dict[str, Any]]) -> list[discord.Embed]:
    lines = [f"{idx}. <@{int(row.get('voter_id') or 0)}>" for idx, row in enumerate(supporters, start=1)]
    chunks = split_lines_for_embed(lines, max_chars=3800) or ["无"]
    embeds: list[discord.Embed] = []
    for idx, chunk in enumerate(chunks, start=1):
        embed = discord.Embed(
            title=f"支持者名单｜{sanitize_public_text(config.get('name'), max_len=120)}",
            color=COLOR_GREEN,
            description=chunk,
        )
        embed.add_field(name="成员", value=f"<@{int(application.get('user_id') or 0)}>", inline=True)
        embed.add_field(name="岗位", value=sanitize_public_text(application.get("field_name"), max_len=80), inline=True)
        embed.set_footer(text=f"Application ID: {application['id']}｜共 {len(supporters)} 人｜第 {idx}/{len(chunks)} 段")
        embeds.append(embed)
    return embeds


def build_continuous_status_embed(configs: list[dict[str, Any]], counts: dict[int, tuple[int, int]]) -> discord.Embed:
    embed = discord.Embed(title="常态申请状态", color=COLOR_BLUE)
    if not configs:
        embed.description = "当前没有常态申请配置。"
        return embed
    lines = []
    for config in configs:
        open_count, approved_count = counts.get(int(config["id"]), (0, 0))
        lines.append(
            f"#{config['id']}｜{sanitize_public_text(config.get('name'), max_len=80)}｜"
            f"{CONT_MODE_LABELS.get(_config_mode(config), _config_mode(config))}｜"
            f"进行中 {open_count}｜已通过 {approved_count}｜"
            f"投票 {human_duration(int(config.get('voting_duration_minutes') or 0))}｜"
            f"冷却 {human_duration(int(config.get('cooldown_minutes') or 0))}"
        )
    embed.description = "\n".join(lines)[:4000]
    return embed
