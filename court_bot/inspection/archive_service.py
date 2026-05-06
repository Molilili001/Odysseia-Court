from __future__ import annotations

import gc
import html as html_escape
import io
import json
from dataclasses import dataclass
from typing import Iterable

import discord
from discord.ext import commands

from court_bot.services.archive_export import ArchiveBuildResult, build_archive

from .constants import (
    ARCHIVABLE_CASE_STATUSES,
    ARCHIVE_ACTION_DELETE,
    ARCHIVE_ACTION_LOCK,
    ARCHIVE_ACTION_ONLY,
    BAN_SIDE_COMPLAINANT,
    CASE_MEMBER_REPLACED,
    CASE_MEMBER_SELECTED,
    RESPONSE_BANNED,
    RESPONSE_DECLINED,
    RESPONSE_DM_FAILED,
    RESPONSE_INVITED,
    RESPONSE_NOT_SELECTED,
    RESPONSE_SELECTED,
    RESPONSE_WILLING,
    VOTE_NO,
    VOTE_YES,
)
from .database import InspectionDatabase
from .settings_service import InspectionSettingsService
from .utils import channel_mention, human_status, parse_iso, trim_text, utc_now_iso


@dataclass(slots=True)
class InspectionArchiveResult:
    case_id: int
    mode: str
    filename: str
    archive_channel_id: int
    archive_message_id: int
    action: str
    warnings: list[str]
    fallback_used: bool = False
    channel_action_done: bool = False
    had_discussion_channel: bool = True


