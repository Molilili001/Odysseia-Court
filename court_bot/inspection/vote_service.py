from __future__ import annotations

from datetime import timedelta

import discord
from discord.ext import commands

from .constants import (
    CASE_ACTIVE_DISCUSSION,
    CASE_MEMBER_SELECTED,
    CASE_VERDICT_PUBLISHED,
    CASE_VOTING,
    VERDICT_NO_MAJORITY,
    VERDICT_REASONABLE,
    VERDICT_UNREASONABLE,
    VOTE_NO,
    VOTE_YES,
)
from .database import InspectionDatabase
from .settings_service import InspectionSettingsService
from .utils import datetime_to_iso, format_dt, mention_users, parse_iso, trim_text, utc_now, utc_now_iso
from .views import build_vote_panel_view


class VoteService:
    """监察案件匿名投票服务。"""

    def __init__(self, bot: commands.Bot, db: InspectionDatabase, settings_service: InspectionSettingsService):
        self.bot = bot
        self.db = db
        self.settings_service = settings_service

    async def get_case(self, case_id: int) -> dict | None:
        return await self.db.fetchone("SELECT * FROM inspection_cases WHERE id = ?", (int(case_id),))

    async def find_case_by_channel(self, guild_id: int, channel_id: int) -> dict | None:
        return await self.db.fetchone(
            """
            SELECT * FROM inspection_cases
            WHERE guild_id = ? AND discussion_channel_id = ?
              AND status IN (?, ?)
            ORDER BY id DESC LIMIT 1
            """,
            (int(guild_id), int(channel_id), CASE_ACTIVE_DISCUSSION, CASE_VOTING),
        )

    async def list_selected_member_ids(self, case_id: int) -> list[int]:
        rows = await self.db.fetchall(
            """
            SELECT user_id FROM inspection_case_members
            WHERE case_id = ? AND status = ?
            ORDER BY id ASC
            """,
            (int(case_id), CASE_MEMBER_SELECTED),
        )
        return [int(row["user_id"]) for row in rows]

    async def user_is_selected_member(self, case_id: int, user_id: int) -> bool:
        row = await self.db.fetchone(
            """
            SELECT 1 FROM inspection_case_members
            WHERE case_id = ? AND user_id = ? AND status = ?
            LIMIT 1
            """,
            (int(case_id), int(user_id), CASE_MEMBER_SELECTED),
        )
        return row is not None

    async def start_vote_panel(
        self,
        interaction: discord.Interaction,
        *,
        case_id: int | None,
        vote_hours: int,
        is_admin: bool,
    ) -> str:
        if interaction.guild is None or interaction.channel is None:
            raise ValueError("请在服务器内的监察临时讨论频道使用。")

        case = await self.get_case(case_id) if case_id is not None else await self.find_case_by_channel(
            interaction.guild.id,
            interaction.channel.id,
        )
        if case is None:
            raise ValueError("无法定位监察案件。请填写案件 ID，或在对应临时讨论频道内使用。")
        if int(case["guild_id"]) != int(interaction.guild.id):
            raise ValueError("案件不属于当前服务器。")
        if int(case.get("discussion_channel_id") or 0) != int(interaction.channel.id):
            raise ValueError("投票面板只能在本案临时讨论频道内召唤。")
        if case.get("status") != CASE_ACTIVE_DISCUSSION:
            raise ValueError("只有讨论阶段的案件可以开始投票。")
        if case.get("vote_panel_message_id"):
            raise ValueError("本案已经创建过投票面板。")

        selected_ids = await self.list_selected_member_ids(int(case["id"]))
        if len(selected_ids) < 3:
            raise ValueError("当前临时监察成员少于 3 人，不能开始投票。")
        if len(selected_ids) % 2 == 0:
            raise ValueError("当前临时监察成员人数为偶数，不能开始投票；请先通过补抽/调整流程保持最终监察组人数为单数。")
        if not is_admin and int(interaction.user.id) not in set(selected_ids):
            raise ValueError("只有管理员或本案临时监察成员可以召唤投票面板。")

        deadline = utc_now() + timedelta(hours=max(1, int(vote_hours)))
        msg: discord.Message | None = None
        try:
            msg = await interaction.channel.send(
                f"监察案件 #{int(case['id'])} 匿名投票开始。\n"
                f"临时监察成员：{mention_users(selected_ids)}\n"
                f"投票截止：{format_dt(deadline)}\n\n"
                "请选择：诉求合理 / 诉求不合理。投票匿名，可在截止前改票。",
                view=build_vote_panel_view(int(case["id"])),
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
            now_iso = utc_now_iso()
            async with self.db.lock:
                conn = self.db.require_conn()
                async with conn.execute(
                    "SELECT status FROM inspection_cases WHERE id = ?",
                    (int(case["id"]),),
                ) as cur_status:
                    current_case = await cur_status.fetchone()
                if current_case is None or current_case["status"] != CASE_ACTIVE_DISCUSSION:
                    raise ValueError("案件状态已变化，投票面板创建已取消。")
                async with conn.execute(
                    """
                    SELECT user_id FROM inspection_case_members
                    WHERE case_id = ? AND status = ?
                    ORDER BY id ASC
                    """,
                    (int(case["id"]), CASE_MEMBER_SELECTED),
                ) as cur_members:
                    current_selected_ids = [int(row["user_id"]) for row in await cur_members.fetchall()]
                if current_selected_ids != selected_ids:
                    raise ValueError("临时监察成员已变化，请重新召唤投票面板。")
                if len(current_selected_ids) % 2 == 0:
                    raise ValueError("当前临时监察成员人数为偶数，不能开始投票。")

                cur = await conn.execute(
                    """
                    UPDATE inspection_cases
                    SET status = ?, vote_panel_message_id = ?, vote_deadline_at = ?, updated_at = ?
                    WHERE id = ? AND status = ? AND (vote_panel_message_id IS NULL OR vote_panel_message_id = 0)
                    """,
                    (
                        CASE_VOTING,
                        int(msg.id),
                        datetime_to_iso(deadline),
                        now_iso,
                        int(case["id"]),
                        CASE_ACTIVE_DISCUSSION,
                    ),
                )
            if cur.rowcount != 1:
                try:
                    await msg.delete()
                except Exception:
                    pass
                raise ValueError("本案投票面板已被其他操作创建或案件状态已变化。")
        except Exception:
            if msg is not None:
                try:
                    await msg.delete()
                except Exception:
                    pass
            raise

        return f"已创建监察案件 #{int(case['id'])} 投票面板；截止时间：{format_dt(deadline)}。"

    async def handle_vote_button(
        self,
        interaction: discord.Interaction,
        *,
        case_id: int,
        vote: str,
    ) -> str:
        normalized_vote = VOTE_YES if vote == VOTE_YES else VOTE_NO
        now_iso = utc_now_iso()
        should_settle = False
        expired = False
        async with self.db.lock:
            conn = self.db.require_conn()
            async with conn.execute("SELECT * FROM inspection_cases WHERE id = ?", (int(case_id),)) as cur_case:
                case_row = await cur_case.fetchone()
            if case_row is None:
                return "该投票不存在或按钮已过期。"
            case = dict(case_row)
            if case.get("status") != CASE_VOTING:
                return "该投票已经结束或案件状态已变化，按钮已过期。"
            deadline = parse_iso(case.get("vote_deadline_at"))
            if deadline is not None and deadline <= utc_now():
                expired = True
            else:
                async with conn.execute(
                    """
                    SELECT user_id FROM inspection_case_members
                    WHERE case_id = ? AND status = ?
                    ORDER BY id ASC
                    """,
                    (int(case_id), CASE_MEMBER_SELECTED),
                ) as cur_members:
                    selected_ids = [int(row["user_id"]) for row in await cur_members.fetchall()]
                if int(interaction.user.id) not in set(selected_ids):
                    return "只有本案临时监察成员可以投票。"

                await conn.execute(
                    """
                    INSERT INTO inspection_votes(case_id, guild_id, user_id, vote, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(case_id, user_id) DO UPDATE SET
                      vote = excluded.vote,
                      updated_at = excluded.updated_at
                    """,
                    (int(case_id), int(case["guild_id"]), int(interaction.user.id), normalized_vote, now_iso, now_iso),
                )
                async with conn.execute(
                    "SELECT COUNT(*) AS count FROM inspection_votes WHERE case_id = ?",
                    (int(case_id),),
                ) as cur_votes:
                    vote_count_row = await cur_votes.fetchone()
                should_settle = int(vote_count_row["count"] if vote_count_row else 0) >= len(selected_ids)

        if expired:
            await self.settle_vote(int(case_id), reason="投票截止时间已到。")
            return "该投票已到截止时间，按钮已过期。"

        if should_settle:
            await self.settle_vote(int(case_id), reason="所有临时监察成员均已投票。")
            return "已记录你的投票；所有成员均已投票，投票已结算。"
        return "已记录你的投票；截止前可再次点击按钮改票。"

    async def get_vote_counts(self, case_id: int) -> dict[str, int]:
        rows = await self.db.fetchall(
            "SELECT vote, COUNT(*) AS count FROM inspection_votes WHERE case_id = ? GROUP BY vote",
            (int(case_id),),
        )
        yes = 0
        no = 0
        for row in rows:
            if row.get("vote") == VOTE_YES:
                yes = int(row.get("count") or 0)
            elif row.get("vote") == VOTE_NO:
                no = int(row.get("count") or 0)
        return {"yes": yes, "no": no, "total": yes + no}

    async def process_voting_due_cases(self) -> None:
        rows = await self.db.fetchall(
            """
            SELECT * FROM inspection_cases
            WHERE status = ? AND vote_deadline_at IS NOT NULL AND vote_deadline_at <= ?
            """,
            (CASE_VOTING, utc_now_iso()),
        )
        for row in rows:
            try:
                await self.settle_vote(int(row["id"]), reason="投票截止时间已到。")
            except Exception as exc:
                await self.settings_service.send_admin_notice(
                    int(row["guild_id"]),
                    f"监察案件 #{int(row['id'])} 投票结算失败：{exc}",
                )

    async def settle_vote(self, case_id: int, *, reason: str) -> bool:
        now_iso = utc_now_iso()
        case: dict | None = None
        yes = 0
        no = 0
        absent = 0
        verdict = VERDICT_NO_MAJORITY
        invalid_selected_count: int | None = None
        notice_guild_id: int | None = None
        async with self.db.lock:
            conn = self.db.require_conn()
            async with conn.execute("SELECT * FROM inspection_cases WHERE id = ?", (int(case_id),)) as cur_case:
                case_row = await cur_case.fetchone()
            if case_row is None or case_row["status"] != CASE_VOTING:
                return False
            case = dict(case_row)

            async with conn.execute(
                """
                SELECT user_id FROM inspection_case_members
                WHERE case_id = ? AND status = ?
                ORDER BY id ASC
                """,
                (int(case_id), CASE_MEMBER_SELECTED),
            ) as cur_members:
                selected_ids = [int(row["user_id"]) for row in await cur_members.fetchall()]
            if len(selected_ids) < 3 or len(selected_ids) % 2 == 0:
                notice_guild_id = int(case_row["guild_id"])
                invalid_selected_count = len(selected_ids)
            else:
                async with conn.execute(
                    "SELECT vote, COUNT(*) AS count FROM inspection_votes WHERE case_id = ? GROUP BY vote",
                    (int(case_id),),
                ) as cur_votes:
                    vote_rows = await cur_votes.fetchall()
                yes = sum(int(row["count"] or 0) for row in vote_rows if row["vote"] == VOTE_YES)
                no = sum(int(row["count"] or 0) for row in vote_rows if row["vote"] == VOTE_NO)
                absent = max(0, len(selected_ids) - yes - no)
                if yes > no:
                    verdict = VERDICT_REASONABLE
                elif no > yes:
                    verdict = VERDICT_UNREASONABLE
                else:
                    verdict = VERDICT_NO_MAJORITY

                cur = await conn.execute(
                    """
                    UPDATE inspection_cases
                    SET status = ?, verdict = ?, closed_at = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (CASE_VERDICT_PUBLISHED, verdict, now_iso, now_iso, int(case_id), CASE_VOTING),
                )
                if cur.rowcount != 1:
                    return False

        if invalid_selected_count is not None:
            await self.settings_service.send_admin_notice(
                int(notice_guild_id or 0),
                f"监察案件 #{int(case_id)} 投票结算已暂停：当前临时监察成员人数为 {invalid_selected_count}，不是有效单数组。"
                "请管理员检查补抽/成员状态后重新处理。",
            )
            return False

        if case is None:
            return False

        public_text = (
            f"监察案件 #{int(case_id)} 裁决结果：{verdict}\n"
            f"案件说明：{trim_text(case.get('description'), 900)}\n"
            f"统计：诉求合理 {yes} 票 / 诉求不合理 {no} 票 / 未投票 {absent} 人\n"
            f"结算原因：{reason}"
        )
        if len(public_text) > 1900:
            public_text = (
                f"监察案件 #{int(case_id)} 裁决结果：{verdict}\n"
                f"案件说明：{trim_text(case.get('description'), 300)}\n"
                f"统计：诉求合理 {yes} 票 / 诉求不合理 {no} 票 / 未投票 {absent} 人\n"
                f"结算原因：{reason}"
            )
        verdict_channel = await self.settings_service.get_verdict_channel(int(case["guild_id"]))
        if verdict_channel is not None:
            try:
                await verdict_channel.send(public_text, allowed_mentions=discord.AllowedMentions.none())
            except Exception as exc:
                await self.settings_service.send_admin_notice(
                    int(case["guild_id"]),
                    f"监察案件 #{int(case_id)} 裁决公示发送失败：{exc}\n\n{public_text}",
                )
        else:
            await self.settings_service.send_admin_notice(
                int(case["guild_id"]),
                f"监察案件 #{int(case_id)} 裁决频道不可用，公示内容如下：\n{public_text}",
            )

        guild = self.bot.get_guild(int(case["guild_id"]))
        if guild is not None and case.get("discussion_channel_id"):
            channel = guild.get_channel(int(case["discussion_channel_id"]))
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(
                        f"本案投票已结束，裁决结果：{verdict}。统计：合理 {yes} / 不合理 {no} / 未投 {absent}。",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except Exception:
                    pass

        if verdict == VERDICT_NO_MAJORITY:
            await self.settings_service.send_admin_notice(
                int(case["guild_id"]),
                f"监察案件 #{int(case_id)} 投票平票，未形成多数，请管理员人工关注。",
            )
        return True
