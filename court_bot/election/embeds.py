from __future__ import annotations

from typing import Any

import discord

from .constants import (
    COLOR_BLUE,
    COLOR_GOLD,
    COLOR_GRAY,
    COLOR_GREEN,
    COLOR_ORANGE,
    COLOR_RED,
    COLOR_YELLOW,
    PUBLICITY_BATCH,
    PUBLICITY_LABELS,
    PUBLIC_SYNC_STATUS_LABELS,
    REGISTRATION_STATUS_LABELS,
    REG_ACTIVE,
    REG_COUNT_DISPLAY_DETAIL,
    REG_COUNT_DISPLAY_HIDDEN,
    REG_COUNT_DISPLAY_TOTAL,
    REG_REJECTED,
    REG_REVOKED,
    REG_WITHDRAWN,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_LABELS,
    STATUS_REGISTRATION,
    STATUS_REGISTRATION_ENDED,
    STATUS_SETUP,
    STATUS_VOTING,
)
from .database import ElectionRepo
from .text_utils import sanitize_public_text, split_lines_for_embed
from .time_utils import format_beijing, format_time_pair, human_duration


def _field_lines(fields: list[dict[str, Any]]) -> str:
    if not fields:
        return "未配置"
    lines = []
    for field in fields:
        lines.append(f"{int(field['sort_order'])}. {sanitize_public_text(field['name'], max_len=80)}：{int(field['winner_count'])} 人")
    return "\n".join(lines)


def build_registration_count_text(fields: list[dict[str, Any]], registrations: list[dict[str, Any]], *, mode: str) -> str | None:
    mode = str(mode or REG_COUNT_DISPLAY_HIDDEN)
    if mode not in (REG_COUNT_DISPLAY_TOTAL, REG_COUNT_DISPLAY_DETAIL):
        return None

    total = len(registrations)
    if mode == REG_COUNT_DISPLAY_TOTAL:
        return f"总人数：{total} 人"

    field_keys = [str(field["field_key"]) for field in fields]
    field_key_set = set(field_keys)
    field_counts = {field_key: 0 for field_key in field_keys}
    all_fields_count = 0

    for registration in registrations:
        selected_keys = set(ElectionRepo.decode_field_keys(registration.get("selected_field_keys"))) & field_key_set
        is_all_fields = bool(field_key_set) and field_key_set.issubset(selected_keys)
        if is_all_fields:
            all_fields_count += 1
        for field_key in ([] if is_all_fields else selected_keys):
            field_counts[field_key] = field_counts.get(field_key, 0) + 1

    lines = [f"总人数：{total} 人"]
    for field in fields:
        lines.append(f"{sanitize_public_text(field.get('name'), max_len=24)}：{field_counts.get(str(field['field_key']), 0)} 人")
    lines.append(f"全选（全部岗位）：{all_fields_count} 人")
    return "\n".join(lines)[:1024]


def _registration_entry_hint(status: str) -> str:
    if status == STATUS_SETUP:
        return "募选尚未开始，报名按钮暂不可用；开始后入口会自动刷新。"
    if status == STATUS_REGISTRATION:
        return "当前开放报名：可报名、查看、编辑或撤回自己的报名。"
    if status == STATUS_REGISTRATION_ENDED:
        return "报名已结束，不能再报名、编辑或撤回；可继续查看自己的报名，等待投票开始。"
    if status == STATUS_VOTING:
        return "投票正在进行：报名入口已关闭，请前往统一投票面板参与投票。"
    if status == STATUS_COMPLETED:
        return "本场募选已结束，报名和投票均已关闭；结果已发布或可由管理员查看。"
    if status == STATUS_CANCELLED:
        return "本场募选已取消，报名和投票均已关闭。"
    return "请根据当前状态使用下方按钮。"


def build_registration_entry_embed(election: dict[str, Any], fields: list[dict[str, Any]], *, registration_count_text: str | None = None) -> discord.Embed:
    status = str(election.get("status") or "")
    embed = discord.Embed(
        title=f"募选报名入口｜{sanitize_public_text(election['name'], max_len=120)}",
        color=COLOR_BLUE,
        description=_registration_entry_hint(status),
    )
    embed.add_field(name="当前状态", value=STATUS_LABELS.get(status, status), inline=True)
    embed.add_field(name="公示模式", value=PUBLICITY_LABELS.get(str(election.get("publicity_mode")), str(election.get("publicity_mode"))), inline=True)
    embed.add_field(name="每人最多可投", value=f"{int(election.get('vote_max_selections') or 0)} 人", inline=True)
    allowed_candidate_roles = ElectionRepo.decode_role_ids(election.get("allowed_candidate_role_ids"))
    embed.add_field(name="报名资格", value=format_role_mentions(allowed_candidate_roles, action="报名")[:1024], inline=False)
    embed.add_field(name="岗位/人数", value=_field_lines(fields)[:1024], inline=False)
    if registration_count_text:
        embed.add_field(name="当前报名人数", value=registration_count_text[:1024], inline=False)
    embed.add_field(name="报名开始", value=format_time_pair(election.get("registration_start_at")), inline=False)
    embed.add_field(name="报名结束", value=format_time_pair(election.get("registration_end_at")), inline=False)
    embed.add_field(name="投票开始", value=format_time_pair(election.get("voting_start_at")), inline=False)
    embed.add_field(name="投票结束", value=format_time_pair(election.get("voting_end_at")), inline=False)
    embed.set_footer(text=f"Election ID: {election['id']}｜时间均按北京时间展示")
    return embed