class InspectionArchiveService:
    """监察案件独立归档服务。

    只复用通用 HTML/ZIP 导出器，不复用 /议诉 数据库或业务状态。
    归档摘要只输出匿名投票统计，不输出 user_id -> vote 的实名映射。
    """

    def __init__(self, bot: commands.Bot, db: InspectionDatabase, settings_service: InspectionSettingsService):
        self.bot = bot
        self.db = db
        self.settings_service = settings_service

    async def archive_case(
        self,
        guild: discord.Guild,
        case_id: int,
        *,
        operator_id: int | None,
        action: str = ARCHIVE_ACTION_ONLY,
        archive_channel: discord.TextChannel | None = None,
    ) -> InspectionArchiveResult:
        action = self._normalize_action(action)
        case = await self.db.fetchone("SELECT * FROM inspection_cases WHERE id = ?", (int(case_id),))
        if case is None:
            raise ValueError("案件不存在。")
        if int(case["guild_id"]) != int(guild.id):
            raise ValueError("案件不属于当前服务器。")
        if case.get("status") not in ARCHIVABLE_CASE_STATUSES:
            allowed = "、".join(human_status(status) for status in ARCHIVABLE_CASE_STATUSES)
            raise ValueError(f"只有已结束监察案件可以归档（允许状态：{allowed}）。")

        discussion_channel = await self._resolve_discussion_channel(guild, case)
        target_channel, fallback_used = await self._resolve_archive_channel(guild, archive_channel=archive_channel)
        if discussion_channel is not None and action == ARCHIVE_ACTION_DELETE and int(target_channel.id) == int(discussion_channel.id):
            raise ValueError("归档并删除时，归档频道不能与本案临时讨论频道相同，否则归档文件会随频道一起删除。")

        header_lines = await self._build_header_lines(guild, case, operator_id=operator_id)
        result = None
        async with self._archive_semaphore():
            try:
                if discussion_channel is not None:
                    result = await build_archive(
                        channel=discussion_channel,
                        header_lines=header_lines,
                        guild_filesize_limit=int(guild.filesize_limit),
                        media_budget_bytes=self._media_budget_bytes(),
                        single_image_max_bytes=self._single_image_max_bytes(),
                        archive_title="监察归档",
                    )
                    description = f"已从 {discussion_channel.mention} 导出为 {result.mode.upper()}。\n处理方式：{self.action_label(action)}。"
                else:
                    result = self._build_summary_only_archive(int(case_id), header_lines)
                    description = "本案没有可读取的临时讨论频道，已导出案件摘要。\n处理方式：仅归档案件摘要。"
                    action_to_record = ARCHIVE_ACTION_ONLY

                summary = discord.Embed(
                    title=f"监察案件 #{int(case_id)}｜归档",
                    description=description,
                    color=0x2B2D31,
                )
                summary.add_field(name="案件状态", value=human_status(case.get("status")), inline=True)
                summary.add_field(name="裁决结果", value=str(case.get("verdict") or "（无/未形成）"), inline=True)
                summary.add_field(name="匿名投票统计", value=await self._vote_stats_text(int(case_id)), inline=False)
                if fallback_used:
                    summary.add_field(name="提示", value="未配置监察归档频道，本次已回退发送到 admin 通知频道。", inline=False)
                if result.warnings:
                    summary.add_field(name="注意", value="\n".join(result.warnings)[:1024], inline=False)
                if operator_id:
                    summary.set_footer(text=f"归档人：{operator_id}")

                ext = "zip" if result.mode == "zip" else "html"
                filename = f"inspection-{int(case_id):04d}-archive.{ext}"
                file = discord.File(fp=io.BytesIO(result.data), filename=filename)
                archive_message = await target_channel.send(
                    embed=summary,
                    file=file,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

                await self._record_archive(
                    case_id=int(case_id),
                    guild_id=int(guild.id),
                    archive_channel_id=int(target_channel.id),
                    archive_message_id=int(archive_message.id),
                    archive_mode=str(result.mode),
                    filename=filename,
                    action=action if discussion_channel is not None else action_to_record,
                    operator_id=operator_id,
                    warnings=list(result.warnings),
                )

                channel_action_done = False
                if discussion_channel is not None:
                    channel_action_done = await self._apply_channel_action(
                        guild,
                        case,
                        discussion_channel,
                        action=action,
                        operator_id=operator_id,
                    )

                return InspectionArchiveResult(
                    case_id=int(case_id),
                    mode=str(result.mode),
                    filename=filename,
                    archive_channel_id=int(target_channel.id),
                    archive_message_id=int(archive_message.id),
                    action=action if discussion_channel is not None else action_to_record,
                    warnings=list(result.warnings),
                    fallback_used=fallback_used,
                    channel_action_done=channel_action_done,
                    had_discussion_channel=discussion_channel is not None,
                )
            finally:
                result = None
                gc.collect()

    def render_result_message(self, result: InspectionArchiveResult) -> str:
        lines = [
            f"监察案件 #{result.case_id} 已归档。",
            f"- 归档文件：`{result.filename}`（{result.mode.upper()}）",
            f"- 归档频道：{channel_mention(result.archive_channel_id)}",
            f"- 处理方式：{self.action_label(result.action)}",
        ]
        if not result.had_discussion_channel:
            lines.append("- 本案没有可读取的临时讨论频道；本次仅归档案件摘要。")
        else:
            if result.action == ARCHIVE_ACTION_ONLY:
                lines.append("- 原临时讨论频道已保留。")
            elif result.action == ARCHIVE_ACTION_LOCK:
                lines.append("- 原临时讨论频道已锁定为仅管理可见。" if result.channel_action_done else "- 原临时讨论频道权限锁定未执行或未完全成功，请检查权限。")
            elif result.action == ARCHIVE_ACTION_DELETE:
                lines.append("- 原临时讨论频道已删除。" if result.channel_action_done else "- 原临时讨论频道删除未完成，请检查权限。")
        if result.fallback_used:
            lines.append("- 提示：未配置监察归档频道，本次回退发送到 admin 通知频道。")
        if result.warnings:
            lines.append("- 注意：" + "；".join(result.warnings)[:800])
        return "\n".join(lines)

    async def _resolve_discussion_channel(self, guild: discord.Guild, case: dict) -> discord.TextChannel | None:
        channel_id = int(case.get("discussion_channel_id") or 0)
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                fetched = await self.bot.fetch_channel(channel_id)
            except Exception:
                fetched = None
            channel = fetched if isinstance(fetched, discord.TextChannel) else None
        if not isinstance(channel, discord.TextChannel):
            return None
        return channel

    async def _resolve_archive_channel(
        self,
        guild: discord.Guild,
        *,
        archive_channel: discord.TextChannel | None,
    ) -> tuple[discord.TextChannel, bool]:
        if archive_channel is not None:
            if int(archive_channel.guild.id) != int(guild.id):
                raise ValueError("指定的归档频道不属于当前服务器。")
            return archive_channel, False

        settings = await self.settings_service.get_settings(guild.id)
        configured = await self.settings_service.get_message_channel(settings.archive_channel_id)
        if isinstance(configured, discord.TextChannel):
            return configured, False

        fallback = await self.settings_service.get_message_channel(settings.admin_notice_channel_id)
        if isinstance(fallback, discord.TextChannel):
            return fallback, True
        raise ValueError("未配置可用的监察归档频道，且 admin 通知频道不可用。")

    async def _build_header_lines(self, guild: discord.Guild, case: dict, *, operator_id: int | None) -> list[str]:
        case_id = int(case["id"])
        responses = await self._get_responses(case_id)
        bans = await self._get_bans(case_id)
        members = await self._get_members(case_id)
        archive_rows = await self._get_archives(case_id)

        header_lines = [
            f"监察案件编号：#{case_id}",
            f"服务器：{guild.name}（ID：{guild.id}）",
            f"案件状态：{human_status(case.get('status'))}",
            f"裁决结果：{case.get('verdict') or '（无/未形成）'}",
            f"创建人：{case.get('created_by')}",
            f"创建时间：{self._archive_dt(case.get('created_at'), fallback='未知')}",
            f"结案时间：{self._archive_dt(case.get('closed_at'), fallback='未知')}",
            f"响应截止：{self._archive_dt(case.get('response_deadline_at'), fallback='未知')}",
            f"Ban 截止：{self._archive_dt(case.get('ban_deadline_at'), fallback='未知')}",
            f"投票截止：{self._archive_dt(case.get('vote_deadline_at'), fallback='未开始')}",
            f"讨论频道：{case.get('discussion_channel_id') or '（无）'}",
            f"投票面板消息 ID：{case.get('vote_panel_message_id') or '（无）'}",
            "",
            f"案件说明：{case.get('description') or '（无）'}",
            f"投诉方说明：{case.get('complainant_statement') or '（无）'}",
            f"被投诉方说明：{case.get('defendant_statement') or '（无）'}",
            f"材料链接：{case.get('material_link') or '（无）'}",
            "",
            f"响应统计：{self._response_counts_text(responses)}",
            f"Ban 记录：{self._bans_text(bans)}",
            f"临时监察成员：{self._members_text(members)}",
            f"补抽记录：{self._replacement_text(members)}",
            f"匿名投票统计：{await self._vote_stats_text(case_id)}",
            "投票隐私：本归档只记录匿名统计，不导出投票人和票值的对应关系。",
        ]
        if archive_rows:
            header_lines.append(f"既有归档次数：{len(archive_rows)}")
        if operator_id:
            header_lines.append(f"本次归档操作人：{operator_id}")
        return header_lines

    async def _get_responses(self, case_id: int) -> list[dict]:
        return await self.db.fetchall(
            "SELECT * FROM inspection_case_responses WHERE case_id = ? ORDER BY created_at ASC",
            (int(case_id),),
        )

    async def _get_bans(self, case_id: int) -> list[dict]:
        return await self.db.fetchall(
            "SELECT * FROM inspection_case_bans WHERE case_id = ? ORDER BY id ASC",
            (int(case_id),),
        )

    async def _get_members(self, case_id: int) -> list[dict]:
        return await self.db.fetchall(
            "SELECT * FROM inspection_case_members WHERE case_id = ? ORDER BY id ASC",
            (int(case_id),),
        )

    async def _get_archives(self, case_id: int) -> list[dict]:
        return await self.db.fetchall(
            "SELECT * FROM inspection_case_archives WHERE case_id = ? ORDER BY id ASC",
            (int(case_id),),
        )

    async def _vote_stats_text(self, case_id: int) -> str:
        selected_rows = await self.db.fetchall(
            """
            SELECT user_id FROM inspection_case_members
            WHERE case_id = ? AND status = ?
            ORDER BY id ASC
            """,
            (int(case_id), CASE_MEMBER_SELECTED),
        )
        total_members = len(selected_rows)
        vote_rows = await self.db.fetchall(
            "SELECT vote, COUNT(*) AS count FROM inspection_votes WHERE case_id = ? GROUP BY vote",
            (int(case_id),),
        )
        yes = sum(int(row.get("count") or 0) for row in vote_rows if row.get("vote") == VOTE_YES)
        no = sum(int(row.get("count") or 0) for row in vote_rows if row.get("vote") == VOTE_NO)
        absent = max(0, total_members - yes - no)
        if total_members <= 0 and yes + no <= 0:
            return "（无投票记录）"
        return f"诉求合理 {yes} 票 / 诉求不合理 {no} 票 / 未投票 {absent} 人 / 当前临时监察成员 {total_members} 人"

    @staticmethod
    def _response_counts_text(responses: Iterable[dict]) -> str:
        order = [
            RESPONSE_INVITED,
            RESPONSE_WILLING,
            RESPONSE_DECLINED,
            RESPONSE_DM_FAILED,
            RESPONSE_SELECTED,
            RESPONSE_NOT_SELECTED,
            RESPONSE_BANNED,
        ]
        counts: dict[str, int] = {}
        for row in responses:
            status = str(row.get("status") or "")
            counts[status] = counts.get(status, 0) + 1
        parts = [f"{human_status(status)} {counts[status]}" for status in order if counts.get(status)]
        for status, count in sorted(counts.items()):
            if status not in order:
                parts.append(f"{human_status(status)} {count}")
        return "、".join(parts) if parts else "（无）"

    @staticmethod
    def _bans_text(bans: Iterable[dict]) -> str:
        parts: list[str] = []
        for row in bans:
            side = "投诉方" if row.get("side") == BAN_SIDE_COMPLAINANT else "被投诉方"
            parts.append(f"{side} Ban {row.get('user_id')}（操作人：{row.get('operator_id')}）")
        return "、".join(parts) if parts else "（无）"

    @staticmethod
    def _members_text(members: Iterable[dict]) -> str:
        parts: list[str] = []
        for row in members:
            if row.get("status") == CASE_MEMBER_REPLACED:
                parts.append(f"{row.get('user_id')}（已被替换 -> {row.get('replaced_by') or '未知'}）")
            else:
                parts.append(f"{row.get('user_id')}（{human_status(row.get('status'))}）")
        return "、".join(parts) if parts else "（无）"

    @staticmethod
    def _replacement_text(members: Iterable[dict]) -> str:
        parts: list[str] = []
        for row in members:
            if row.get("status") != CASE_MEMBER_REPLACED:
                continue
            parts.append(
                f"{row.get('user_id')} -> {row.get('replaced_by') or '未知'}；原因：{trim_text(row.get('replace_reason') or '（未填写）', 200)}"
            )
        return "、".join(parts) if parts else "（无）"

    async def _record_archive(
        self,
        *,
        case_id: int,
        guild_id: int,
        archive_channel_id: int,
        archive_message_id: int,
        archive_mode: str,
        filename: str,
        action: str,
        operator_id: int | None,
        warnings: list[str],
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO inspection_case_archives(
              case_id, guild_id, archive_channel_id, archive_message_id,
              archive_mode, filename, action, operator_id, warnings_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(case_id),
                int(guild_id),
                int(archive_channel_id),
                int(archive_message_id),
                str(archive_mode),
                str(filename),
                str(action),
                int(operator_id) if operator_id else None,
                json.dumps(warnings, ensure_ascii=False),
                utc_now_iso(),
            ),
        )
        await self.db.commit()

    async def _apply_channel_action(
        self,
        guild: discord.Guild,
        case: dict,
        channel: discord.TextChannel,
        *,
        action: str,
        operator_id: int | None,
    ) -> bool:
        case_id = int(case["id"])
        if action == ARCHIVE_ACTION_ONLY:
            try:
                await channel.send(
                    f"监察案件 #{case_id} 已完成归档；本频道按“仅归档”处理继续保留。",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                pass
            return True

        if action == ARCHIVE_ACTION_LOCK:
            try:
                selected_rows = await self.db.fetchall(
                    """
                    SELECT DISTINCT user_id FROM inspection_case_members
                    WHERE case_id = ?
                    """,
                    (case_id,),
                )
                hidden_count = 0
                for row in selected_rows:
                    member = guild.get_member(int(row["user_id"]))
                    if member is None:
                        try:
                            member = await guild.fetch_member(int(row["user_id"]))
                        except Exception:
                            member = None
                    if member is None:
                        continue
                    await channel.set_permissions(
                        member,
                        overwrite=discord.PermissionOverwrite(
                            view_channel=False,
                            send_messages=False,
                            read_message_history=False,
                        ),
                        reason=f"监察案件 #{case_id} 已归档并锁定为仅管理可见",
                    )
                    hidden_count += 1
                await channel.send(
                    f"监察案件 #{case_id} 已完成归档；本频道已锁定为仅管理可见，"
                    f"已移除 {hidden_count} 名临时监察成员的查看权限。",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return True
            except Exception as exc:
                await self.settings_service.send_admin_notice(
                    int(guild.id),
                    f"监察案件 #{case_id} 已归档，但锁定临时讨论频道为仅管理可见失败：{exc}",
                )
                return False

        if action == ARCHIVE_ACTION_DELETE:
            try:
                await channel.delete(reason=f"监察案件 #{case_id} 已归档并删除；操作人：{operator_id or '未知'}")
                return True
            except Exception as exc:
                await self.settings_service.send_admin_notice(
                    int(guild.id),
                    f"监察案件 #{case_id} 已归档，但删除临时讨论频道失败：{exc}",
                )
                return False

        return False

    def _archive_semaphore(self):
        semaphore = getattr(self.bot, "_archive_semaphore", None)
        if semaphore is not None:
            return semaphore
        return _NoopAsyncContext()

    def _media_budget_bytes(self) -> int:
        config = getattr(self.bot, "config", None)
        value = int(getattr(config, "archive_media_budget_mb", 0) or 0)
        return value * 1024 * 1024 if value > 0 else 0

    def _single_image_max_bytes(self) -> int:
        config = getattr(self.bot, "config", None)
        value = int(getattr(config, "archive_single_image_max_mb", 0) or 0)
        return value * 1024 * 1024 if value > 0 else 0

    @staticmethod
    def _normalize_action(action: str) -> str:
        if action in {ARCHIVE_ACTION_ONLY, ARCHIVE_ACTION_LOCK, ARCHIVE_ACTION_DELETE}:
            return action
        return ARCHIVE_ACTION_ONLY

    @staticmethod
    def action_label(action: str) -> str:
        return {
            ARCHIVE_ACTION_ONLY: "仅归档",
            ARCHIVE_ACTION_LOCK: "归档并锁定频道（仅管理可见）",
            ARCHIVE_ACTION_DELETE: "归档并删除频道",
        }.get(action, "仅归档")

    @staticmethod
    def _build_summary_only_archive(case_id: int, header_lines: list[str]) -> ArchiveBuildResult:
        escaped_lines = "\n".join(html_escape.escape(line) for line in header_lines)
        html = f"""<!doctype html>
<html lang='zh-CN'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>监察归档</title>
  <style>
    body {{ margin:0; background:#313338; color:#dbdee1; font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; }}
    .wrap {{ max-width: 1000px; margin: 0 auto; padding: 24px; }}
    .header {{ background:#2b2d31; border-radius:10px; padding:18px 20px; }}
    h1 {{ margin:0 0 10px 0; font-size:18px; }}
    pre {{ color:#b5bac1; white-space:pre-wrap; line-height:1.5; font-size:13px; }}
  </style>
</head>
<body><div class='wrap'><div class='header'><h1>📌 监察归档</h1><pre>{escaped_lines}</pre></div></div></body>
</html>"""
        return ArchiveBuildResult(
            mode="html",
            filename=f"inspection-{int(case_id):04d}-summary.html",
            data=html.encode("utf-8"),
            warnings=["本案没有可读取的临时讨论频道，归档仅包含案件摘要。"],
        )

    @staticmethod
    def _archive_dt(value: str | None, *, fallback: str) -> str:
        dt = parse_iso(value)
        if dt is None:
            return fallback
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


class _NoopAsyncContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return False
