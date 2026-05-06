from __future__ import annotations

import random
from datetime import timedelta
from typing import Iterable

import discord
from discord.ext import commands

from .candidate_service import CandidateService
from .constants import (
    BAN_SIDE_COMPLAINANT,
    BAN_SIDE_DEFENDANT,
    CASE_ACTIVE_DISCUSSION,
    CASE_BAN_PENDING,
    CASE_BLOCKED_INSUFFICIENT_RESPONSES,
    CASE_CANCELLED,
    CASE_COLLECTING_RESPONSES,
    CASE_MEMBER_REPLACED,
    CASE_MEMBER_SELECTED,
    CASE_VERDICT_PUBLISHED,
    CASE_VOTING,
    RESPONSE_BANNED,
    RESPONSE_DECLINED,
    RESPONSE_DM_FAILED,
    RESPONSE_INVITED,
    RESPONSE_NOT_SELECTED,
    RESPONSE_SELECTED,
    RESPONSE_WILLING,
    ban_rule_for_willing_count,
    draw_size_for_available_count,
)
from .database import InspectionDatabase
from .settings_service import InspectionSettings, InspectionSettingsService
from .utils import (
    datetime_to_iso,
    format_dt,
    human_status,
    channel_mention,
    mention_user,
    mention_users,
    normalize_ids,
    parse_iso,
    sanitize_channel_name,
    trim_text,
    utc_now,
    utc_now_iso,
)
from .views import build_case_invitation_view