def format_role_mentions(role_ids: list[int], *, guild: discord.Guild | None = None, action: str = "投票", max_chars: int = 1024) -> str:
    if not role_ids:
        return f"所有服务器成员均可{action}"
    parts: list[str] = []
    for role_id in role_ids:
        label = f"<@&{int(role_id)}>"
        candidate = label if not parts else "、" + label
        if len("".join(parts)) + len(candidate) > max_chars - 1:
            parts.append("…")
            break
        parts.append(candidate)
    return f"拥有以下任意一个身份组即可{action}：" + "".join(parts)


def format_discord_username(value: object) -> str:
    username = sanitize_public_text(value, max_len=80, fallback="").strip()
    username = username.lstrip("@").replace("`", "ˋ").strip()
    return f"`{username}`" if username else "未知"


def format_candidate_vote_line(candidate: dict[str, Any], *, prefix: str = "") -> str:
    display_name = sanitize_public_text(candidate.get("display_name"), max_len=80)
    user_id = int(candidate.get("user_id") or 0)
    field_names = [sanitize_public_text(name, max_len=40, fallback="") for name in candidate.get("field_names") or []]
    field_text = "、".join(name for name in field_names if name) or "未选择岗位"
    return f"{prefix}{display_name}（用户ID：{user_id}）｜参选：{field_text}"


def format_public_candidate_vote_line(candidate: dict[str, Any], *, prefix: str = "") -> str:
    display_name = sanitize_public_text(candidate.get("display_name"), max_len=80)
    user_id = int(candidate.get("user_id") or 0)
    username = format_discord_username(candidate.get("username"))
    user_mention = f"<@{user_id}>" if user_id else "未知用户"
    field_names = [sanitize_public_text(name, max_len=40, fallback="") for name in candidate.get("field_names") or []]
    field_text = "、".join(name for name in field_names if name) or "未选择岗位"
    return f"{prefix}👤 {display_name}（🏷️ {username}｜🔗 {user_mention}）｜🗳️ 参选：{field_text}"


def build_vote_candidate_list_embeds(election: dict[str, Any], candidates: list[dict[str, Any]], *, page_size: int = 20) -> list[discord.Embed]:
    title = f"统一投票候选人名单｜{sanitize_public_text(election['name'], max_len=120)}"
    if not candidates:
        embed = discord.Embed(title=title, color=COLOR_RED, description="当前没有有效候选人。")
        embed.set_footer(text=f"Election ID: {election['id']}")
        return [embed]
    embeds: list[discord.Embed] = []
    total_pages = max(1, (len(candidates) + page_size - 1) // page_size)
    for page_index in range(total_pages):
        start = page_index * page_size
        page_candidates = candidates[start : start + page_size]
        lines = [format_public_candidate_vote_line(candidate, prefix=f"{start + idx}. ") for idx, candidate in enumerate(page_candidates, start=1)]
        embed = discord.Embed(
            title=title if page_index == 0 else title + f"（续 {page_index + 1}）",
            color=COLOR_RED,
            description="\n".join(lines)[:4000],
        )
        embed.set_footer(text=f"Election ID: {election['id']}｜候选人名单 {page_index + 1}/{total_pages}")
        embeds.append(embed)
    return embeds


def build_candidate_public_embed(
    election: dict[str, Any],
    registration: dict[str, Any],
    field_names: dict[str, str],
) -> discord.Embed:
    status = str(registration.get("status") or REG_ACTIVE)
    color = COLOR_GREEN if status == REG_ACTIVE else COLOR_GRAY
    if status == REG_REJECTED:
        color = COLOR_ORANGE
    elif status == REG_REVOKED:
        color = COLOR_RED
    elif status == REG_WITHDRAWN:
        color = COLOR_YELLOW

    selected_keys = ElectionRepo.decode_field_keys(registration.get("selected_field_keys"))
    selected_names = [field_names.get(key, key) for key in selected_keys]
    reason = ""
    if status == REG_REJECTED:
        reason = str(registration.get("rejected_reason") or "未填写")
    elif status == REG_REVOKED:
        reason = str(registration.get("revoked_reason") or "未填写")

    user_id = int(registration.get("user_id") or 0)
    display_name = sanitize_public_text(registration.get("display_name"), max_len=100)
    username = format_discord_username(registration.get("username"))
    user_mention = f"<@{user_id}>" if user_id else "未知用户"
    status_icon = {
        REG_ACTIVE: "✅",
        REG_WITHDRAWN: "↩️",
        REG_REJECTED: "⚠️",
        REG_REVOKED: "⛔",
    }.get(status, "📌")
    status_label = REGISTRATION_STATUS_LABELS.get(status, status)

    embed = discord.Embed(
        title=f"【候选人公示】｜{sanitize_public_text(election['name'], max_len=120)}",
        color=color,
    )
    embed.add_field(name="👤 候选人", value=display_name, inline=True)
    embed.add_field(name="🏷️ 用户名", value=username, inline=True)
    embed.add_field(name="🔗 提及", value=user_mention, inline=True)
    embed.add_field(name="📌 当前状态", value=f"{status_icon} {status_label}", inline=False)
    embed.add_field(name="🗳️ 参选岗位", value=sanitize_public_text("、".join(selected_names) or "未选择", max_len=1024), inline=False)
    intro = sanitize_public_text(registration.get("self_intro"), max_len=1000, fallback="未填写")
    embed.add_field(name="📝 参选宣言", value=intro or "未填写", inline=False)
    embed.add_field(name="🕒 报名时间", value=format_time_pair(registration.get("registered_at")), inline=False)
    embed.add_field(name="🔄 最后修改", value=format_time_pair(registration.get("last_modified_at")), inline=False)
    if reason:
        embed.add_field(name="📎 状态原因", value=sanitize_public_text(reason, max_len=1000), inline=False)
    embed.set_footer(text=f"Election ID: {election['id']}｜Registration ID: {registration['id']}")
    return embed


def build_status_embed(
    election: dict[str, Any],
    fields: list[dict[str, Any]],
    counts: dict[str, int],
    vote_count: int,
    *,
    is_admin_view: bool = False,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"募选状态｜{sanitize_public_text(election['name'], max_len=120)}",
        color=COLOR_BLUE,
    )
    embed.add_field(name="募选 ID", value=str(election["id"]), inline=True)
    embed.add_field(name="状态", value=STATUS_LABELS.get(str(election.get("status")), str(election.get("status"))), inline=True)
    embed.add_field(name="公示模式", value=PUBLICITY_LABELS.get(str(election.get("publicity_mode")), str(election.get("publicity_mode"))), inline=True)
    embed.add_field(name="岗位/人数", value=_field_lines(fields)[:1024], inline=False)
    embed.add_field(name="每人最多可投", value=f"{int(election.get('vote_max_selections') or 0)} 人", inline=True)
    allowed_candidate_roles = ElectionRepo.decode_role_ids(election.get("allowed_candidate_role_ids"))
    embed.add_field(name="报名资格", value=format_role_mentions(allowed_candidate_roles, action="报名")[:1024], inline=False)
    allowed_roles = ElectionRepo.decode_role_ids(election.get("allowed_voter_role_ids"))
    role_text = format_role_mentions(allowed_roles)
    embed.add_field(name="投票资格", value=role_text[:1024], inline=False)
    embed.add_field(
        name="阶段持续时间",
        value=(
            f"报名：{human_duration(int(election.get('registration_duration_minutes') or 0))}\n"
            f"公示/缓冲：{human_duration(int(election.get('publicity_duration_minutes') or 0))}\n"
            f"投票：{human_duration(int(election.get('voting_duration_minutes') or 0))}"
        ),
        inline=False,
    )
    embed.add_field(name="报名开始", value=format_time_pair(election.get("registration_start_at")), inline=False)
    embed.add_field(name="报名结束", value=format_time_pair(election.get("registration_end_at")), inline=False)
    embed.add_field(name="投票开始", value=format_time_pair(election.get("voting_start_at")), inline=False)
    embed.add_field(name="投票结束", value=format_time_pair(election.get("voting_end_at")), inline=False)

    if is_admin_view or election.get("publicity_mode") != PUBLICITY_BATCH or str(election.get("status")) != "registration":
        embed.add_field(
            name="报名统计",
            value=(
                f"有效：{counts.get('active', 0)}\n"
                f"撤回：{counts.get('withdrawn', 0)}\n"
                f"打回：{counts.get('rejected', 0)}\n"
                f"撤销：{counts.get('revoked', 0)}"
            ),
            inline=True,
        )
    else:
        embed.add_field(name="报名统计", value="非实时公示报名期内不公开报名统计。", inline=False)
    embed.add_field(name="投票人数", value=str(vote_count), inline=True)
    if election.get("publicity_mode") == PUBLICITY_BATCH:
        embed.add_field(name="统一公示状态", value=str(election.get("batch_publicity_status") or "pending"), inline=True)
    embed.set_footer(text="时间均按北京时间展示；管理员视图可能包含更多统计。")
    return embed


def build_vote_entry_embed(election: dict[str, Any], active_count: int, *, guild: discord.Guild | None = None) -> discord.Embed:
    allowed_roles = ElectionRepo.decode_role_ids(election.get("allowed_voter_role_ids"))
    role_text = format_role_mentions(allowed_roles, guild=guild)
    embed = discord.Embed(
        title=f"统一投票｜{sanitize_public_text(election['name'], max_len=120)}",
        color=COLOR_RED,
        description=(
            "下方会列出全部有效候选人。点击『开始投票』进入私密分页选择界面；投票提交后不能更改。"
            "点击『我的投票』可私密查看自己是否已投票及已提交的选择。"
        ),
    )
    embed.add_field(name="有效候选人数", value=str(active_count), inline=True)
    embed.add_field(name="每人最多可投", value=f"{int(election.get('vote_max_selections') or 0)} 人", inline=True)
    embed.add_field(name="投票资格", value=role_text, inline=False)
    embed.add_field(name="投票结束", value=format_time_pair(election.get("voting_end_at")), inline=False)
    embed.set_footer(text=f"Election ID: {election['id']}｜不会公开谁投给谁")
    return embed


def build_vote_page_embed(election: dict[str, Any], page: int, total_pages: int, selected_count: int, max_selectable: int, page_candidates: list[dict[str, Any]] | None = None) -> discord.Embed:
    embed = discord.Embed(
        title=f"投票选择｜{sanitize_public_text(election['name'], max_len=120)}",
        color=COLOR_RED,
        description="使用下方选择菜单选择本页候选人，可翻页累计选择。完成后点击『完成选择』。",
    )
    embed.add_field(name="当前页", value=f"{page + 1}/{total_pages}", inline=True)
    embed.add_field(name="已选择", value=f"{selected_count}/{max_selectable}", inline=True)
    if page_candidates is not None:
        lines = [format_candidate_vote_line(candidate, prefix=f"{idx}. ") for idx, candidate in enumerate(page_candidates, start=1)]
        embed.add_field(name="本页候选人", value=("\n".join(lines) or "（无）")[:1024], inline=False)
    embed.set_footer(text="投票提交后不能更改；不会公开谁投给谁。")
    return embed


def build_vote_confirm_embed(election: dict[str, Any], selected_regs: list[dict[str, Any]]) -> discord.Embed:
    lines = [f"- {sanitize_public_text(r.get('display_name'), max_len=80)}（用户ID：{int(r.get('user_id') or 0)}）" for r in selected_regs]
    embed = discord.Embed(
        title=f"确认投票｜{sanitize_public_text(election['name'], max_len=120)}",
        color=COLOR_YELLOW,
        description="请确认你的选择。提交后不能更改。",
    )
    chunks = split_lines_for_embed(lines, max_chars=3500)
    embed.add_field(name=f"已选择 {len(selected_regs)} 人", value=chunks[0], inline=False)
    return embed


def build_my_vote_status_embed(
    election: dict[str, Any],
    vote_record: dict[str, Any] | None,
    selected_candidates: list[dict[str, Any]],
    *,
    invalidation: dict[str, Any] | None = None,
    is_eligible: bool = True,
    eligibility_note: str | None = None,
) -> discord.Embed:
    """Build a private self-service view of one voter's submitted ballot."""

    election_status = str(election.get("status") or "")
    election_status_label = STATUS_LABELS.get(election_status, election_status or "未知")
    if invalidation is not None:
        color = COLOR_RED
        vote_status = "已被管理员作废"
        description = "你的投票记录已被管理员作废，不能重新投票。如有疑问请联系管理员。"
    elif vote_record is not None:
        color = COLOR_GREEN
        vote_status = "已提交"
        description = "你已经完成投票。以下内容仅你本人可见；投票提交后不能更改。"
    else:
        color = COLOR_GRAY
        vote_status = "尚未提交"
        if election_status == STATUS_VOTING:
            description = "你尚未提交本场募选投票。可回到投票面板点击『开始投票』。"
        else:
            description = "当前没有你的投票记录。"

    embed = discord.Embed(
        title=f"我的投票情况｜{sanitize_public_text(election['name'], max_len=120)}",
        color=color,
        description=description,
    )
    embed.add_field(name="募选 ID", value=str(election["id"]), inline=True)
    embed.add_field(name="当前阶段", value=election_status_label, inline=True)
    embed.add_field(name="投票状态", value=vote_status, inline=True)
    embed.add_field(name="投票结束", value=format_time_pair(election.get("voting_end_at")), inline=False)
    if vote_record is not None:
        embed.add_field(name="提交时间", value=format_time_pair(vote_record.get("created_at")), inline=False)
    if invalidation is not None:
        embed.add_field(name="作废时间", value=format_time_pair(invalidation.get("created_at")), inline=False)
        reason = sanitize_public_text(invalidation.get("reason"), max_len=1000, fallback="未填写")
        embed.add_field(name="作废原因", value=reason or "未填写", inline=False)
    if not is_eligible:
        embed.add_field(name="当前投票资格", value=(eligibility_note or "当前不具备本场募选投票资格。")[:1024], inline=False)

    if vote_record is not None:
        lines: list[str] = []
        for idx, candidate in enumerate(selected_candidates, start=1):
            user_id = int(candidate.get("user_id") or 0)
            if candidate.get("missing"):
                lines.append(f"{idx}. 未找到候选人记录（用户ID：{user_id}）")
                continue
            line = format_candidate_vote_line(candidate, prefix=f"{idx}. ")
            reg_status = str(candidate.get("status") or REG_ACTIVE)
            if reg_status != REG_ACTIVE:
                line += f"｜当前报名状态：{REGISTRATION_STATUS_LABELS.get(reg_status, reg_status)}"
            line = sanitize_public_text(line, max_len=700, fallback="")
            lines.append(line)
        chunks = split_lines_for_embed(lines, max_chars=900) if lines else []
        if not chunks:
            embed.add_field(name="已选择候选人", value="记录为空或候选人已不可见。", inline=False)
        else:
            remaining_slots = max(0, 25 - len(embed.fields))
            displayed_chunks = chunks[:remaining_slots]
            truncated = len(chunks) > len(displayed_chunks)
            for chunk_idx, chunk in enumerate(displayed_chunks, start=1):
                suffix = "" if chunk_idx == 1 else f"（续 {chunk_idx}）"
                value = chunk
                if truncated and chunk_idx == len(displayed_chunks):
                    value = f"{chunk}\n…后续内容已省略。"[:1024]
                embed.add_field(name=f"已选择候选人{suffix}", value=value, inline=False)

    embed.set_footer(text="仅你本人可见；不会公开谁投给谁。")
    return embed


def build_result_embeds(election: dict[str, Any], result: dict[str, Any]) -> list[discord.Embed]:
    title = f"🏆 {sanitize_public_text(election['name'], max_len=120)} - 募选结果"
    if result.get("is_void"):
        embed = discord.Embed(title=title, color=COLOR_GOLD, description=sanitize_public_text(result.get("void_reason"), max_len=3000))
        embed.set_footer(text=f"Election ID: {election['id']}｜时间：{format_beijing(result.get('calculated_at'))}")
        return [embed]

    embeds: list[discord.Embed] = []
    current = discord.Embed(
        title=title,
        color=COLOR_GOLD,
        description=(
            "各职位候选人得票情况如下：\n"
            f"总投票人数：{int(result.get('total_voters') or 0)}｜总票数：{int(result.get('total_votes') or 0)}｜"
            f"每人最多可投：{int(election.get('vote_max_selections') or 0)}"
        ),
    )
    embeds.append(current)

    def candidate_status(candidate: dict[str, Any], winners: list[dict[str, Any]], winner_count: int) -> str:
        uid = str(candidate.get("user_id"))
        field_key = str(candidate.get("field_key") or "")
        won_field_key = candidate.get("won_field_key")
        if won_field_key:
            if str(won_field_key) == field_key:
                return "✅ 确定当选"
            won_field_name = sanitize_public_text(candidate.get("won_field_name"), max_len=40, fallback="其它岗位")
            return f"🏅 已在「{won_field_name}」当选"
        if uid in {str(w.get("user_id")) for w in winners}:
            return "✅ 确定当选"
        rank = int(candidate.get("rank") or 0)
        if rank > 0 and rank <= winner_count + 3:
            return "🔄 递补顺位"
        return "❌ 确定落选"

    def candidate_line(candidate: dict[str, Any], winners: list[dict[str, Any]], winner_count: int) -> str:
        uid = int(candidate.get("user_id") or 0)
        display_name = sanitize_public_text(candidate.get("display_name"), max_len=80)
        votes = int(candidate.get("votes") or 0)
        status = candidate_status(candidate, winners, winner_count)
        note = ""
        won_field_key = str(candidate.get("won_field_key") or "")
        current_field_key = str(candidate.get("field_key") or "")
        if won_field_key and won_field_key != current_field_key:
            note = "（不占用本岗位名额）"
        return f"<@{uid}>\n{display_name} {votes}票 {status}{note}"

    def append_field(name: str, value: str) -> None:
        nonlocal current
        if len(current.fields) >= 20:
            current = discord.Embed(title=title + f"（续 {len(embeds) + 1}）", color=COLOR_GOLD)
            embeds.append(current)
        current.add_field(name=name[:256], value=value[:1024], inline=False)

    for field in result.get("fields", []):
        candidates = field.get("candidates") or []
        winners = field.get("winners") or []
        winner_count = int(field.get("winner_count") or 0)
        lines: list[str] = []
        if candidates:
            for candidate in candidates:
                lines.append(candidate_line(candidate, winners, winner_count))
        else:
            lines.append("（本岗位无候选人）")
        chunks = split_lines_for_embed(lines, max_chars=900)
        header = f"{sanitize_public_text(field.get('field_name'), max_len=80)}（募选{winner_count}人）"
        if int(field.get("vacancies") or 0):
            header += f"｜空缺 {int(field.get('vacancies') or 0)}"
        for chunk_idx, chunk in enumerate(chunks):
            append_field(header if chunk_idx == 0 else header + f"（续 {chunk_idx + 1}）", chunk)

    for embed in embeds:
        embed.set_footer(text=f"Election ID: {election['id']}｜同票按报名时间早者优先；已在前序岗位当选者不占后续岗位名额；递补仅表示顺位靠前")
    return embeds


def build_election_list_embed(rows: list[dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(title="募选列表", color=COLOR_BLUE)
    if not rows:
        embed.description = "当前没有可显示的募选。"
        return embed
    lines = []
    for row in rows:
        lines.append(
            f"#{row['id']}｜{sanitize_public_text(row.get('name'), max_len=80)}｜"
            f"{STATUS_LABELS.get(str(row.get('status')), row.get('status'))}｜"
            f"{PUBLICITY_LABELS.get(str(row.get('publicity_mode')), row.get('publicity_mode'))}｜"
            f"报名：{format_beijing(row.get('registration_start_at'))}"
        )
    embed.description = "\n".join(lines)[:4000]
    return embed


def build_help_embeds() -> list[discord.Embed]:
    """Detailed help pages for the /募选 command group.

    Keep each field comfortably below Discord's 1024-char field limit so the
    command can be sent as one ephemeral multi-embed response.
    """

    overview = discord.Embed(
        title="募选系统帮助｜1/4 总览与规则",
        color=COLOR_BLUE,
        description=(
            "这是独立的新募选模块：支持自定义岗位/人数、报名、公示、统一投票、计票、运维自检。"
            "所有时间按北京时间展示，数据库按 UTC 保存。"
        ),
    )
    overview.add_field(
        name="基本流程",
        value=(
            "1. 管理员 `/募选 创建` 建立募选。\n"
            "2. `/募选 设置入口` 发送或重发报名入口，成员用按钮报名/编辑/撤回。\n"
            "3. 报名结束后进入公示/缓冲；实时模式已逐个公示，统一模式会批量公示。\n"
            "4. 到投票时间后发布统一投票面板，成员私密分页选择候选人。\n"
            "5. 投票结束后自动或手动计票并发布结果。"
        ),
        inline=False,
    )
    overview.add_field(
        name="公示模式",
        value=(
            "`实时公示`：候选人报名或编辑后立即同步公示。\n"
            "`报名结束后统一公示`：报名期内不公开候选人；报名结束后统一发布。\n"
            "两种模式都遵守：**一个候选人 = 一条 Embed 公示消息**。"
        ),
        inline=False,
    )
    overview.add_field(
        name="身份组资格",
        value=(
            "报名身份组和投票身份组都是每场募选单独配置。\n"
            "未配置：所有服务器成员均可报名/投票。\n"
            "配置多个：拥有任意一个即可，**OR 逻辑，没有优先级**。"
        ),
        inline=False,
    )
    overview.add_field(
        name="计票规则",
        value=(
            "统一投票；每名投票者最多选择创建时配置的人数。\n"
            "岗位按创建时配置顺序依次结算；一个候选人先在靠前岗位当选后，不再占后续岗位名额。\n"
            "同票按报名时间早者优先；不自动补位；无人有效报名或无人投票则作废。"
        ),
        inline=False,
    )
    overview.add_field(
        name="权限与安全",
        value=(
            "未配置募选管理身份组时，管理命令使用 `Manage Guild` 或 `Administrator` 兜底；配置后允许 `Administrator` 或任一募选管理身份组。\n"
            "参选宣言禁止用户提及、身份组提及、`@everyone`、`@here`。\n"
            "投票不会公开“谁投给谁”；清除投票会禁止该成员重新投票。"
        ),
        inline=False,
    )
    overview.set_footer(text="命令参数名在 Discord 中显示为中文；如有多场未完成募选，请填写 募选id。")

    setup = discord.Embed(
        title="募选系统帮助｜2/4 创建与配置命令",
        color=COLOR_BLUE,
        description="以下命令主要给管理员使用，用于创建、配置、查看和管理候选人。",
    )
    setup.add_field(
        name="/募选 设置",
        value=(
            "用途：查看或更新募选模块管理身份组。\n"
            "未配置：管理命令使用 `Manage Guild` 或 `Administrator` 兜底。\n"
            "已配置：管理命令允许 `Administrator` 或任一募选管理身份组使用。\n"
            "填写 `清空`、`none`、`all` 可恢复原生权限兜底。"
        ),
        inline=False,
    )
    setup.add_field(
        name="/募选 创建",
        value=(
            "用途：创建一场募选。\n"
            "必填：名称、岗位配置、每人最多投票数、公示模式、报名/公示/投票持续时间、报名/投票/公示频道。\n"
            "可选：允许报名身份组、允许投票身份组、告警频道、报名开始时间、立即发送入口。\n"
            "岗位格式示例：`大当家:1,二当家:3,执行成员:9`。\n"
            "持续时间示例：`3天`、`72小时`、`2天6小时`、`30分钟`；不填开始时间即立即进入报名。"
        ),
        inline=False,
    )
    setup.add_field(
        name="/募选 设置入口",
        value=(
            "用途：发送或重发报名入口。\n"
            "参数：`募选id` 可选；`频道` 可选，不填则使用配置的报名频道。\n"
            "入口按钮：报名、我的报名、编辑报名、撤回报名。\n"
            "入口面板会在阶段变化、报名/投票身份组变更时自动刷新。"
        ),
        inline=False,
    )
    setup.add_field(
        name="/募选 刷新入口、/募选 刷新展示",
        value=(
            "`刷新入口`：原地编辑已发送的报名入口，不重发新消息。\n"
            "`刷新展示`：按范围原地刷新报名入口、公示和投票展示消息；适合代码更新后无缝套用新样式。\n"
            "范围：自动、全部、报名入口、公示、投票面板。"
        ),
        inline=False,
    )
    setup.add_field(
        name="/募选 常态 入口、刷新入口、刷新展示",
        value=(
            "`入口`：发送或重发长期申请入口。\n"
            "`刷新入口`：原地编辑已记录的常态申请入口，不重发新消息。\n"
            "`刷新展示`：按范围刷新常态入口和进行中的申请投票面板。\n"
            "范围：自动、全部、入口、投票面板。"
        ),
        inline=False,
    )
    setup.add_field(
        name="/募选 状态、/募选 列表",
        value=(
            "`/募选 状态`：查看阶段、岗位、报名资格、投票资格、时间、统计、公示状态。\n"
            "`/募选 列表`：列出当前服务器募选；`包含已完成=True` 时也显示已完成/已取消。"
        ),
        inline=False,
    )
    setup.add_field(
        name="/募选 报名身份组",
        value=(
            "用途：查看或更新允许报名身份组。\n"
            "不填 `身份组列表`：只查看当前设置。\n"
            "填写 ID/提及：更新为这些身份组，拥有任意一个即可报名。\n"
            "填写 `清空`、`无`、`不限`、`all`、`clear` 等：清空限制，所有成员可报名。\n"
            "限制：只能在未开始或报名阶段修改。"
        ),
        inline=False,
    )
    setup.add_field(
        name="/募选 投票身份组",
        value=(
            "用途：查看或更新允许投票身份组。用法与报名身份组类似。\n"
            "不填查看；填多个身份组则 OR；填 `清空/无/不限/all/clear` 清空。\n"
            "限制：投票开始后不能修改。"
        ),
        inline=False,
    )
    setup.add_field(
        name="/募选 候选人",
        value=(
            "用途：管理员查看或处理候选人。\n"
            "操作：`查看`、`打回`、`撤销`、`恢复`、`重发公示`。\n"
            "打回/撤销/恢复会写入审计日志；实时公示或已有公示消息会同步刷新。"
        ),
        inline=False,
    )

    voter = discord.Embed(
        title="募选系统帮助｜3/4 报名、投票、公示与结果",
        color=COLOR_ORANGE,
        description="以下命令覆盖普通成员自查、投票面板、公示修复和结果处理。",
    )
    voter.add_field(
        name="报名入口按钮",
        value=(
            "`报名`：选择参选岗位并填写参选宣言。\n"
            "`我的报名`：查看自己的报名状态、参选岗位、报名时间、公示状态。\n"
            "`编辑报名`：报名期内修改岗位/宣言，不刷新原报名时间。\n"
            "`撤回报名`：报名期内撤回；撤回后重新报名会刷新报名时间。"
        ),
        inline=False,
    )
    voter.add_field(
        name="/募选 我的报名、/募选 我的投票、/募选 帮助",
        value=(
            "`/募选 我的报名`：不用找入口消息，也能查看自己的报名。可填 `募选id`。\n"
            "`/募选 我的投票`：私密查看自己是否已投票、已提交的选择或作废状态。可填 `募选id`。\n"
            "`/募选 帮助`：显示当前帮助文档。"
        ),
        inline=False,
    )
    voter.add_field(
        name="/募选 同步公示",
        value=(
            "用途：修复或刷新候选人公示。\n"
            "范围：`失败项`、`全部已公示`、`全部有效候选人`。\n"
            "可选 `候选人`：只同步指定成员。\n"
            "统一公示模式若未完整成功，需在投票开始前修复，否则会按规则作废。"
        ),
        inline=False,
    )
    voter.add_field(
        name="/募选 开始投票",
        value=(
            "用途：应急手动开始投票。\n"
            "如果仍在报名阶段，会先尝试关闭报名；统一公示未完整成功时不能手动开始。\n"
            "投票面板会列出所有有效候选人；候选人过多时会拆分多条消息。"
        ),
        inline=False,
    )
    voter.add_field(
        name="投票面板按钮",
        value=(
            "点击 `开始投票` 后进入私密分页选择界面。\n"
            "可以跨页累计选择；超过上限时本次选择不会保存。\n"
            "点击 `完成选择` 后确认，提交后不可更改；不会公开投票明细。\n"
            "点击 `我的投票` 可私密查看自己的当前投票情况。"
        ),
        inline=False,
    )
    voter.add_field(
        name="/募选 结束并计票、/募选 重算结果、/募选 计票预览",
        value=(
            "`结束并计票`：应急手动结束募选，写入结果并发布。\n"
            "`重算结果`：重新计算并再次发布结果，通常用于管理员清除投票后修正。\n"
            "`计票预览`：只计算并预览，不写入、不发布，适合检查结果。"
        ),
        inline=False,
    )

    ops = discord.Embed(
        title="募选系统帮助｜4/4 运维、审计与故障处理",
        color=COLOR_GRAY,
        description="以下命令主要用于排错、人工推进和审计。",
    )
    ops.add_field(
        name="/募选 运维自检",
        value=(
            "用途：检查当前服务器未完成募选，或指定 `募选id`。\n"
            "会报告字段数、有效报名数、投票记录数、报名/投票身份组数量、缺失身份组、频道权限、公示状态等。\n"
            "只检查，不推进状态。"
        ),
        inline=False,
    )
    ops.add_field(
        name="/募选 运维tick",
        value=(
            "用途：手动执行一次 Scheduler tick，会实际推进到期状态。\n"
            "必须设置 `确认执行=True`。\n"
            "常用于 VPS 重启后、测试服快速验证阶段流转、或怀疑自动调度未执行时。"
        ),
        inline=False,
    )
    ops.add_field(
        name="/募选 运维推进",
        value=(
            "用途：按指定 `募选id` 强制推进一场募选到下一阶段。\n"
            "必须设置 `确认执行=True`，并且必须明确填写募选 ID。\n"
            "推进链路：未开始 -> 报名中 -> 报名结束/公示期 -> 投票中 -> 已完成。\n"
            "报名结束会同步刷新入口面板；统一公示未完整成功时不会强行开始投票。"
        ),
        inline=False,
    )
    ops.add_field(
        name="/募选 清除投票",
        value=(
            "用途：作废某成员投票记录，并禁止该成员重新投票。\n"
            "参数：`投票者` 必填；`募选id` 可选；`原因` 可选。\n"
            "注意：不会自动重算结果；需要时再执行 `计票预览` 或 `重算结果`。"
        ),
        inline=False,
    )
    ops.add_field(
        name="/募选 取消",
        value=(
            "用途：取消未完成募选。\n"
            "参数：`募选id` 可选；`原因` 可选，会写入状态和审计日志。\n"
            "取消后不再由 scheduler 推进。"
        ),
        inline=False,
    )
    ops.add_field(
        name="/募选 审计",
        value=(
            "用途：查看操作日志。\n"
            "参数：`募选id` 可选；`数量限制` 1-50。\n"
            "可用于追踪创建、报名、候选人处理、公示同步、投票清除、取消等关键操作。"
        ),
        inline=False,
    )
    ops.add_field(
        name="常见排错",
        value=(
            "看不到命令：重启后检查日志是否有 `CommandSyncFailure`，客户端可 Ctrl+R 刷新。\n"
            "入口不能报名：确认状态是报名中，且成员拥有允许报名身份组之一。\n"
            "不能投票：确认状态是投票中、投票面板已生成，且成员拥有允许投票身份组之一。\n"
            "公示失败：先 `/募选 运维自检`，再 `/募选 同步公示` 修复。"
        ),
        inline=False,
    )
    ops.set_footer(text="建议先在测试服完整走一遍：创建 → 报名 → 公示 → 投票 → 计票。")

    return [overview, setup, voter, ops]


def build_help_embed() -> discord.Embed:
    """Backward-compatible single-embed accessor."""

    return build_help_embeds()[0]