class CaseService:
    """监察案件创建、响应、Ban、抽取、补抽与取消服务。"""

    def __init__(
        self,
        bot: commands.Bot,
        db: InspectionDatabase,
        settings_service: InspectionSettingsService,
        candidate_service: CandidateService,
    ):
        self.bot = bot
        self.db = db
        self.settings_service = settings_service
        self.candidate_service = candidate_service

    async def get_case(self, case_id: int) -> dict | None:
        return await self.db.fetchone("SELECT * FROM inspection_cases WHERE id = ?", (int(case_id),))

    async def find_case_by_discussion_channel(self, guild_id: int, channel_id: int) -> dict | None:
        return await self.db.fetchone(
            """
            SELECT * FROM inspection_cases
            WHERE guild_id = ? AND discussion_channel_id = ?
              AND status IN (?, ?)
            ORDER BY id DESC LIMIT 1
            """,
            (int(guild_id), int(channel_id), CASE_ACTIVE_DISCUSSION, CASE_VOTING),
        )

    async def create_case(
        self,
        guild: discord.Guild,
        *,
        created_by: int,
        description: str,
        complainant_statement: str,
        defendant_statement: str,
        response_hours: int,
        ban_hours: int,
        material_link: str | None = None,
    ) -> tuple[dict, dict[str, int]]:
        settings, error = await self.settings_service.validate_complete(guild)
        if error or settings is None:
            raise ValueError(error or "监察模块尚未完整配置。")

        role = guild.get_role(settings.candidate_role_id or 0)
        if role is None:
            raise ValueError("监察候补身份组不存在，请重新执行 /监察 设置。")

        now = utc_now()
        response_deadline = now + timedelta(hours=max(1, int(response_hours)))
        ban_deadline = response_deadline + timedelta(hours=max(1, int(ban_hours)))

        cur = await self.db.execute(
            """
            INSERT INTO inspection_cases(
              guild_id, status, description, complainant_statement, defendant_statement,
              material_link, response_deadline_at, ban_deadline_at, created_by,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(guild.id),
                CASE_COLLECTING_RESPONSES,
                description.strip(),
                complainant_statement.strip(),
                defendant_statement.strip(),
                (material_link or "").strip() or None,
                datetime_to_iso(response_deadline),
                datetime_to_iso(ban_deadline),
                int(created_by),
                datetime_to_iso(now),
                datetime_to_iso(now),
            ),
        )
        case_id = int(cur.lastrowid or 0)
        case_row = await self.get_case(case_id) if case_id else None
        if case_row is None:
            raise RuntimeError("创建监察案件失败。")

        case_id = int(case_row["id"])
        active_candidates = await self.candidate_service.list_active_candidates(guild.id)
        eligible_members: list[discord.Member] = []
        for candidate in active_candidates:
            member = await self.candidate_service._get_member(guild, int(candidate["user_id"]))
            if member is None:
                continue
            if role in member.roles:
                eligible_members.append(member)

        stats = {"invited": 0, "dm_failed": 0, "skipped": 0}
        for member in eligible_members:
            status = RESPONSE_INVITED
            dm_error = None
            try:
                dm_lines = [
                    f"你被邀请参与监察案件 #{case_id}。",
                    f"案件说明：{trim_text(description, 430)}",
                    f"投诉方说明：{trim_text(complainant_statement, 430)}",
                    f"被投诉方说明：{trim_text(defendant_statement, 430)}",
                ]
                if material_link:
                    dm_lines.append(f"材料链接：{trim_text(material_link, 250)}")
                dm_lines.extend(
                    [
                        f"响应截止：{format_dt(response_deadline)}",
                        "请点击按钮选择是否愿意参与本案临时监察组。",
                    ]
                )
                await member.send(
                    "\n".join(dm_lines),
                    view=build_case_invitation_view(case_id),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                stats["invited"] += 1
            except Exception as exc:
                status = RESPONSE_DM_FAILED
                dm_error = str(exc)[:500]
                stats["dm_failed"] += 1

            await self.db.execute(
                """
                INSERT INTO inspection_case_responses(
                  case_id, guild_id, user_id, status, responded_at, dm_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?)
                ON CONFLICT(case_id, user_id) DO UPDATE SET
                  status = excluded.status,
                  dm_error = excluded.dm_error,
                  updated_at = excluded.updated_at
                """,
                (case_id, int(guild.id), int(member.id), status, dm_error, utc_now_iso(), utc_now_iso()),
            )
        await self.db.commit()

        stats["skipped"] = max(0, len(active_candidates) - len(eligible_members))
        # 如果没有任何仍待响应的邀请（例如没有候补、或全部 DM 失败），立即推进响应阶段，
        # 避免案件一直等到响应截止才被标记为人数不足。
        await self.maybe_finish_response_phase(case_id)

        case_row = await self.get_case(case_id)
        if case_row is None:
            raise RuntimeError("创建监察案件后无法读取案件。")
        return case_row, stats

    async def handle_case_response(
        self,
        interaction: discord.Interaction,
        *,
        case_id: int,
        willing: bool,
    ) -> str:
        case = await self.get_case(case_id)
        if case is None:
            return "该案件不存在或按钮已过期。"
        if case.get("status") != CASE_COLLECTING_RESPONSES:
            return "该案件响应阶段已结束，按钮已过期。"
        deadline = parse_iso(case.get("response_deadline_at"))
        if deadline is not None and deadline <= utc_now():
            await self.finish_response_phase(int(case_id))
            return "该案件响应阶段已到截止时间，按钮已过期。"
        if int(case["guild_id"]) not in {g.id for g in self.bot.guilds}:
            return "无法定位案件所在服务器。"
        row = await self.db.fetchone(
            "SELECT * FROM inspection_case_responses WHERE case_id = ? AND user_id = ?",
            (int(case_id), int(interaction.user.id)),
        )
        if row is None or row.get("status") not in (RESPONSE_INVITED, RESPONSE_WILLING, RESPONSE_DECLINED):
            return "你不在本案可响应名单中，或该邀请已过期。"

        status = RESPONSE_WILLING if willing else RESPONSE_DECLINED
        now_iso = utc_now_iso()
        cur = await self.db.execute(
            """
            UPDATE inspection_case_responses
            SET status = ?, responded_at = ?, updated_at = ?
            WHERE case_id = ?
              AND user_id = ?
              AND status IN (?, ?, ?)
              AND EXISTS (
                SELECT 1 FROM inspection_cases c
                WHERE c.id = ?
                  AND c.status = ?
                  AND c.response_deadline_at > ?
              )
            """,
            (
                status,
                now_iso,
                now_iso,
                int(case_id),
                int(interaction.user.id),
                RESPONSE_INVITED,
                RESPONSE_WILLING,
                RESPONSE_DECLINED,
                int(case_id),
                CASE_COLLECTING_RESPONSES,
                now_iso,
            ),
        )
        await self.db.commit()
        if cur.rowcount != 1:
            await self.maybe_finish_response_phase(int(case_id))
            return "该案件响应阶段已结束，按钮已过期。"

        await self.maybe_finish_response_phase(int(case_id))
        return "已记录：愿意参与本案。" if willing else "已记录：不参与本案。"

    async def maybe_finish_response_phase(self, case_id: int) -> None:
        case = await self.get_case(case_id)
        if case is None or case.get("status") != CASE_COLLECTING_RESPONSES:
            return
        responses = await self.get_responses(case_id)
        pending = [r for r in responses if r.get("status") == RESPONSE_INVITED]
        deadline_due = datetime_to_iso(utc_now()) >= str(case.get("response_deadline_at"))
        if pending and not deadline_due:
            return
        await self.finish_response_phase(case_id)

    async def finish_response_phase(self, case_id: int) -> None:
        now_iso = utc_now_iso()
        action: str | None = None
        case: dict | None = None
        willing_count = 0
        rule = None

        # 响应阶段结束是多按钮/后台都可能触发的状态机节点，必须在同一把 DB 锁内
        # 重新读取响应与条件更新，避免“最后几个人并发响应”时用旧统计错误封案。
        async with self.db.lock:
            conn = self.db.require_conn()
            async with conn.execute("SELECT * FROM inspection_cases WHERE id = ?", (int(case_id),)) as cur_case:
                case_row = await cur_case.fetchone()
            if case_row is None or case_row["status"] != CASE_COLLECTING_RESPONSES:
                return
            case = dict(case_row)

            async with conn.execute(
                "SELECT status FROM inspection_case_responses WHERE case_id = ?",
                (int(case_id),),
            ) as cur_responses:
                response_rows = await cur_responses.fetchall()
            pending_count = sum(1 for row in response_rows if row["status"] == RESPONSE_INVITED)
            deadline_due = now_iso >= str(case.get("response_deadline_at"))
            if pending_count and not deadline_due:
                return

            willing_count = sum(1 for row in response_rows if row["status"] == RESPONSE_WILLING)
            if willing_count < 3:
                cur = await conn.execute(
                    """
                    UPDATE inspection_cases
                    SET status = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (CASE_BLOCKED_INSUFFICIENT_RESPONSES, now_iso, int(case_id), CASE_COLLECTING_RESPONSES),
                )
                if cur.rowcount != 1:
                    return
                action = "blocked"
            else:
                rule = ban_rule_for_willing_count(willing_count)
                cur = await conn.execute(
                    """
                    UPDATE inspection_cases
                    SET status = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (CASE_BAN_PENDING, now_iso, int(case_id), CASE_COLLECTING_RESPONSES),
                )
                if cur.rowcount != 1:
                    return
                action = "draw" if rule.slots_per_side <= 0 else "ban"

        if case is None or action is None:
            return

        if action == "blocked":
            await self.settings_service.send_admin_notice(
                int(case["guild_id"]),
                f"监察案件 #{case_id} 愿意参与人数不足（{willing_count} 人），无法抽取临时监察组。",
            )
            return

        if action == "draw":
            await self.settings_service.send_admin_notice(
                int(case["guild_id"]),
                f"监察案件 #{case_id} 愿意参与人数为 {willing_count} 人，无 Ban 位，将直接无 Ban 抽取。",
            )
            guild = self.bot.get_guild(int(case["guild_id"]))
            if guild is not None:
                try:
                    await self.draw_case(guild, case_id, operator_id=int(case.get("created_by") or 0), ban_user_ids=[])
                except Exception as exc:
                    await self.settings_service.send_admin_notice(
                        int(case["guild_id"]),
                        f"监察案件 #{case_id} 自动无 Ban 抽取失败：{exc}",
                    )
            return

        if rule is None:
            return
        await self.settings_service.send_admin_notice(
            int(case["guild_id"]),
            f"监察案件 #{case_id} 已进入 Ban 阶段：愿意参与 {willing_count} 人，每方 {rule.slots_per_side} 个 Ban 位，"
            f"Ban 截止：{format_dt(case.get('ban_deadline_at'))}。",
        )

    async def get_responses(self, case_id: int) -> list[dict]:
        return await self.db.fetchall(
            "SELECT * FROM inspection_case_responses WHERE case_id = ? ORDER BY created_at ASC",
            (int(case_id),),
        )

    async def get_bans(self, case_id: int) -> list[dict]:
        return await self.db.fetchall(
            "SELECT * FROM inspection_case_bans WHERE case_id = ? ORDER BY id ASC",
            (int(case_id),),
        )

    async def get_members(self, case_id: int, *, status: str | None = None) -> list[dict]:
        if status:
            return await self.db.fetchall(
                "SELECT * FROM inspection_case_members WHERE case_id = ? AND status = ? ORDER BY id ASC",
                (int(case_id), status),
            )
        return await self.db.fetchall(
            "SELECT * FROM inspection_case_members WHERE case_id = ? ORDER BY id ASC",
            (int(case_id),),
        )

    async def ban_and_draw(
        self,
        guild: discord.Guild,
        case_id: int,
        *,
        operator_id: int,
        complainant_bans: Iterable[int | None],
        defendant_bans: Iterable[int | None],
    ) -> str:
        case = await self.get_case(case_id)
        if case is None:
            raise ValueError("案件不存在。")
        if int(case["guild_id"]) != int(guild.id):
            raise ValueError("案件不属于当前服务器。")
        if case.get("status") != CASE_BAN_PENDING:
            raise ValueError("只有等待 Ban 阶段的案件可以执行 Ban 并抽取。")

        willing_ids = [int(r["user_id"]) for r in await self.get_responses(case_id) if r.get("status") == RESPONSE_WILLING]
        rule = ban_rule_for_willing_count(len(willing_ids))
        if rule.slots_per_side <= 0:
            raise ValueError("本案当前人数规则没有 Ban 位，请使用无 Ban 抽取。")

        complainant_ids = normalize_ids(complainant_bans)
        defendant_ids = normalize_ids(defendant_bans)
        self._validate_ban_side(complainant_ids, willing_ids, rule.slots_per_side, "投诉方")
        self._validate_ban_side(defendant_ids, willing_ids, rule.slots_per_side, "被投诉方")

        unique_banned = set(complainant_ids) | set(defendant_ids)
        remaining = [uid for uid in willing_ids if uid not in unique_banned]
        if len(remaining) < rule.minimum_remaining or len(remaining) < 3:
            raise ValueError(f"Ban 后剩余人数不足，至少需要保留 {max(3, rule.minimum_remaining)} 人。")

        return await self.draw_case(
            guild,
            case_id,
            operator_id=operator_id,
            ban_user_ids=list(unique_banned),
            complainant_bans=complainant_ids,
            defendant_bans=defendant_ids,
        )

    @staticmethod
    def _validate_ban_side(ids: list[int], willing_ids: list[int], slots: int, label: str) -> None:
        if len(ids) > slots:
            raise ValueError(f"{label} Ban 人数超过当前 Ban 位（{slots}）。")
        if len(ids) != len(set(ids)):
            raise ValueError(f"{label} 内部不能重复 Ban 同一人。")
        willing_set = set(willing_ids)
        for uid in ids:
            if uid not in willing_set:
                raise ValueError(f"{label} Ban 的成员 {mention_user(uid)} 不在本案 willing 响应池。")

    async def draw_case(
        self,
        guild: discord.Guild,
        case_id: int,
        *,
        operator_id: int,
        ban_user_ids: Iterable[int] = (),
        complainant_bans: Iterable[int] = (),
        defendant_bans: Iterable[int] = (),
    ) -> str:
        settings, error = await self.settings_service.validate_complete(guild)
        if error or settings is None:
            raise ValueError(error or "监察模块尚未完整配置。")

        case = await self.get_case(case_id)
        if case is None:
            raise ValueError("案件不存在。")
        if int(case["guild_id"]) != int(guild.id):
            raise ValueError("案件不属于当前服务器。")
        if case.get("status") != CASE_BAN_PENDING:
            raise ValueError("只有等待 Ban 阶段的案件可以抽取。")

        role = guild.get_role(settings.candidate_role_id or 0)
        if role is None:
            raise ValueError("监察候补身份组不存在，请重新执行 /监察 设置。")

        responses = await self.get_responses(case_id)
        willing_ids = [int(row["user_id"]) for row in responses if row.get("status") == RESPONSE_WILLING]
        banned_set = set(int(uid) for uid in ban_user_ids)
        available_ids: list[int] = []
        for uid in willing_ids:
            if uid in banned_set:
                continue
            member = await self.candidate_service._get_member(guild, uid)
            if member is None or role not in member.roles:
                continue
            available_ids.append(uid)

        draw_size = draw_size_for_available_count(len(available_ids))
        if draw_size <= 0:
            await self._mark_case_blocked(case_id)
            raise ValueError("当前仍在服务器且拥有候补身份组的可抽取人数不足 3 人，案件已标记为响应人数不足。")
        if draw_size % 2 == 0:
            raise ValueError("抽取人数规则异常：临时监察组人数必须为单数。")

        selected_ids = random.sample(available_ids, draw_size)
        discussion_channel: discord.TextChannel | None = None
        try:
            discussion_channel = await self._create_discussion_channel(guild, settings, case, selected_ids)
            await self._persist_draw_result(
                case_id,
                guild.id,
                operator_id=operator_id,
                selected_ids=selected_ids,
                banned_ids=list(banned_set),
                complainant_bans=list(complainant_bans),
                defendant_bans=list(defendant_bans),
                discussion_channel_id=int(discussion_channel.id),
                all_willing_ids=willing_ids,
            )
        except Exception:
            if discussion_channel is not None:
                try:
                    await discussion_channel.delete(reason=f"监察案件 #{case_id} 抽取落库失败，回滚临时频道")
                except Exception:
                    pass
            raise

        await self._send_discussion_intro(discussion_channel, case, selected_ids)
        await self.settings_service.send_admin_notice(
            guild.id,
            f"监察案件 #{case_id} 已完成抽取，临时监察组 {len(selected_ids)} 人（单数）：{mention_users(selected_ids)}；讨论频道：{channel_mention(discussion_channel.id)}",
        )
        return f"已完成抽取。临时监察组 {len(selected_ids)} 人（单数）：{mention_users(selected_ids)}；讨论频道：{channel_mention(discussion_channel.id)}。"

    async def _persist_draw_result(
        self,
        case_id: int,
        guild_id: int,
        *,
        operator_id: int,
        selected_ids: list[int],
        banned_ids: list[int],
        complainant_bans: list[int],
        defendant_bans: list[int],
        discussion_channel_id: int,
        all_willing_ids: list[int],
    ) -> None:
        now = utc_now_iso()
        async with self.db.lock:
            conn = self.db.require_conn()
            await conn.execute("BEGIN")
            try:
                async with conn.execute(
                    "SELECT status FROM inspection_cases WHERE id = ?",
                    (int(case_id),),
                ) as cur:
                    case_row = await cur.fetchone()
                if case_row is None or case_row["status"] != CASE_BAN_PENDING:
                    raise ValueError("案件状态已变化，抽取已取消。")

                async with conn.execute(
                    "SELECT 1 FROM inspection_case_members WHERE case_id = ? LIMIT 1",
                    (int(case_id),),
                ) as cur:
                    existing_member = await cur.fetchone()
                if existing_member is not None:
                    raise ValueError("本案已经完成过抽取。")

                for uid in complainant_bans:
                    await conn.execute(
                        """
                        INSERT INTO inspection_case_bans(case_id, guild_id, side, user_id, operator_id, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (int(case_id), int(guild_id), BAN_SIDE_COMPLAINANT, int(uid), int(operator_id), now),
                    )
                for uid in defendant_bans:
                    await conn.execute(
                        """
                        INSERT INTO inspection_case_bans(case_id, guild_id, side, user_id, operator_id, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (int(case_id), int(guild_id), BAN_SIDE_DEFENDANT, int(uid), int(operator_id), now),
                    )
                for uid in set(banned_ids):
                    await conn.execute(
                        """
                        UPDATE inspection_case_responses
                        SET status = ?, updated_at = ?
                        WHERE case_id = ? AND user_id = ?
                        """,
                        (RESPONSE_BANNED, now, int(case_id), int(uid)),
                    )
                for uid in selected_ids:
                    await conn.execute(
                        """
                        UPDATE inspection_case_responses
                        SET status = ?, updated_at = ?
                        WHERE case_id = ? AND user_id = ?
                        """,
                        (RESPONSE_SELECTED, now, int(case_id), int(uid)),
                    )
                    await conn.execute(
                        """
                        INSERT INTO inspection_case_members(
                          case_id, guild_id, user_id, status, replaced_by, replace_reason, selected_at, updated_at
                        ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)
                        """,
                        (int(case_id), int(guild_id), int(uid), CASE_MEMBER_SELECTED, now, now),
                    )
                selected_set = set(selected_ids)
                banned_set = set(banned_ids)
                for uid in all_willing_ids:
                    if uid in selected_set or uid in banned_set:
                        continue
                    await conn.execute(
                        """
                        UPDATE inspection_case_responses
                        SET status = ?, updated_at = ?
                        WHERE case_id = ? AND user_id = ?
                        """,
                        (RESPONSE_NOT_SELECTED, now, int(case_id), int(uid)),
                    )
                update_cur = await conn.execute(
                    """
                    UPDATE inspection_cases
                    SET status = ?, discussion_channel_id = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        CASE_ACTIVE_DISCUSSION,
                        int(discussion_channel_id),
                        now,
                        int(case_id),
                        CASE_BAN_PENDING,
                    ),
                )
                if update_cur.rowcount != 1:
                    raise ValueError("案件状态已变化，抽取结果未写入。")
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def _create_discussion_channel(
        self,
        guild: discord.Guild,
        settings: InspectionSettings,
        case: dict,
        selected_ids: list[int],
    ) -> discord.TextChannel:
        category = guild.get_channel(settings.discussion_category_id or 0)
        if not isinstance(category, discord.CategoryChannel):
            raise ValueError("临时讨论频道分类不存在，请重新配置。")

        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        me = guild.me or guild.get_member(self.bot.user.id) if self.bot.user else None
        if me is not None:
            overwrites[me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
            )
        for uid in selected_ids:
            member = await self.candidate_service._get_member(guild, int(uid))
            if member is not None:
                overwrites[member] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                )

        name = sanitize_channel_name(f"监察-{int(case['id'])}")
        return await guild.create_text_channel(
            name=name,
            category=category,
            overwrites=overwrites,
            reason=f"监察案件 #{int(case['id'])} 抽取临时监察组",
        )

    async def _send_discussion_intro(self, channel: discord.TextChannel, case: dict, selected_ids: list[int]) -> None:
        material = case.get("material_link") or "（无）"
        await channel.send(
            f"监察案件 #{int(case['id'])} 临时讨论频道已创建。\n"
            f"临时监察成员人数：{len(selected_ids)} 人（单数）。\n"
            f"案件说明：{trim_text(case.get('description'), 500)}\n"
            f"投诉方说明：{trim_text(case.get('complainant_statement'), 450)}\n"
            f"被投诉方说明：{trim_text(case.get('defendant_statement'), 450)}\n"
            f"材料链接：{trim_text(material, 300)}\n"
            f"临时监察成员：{mention_users(selected_ids)}\n\n"
            "讨论完成后可在本频道使用 `/监察 投票面板` 发起匿名投票。",
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )

    async def _mark_case_blocked(self, case_id: int) -> None:
        await self.db.execute(
            "UPDATE inspection_cases SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            (CASE_BLOCKED_INSUFFICIENT_RESPONSES, utc_now_iso(), int(case_id), CASE_BAN_PENDING),
        )
        await self.db.commit()

    async def replace_member(
        self,
        guild: discord.Guild,
        case_id: int,
        *,
        replaced_user_id: int,
        operator_id: int,
        reason: str | None = None,
    ) -> str:
        case = await self.get_case(case_id)
        if case is None:
            raise ValueError("案件不存在。")
        if int(case["guild_id"]) != int(guild.id):
            raise ValueError("案件不属于当前服务器。")
        if case.get("status") != CASE_ACTIVE_DISCUSSION:
            raise ValueError("只允许在讨论阶段补抽；投票开始后不允许补抽。")

        settings, error = await self.settings_service.validate_complete(guild)
        if error or settings is None:
            raise ValueError(error or "监察模块尚未完整配置。")
        role = guild.get_role(settings.candidate_role_id or 0)
        if role is None:
            raise ValueError("监察候补身份组不存在，请重新执行 /监察 设置。")

        current = await self.db.fetchone(
            """
            SELECT * FROM inspection_case_members
            WHERE case_id = ? AND user_id = ? AND status = ?
            ORDER BY id DESC LIMIT 1
            """,
            (int(case_id), int(replaced_user_id), CASE_MEMBER_SELECTED),
        )
        if current is None:
            raise ValueError("被替换用户不是当前 selected 临时监察成员。")

        pool = await self.db.fetchall(
            """
            SELECT r.user_id
            FROM inspection_case_responses r
            WHERE r.case_id = ? AND r.status = ?
              AND r.user_id NOT IN (
                SELECT m.user_id FROM inspection_case_members m WHERE m.case_id = ? AND m.status = ?
              )
            ORDER BY r.updated_at ASC
            """,
            (int(case_id), RESPONSE_NOT_SELECTED, int(case_id), CASE_MEMBER_SELECTED),
        )
        if not pool:
            raise ValueError("没有可用于补抽的未抽中候选。")

        random.shuffle(pool)
        new_user_id: int | None = None
        new_member: discord.Member | None = None
        for row in pool:
            candidate_id = int(row["user_id"])
            candidate_member = await self.candidate_service._get_member(guild, candidate_id)
            if candidate_member is not None and role in candidate_member.roles:
                new_user_id = candidate_id
                new_member = candidate_member
                break
        if new_user_id is None or new_member is None:
            raise ValueError("没有仍在服务器且拥有候补身份组的可补抽候选。")

        channel = guild.get_channel(int(case.get("discussion_channel_id") or 0))
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("无法定位本案临时讨论频道。")

        old_member = await self.candidate_service._get_member(guild, int(replaced_user_id))

        if old_member is not None:
            await channel.set_permissions(old_member, overwrite=None, reason="监察组补抽：移除被替换成员权限")
        await channel.set_permissions(
            new_member,
            overwrite=discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            reason="监察组补抽：授予新成员权限",
        )

        now = utc_now_iso()
        try:
            async with self.db.lock:
                conn = self.db.require_conn()
                await conn.execute("BEGIN")
                try:
                    async with conn.execute(
                        "SELECT status FROM inspection_cases WHERE id = ?",
                        (int(case_id),),
                    ) as cur_case:
                        case_row = await cur_case.fetchone()
                    if case_row is None or case_row["status"] != CASE_ACTIVE_DISCUSSION:
                        raise ValueError("案件状态已变化，补抽取消。")

                    cur = await conn.execute(
                        """
                        UPDATE inspection_case_members
                        SET status = ?, replaced_by = ?, replace_reason = ?, updated_at = ?
                        WHERE id = ? AND status = ?
                        """,
                        (CASE_MEMBER_REPLACED, new_user_id, reason, now, int(current["id"]), CASE_MEMBER_SELECTED),
                    )
                    if cur.rowcount != 1:
                        raise ValueError("被替换成员状态已变化，补抽取消。")
                    await conn.execute(
                        """
                        INSERT INTO inspection_case_members(
                          case_id, guild_id, user_id, status, replaced_by, replace_reason, selected_at, updated_at
                        ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)
                        """,
                        (int(case_id), int(guild.id), new_user_id, CASE_MEMBER_SELECTED, now, now),
                    )
                    await conn.execute(
                        "UPDATE inspection_case_responses SET status = ?, updated_at = ? WHERE case_id = ? AND user_id = ?",
                        (RESPONSE_SELECTED, now, int(case_id), new_user_id),
                    )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
        except Exception:
            # 权限已更新但 DB 落库失败时尽量回滚权限，避免频道状态和数据库不一致。
            try:
                if old_member is not None:
                    await channel.set_permissions(
                        old_member,
                        overwrite=discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                        reason="监察组补抽落库失败：恢复原成员权限",
                    )
                await channel.set_permissions(new_member, overwrite=None, reason="监察组补抽落库失败：移除新成员权限")
            except Exception:
                pass
            raise

        await channel.send(
            f"监察案件 #{case_id} 补抽完成：{mention_user(int(replaced_user_id))} 被替换，"
            f"新成员为 {mention_user(new_user_id)}。原因：{reason or '（未填写）'}",
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
        return f"补抽完成：{mention_user(int(replaced_user_id))} -> {mention_user(new_user_id)}。"

    async def cancel_case(
        self,
        guild: discord.Guild,
        case_id: int,
        *,
        reason: str | None = None,
    ) -> str:
        case = await self.get_case(case_id)
        if case is None:
            raise ValueError("案件不存在。")
        if int(case["guild_id"]) != int(guild.id):
            raise ValueError("案件不属于当前服务器。")
        if case.get("status") == CASE_CANCELLED:
            raise ValueError("案件已经取消。")

        now = utc_now_iso()
        cur = await self.db.execute(
            """
            UPDATE inspection_cases
            SET status = ?, updated_at = ?, closed_at = ?
            WHERE id = ?
              AND status NOT IN (?, ?)
            """,
            (
                CASE_CANCELLED,
                now,
                now,
                int(case_id),
                CASE_CANCELLED,
                CASE_VERDICT_PUBLISHED,
            ),
        )
        await self.db.commit()
        if cur.rowcount != 1:
            latest = await self.get_case(case_id)
            latest_status = human_status(latest.get("status") if latest else None)
            raise ValueError(f"案件状态已变化（当前：{latest_status}），不允许取消。")

        notice = f"监察案件 #{case_id} 已取消。原因：{reason or '（未填写）'}"
        channel_id = case.get("discussion_channel_id")
        if channel_id:
            channel = guild.get_channel(int(channel_id))
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(notice, allowed_mentions=discord.AllowedMentions.none())
                except Exception:
                    pass
        await self.settings_service.send_admin_notice(guild.id, notice)
        return notice

    async def process_response_due_cases(self) -> None:
        rows = await self.db.fetchall(
            """
            SELECT * FROM inspection_cases
            WHERE status = ? AND response_deadline_at <= ?
            """,
            (CASE_COLLECTING_RESPONSES, utc_now_iso()),
        )
        for row in rows:
            try:
                await self.finish_response_phase(int(row["id"]))
            except Exception as exc:
                await self.settings_service.send_admin_notice(
                    int(row["guild_id"]),
                    f"监察案件 #{int(row['id'])} 响应阶段自动推进失败：{exc}",
                )

    async def process_ban_due_cases(self) -> None:
        rows = await self.db.fetchall(
            """
            SELECT * FROM inspection_cases
            WHERE status = ? AND ban_deadline_at <= ?
            """,
            (CASE_BAN_PENDING, utc_now_iso()),
        )
        for row in rows:
            case_id = int(row["id"])
            bans = await self.get_bans(case_id)
            if bans:
                if int(row.get("no_ban_timeout_notified") or 0) == 0:
                    await self.db.execute(
                        "UPDATE inspection_cases SET no_ban_timeout_notified = 1, updated_at = ? WHERE id = ?",
                        (utc_now_iso(), case_id),
                    )
                    await self.db.commit()
                    await self.settings_service.send_admin_notice(
                        int(row["guild_id"]),
                        f"监察案件 #{case_id} Ban 阶段已超时但仍停留在 ban_pending，且已有 Ban 记录，请管理员关注。",
                    )
                continue
            if int(row.get("no_ban_timeout_notified") or 0) == 0:
                await self.db.execute(
                    "UPDATE inspection_cases SET no_ban_timeout_notified = 1, updated_at = ? WHERE id = ?",
                    (utc_now_iso(), case_id),
                )
                await self.db.commit()
                await self.settings_service.send_admin_notice(
                    int(row["guild_id"]),
                    f"监察案件 #{case_id} Ban 阶段超时且没有录入 Ban，将自动无 Ban 抽取。",
                )
            guild = self.bot.get_guild(int(row["guild_id"]))
            if guild is None:
                continue
            try:
                await self.draw_case(guild, case_id, operator_id=int(row.get("created_by") or 0), ban_user_ids=[])
            except Exception as exc:
                await self.settings_service.send_admin_notice(
                    int(row["guild_id"]),
                    f"监察案件 #{case_id} 自动无 Ban 抽取失败：{exc}",
                )

    async def render_case_status(self, case_id: int) -> str:
        case = await self.get_case(case_id)
        if case is None:
            raise ValueError("案件不存在。")
        responses = await self.get_responses(case_id)
        bans = await self.get_bans(case_id)
        members = await self.get_members(case_id)
        response_counts: dict[str, int] = {}
        for row in responses:
            status = str(row.get("status"))
            response_counts[status] = response_counts.get(status, 0) + 1
        counts_text = "、".join(f"{human_status(k)} {v}" for k, v in sorted(response_counts.items())) or "（无）"
        response_list_text = self._render_response_lists(responses)
        bans_text = "、".join(
            f"{'投诉方' if b.get('side') == BAN_SIDE_COMPLAINANT else '被投诉方'} Ban {mention_user(int(b['user_id']))}"
            for b in bans
        ) or "（无）"
        members_text = "、".join(
            f"{mention_user(int(m['user_id']))}（{human_status(m.get('status'))}）"
            for m in members
        ) or "（无）"
        selected_count = sum(1 for m in members if m.get("status") == CASE_MEMBER_SELECTED)
        return (
            f"监察案件 #{int(case['id'])}\n"
            f"- 当前状态：{human_status(case.get('status'))}\n"
            f"- 响应统计：{counts_text}\n"
            f"- 响应名单：\n{response_list_text}\n"
            f"- Ban 记录：{bans_text}\n"
            f"- 当前临时监察组人数：{selected_count} 人（{'单数' if selected_count % 2 == 1 else '偶数/异常'}）\n"
            f"- 临时监察组成员：{members_text}\n"
            f"- 响应截止：{format_dt(case.get('response_deadline_at'))}\n"
            f"- Ban 截止：{format_dt(case.get('ban_deadline_at'))}\n"
            f"- 投票截止：{format_dt(case.get('vote_deadline_at'))}\n"
            f"- 裁决结果：{case.get('verdict') or '（未形成/未公示）'}\n"
            f"- 材料链接：{case.get('material_link') or '（无）'}\n"
            f"- 案件说明：{trim_text(case.get('description'), 300)}\n"
            f"- 投诉方说明：{trim_text(case.get('complainant_statement'), 240)}\n"
            f"- 被投诉方说明：{trim_text(case.get('defendant_statement'), 240)}"
        )

    @staticmethod
    def _format_response_user_list(user_ids: list[int], *, max_items: int = 18) -> str:
        if not user_ids:
            return "（无）"
        shown = user_ids[:max_items]
        parts = [f"{mention_user(uid)}(`{uid}`)" for uid in shown]
        remaining = len(user_ids) - len(shown)
        if remaining > 0:
            parts.append(f"……另 {remaining} 人")
        return "、".join(parts)

    @classmethod
    def _render_response_lists(cls, responses: list[dict]) -> str:
        groups = [
            ("愿意参与（Ban 可选）", {RESPONSE_WILLING}, 18),
            ("未响应", {RESPONSE_INVITED}, 8),
            ("已拒绝", {RESPONSE_DECLINED}, 8),
            ("DM 失败", {RESPONSE_DM_FAILED}, 8),
            ("已抽中", {RESPONSE_SELECTED}, 8),
            ("未抽中", {RESPONSE_NOT_SELECTED}, 8),
            ("已被 Ban", {RESPONSE_BANNED}, 8),
        ]
        lines: list[str] = []
        for label, statuses, max_items in groups:
            ids = [int(row["user_id"]) for row in responses if row.get("status") in statuses]
            if ids:
                lines.append(f"  - {label}：{cls._format_response_user_list(ids, max_items=max_items)}")
        return "\n".join(lines) if lines else "  - （无响应记录）"
