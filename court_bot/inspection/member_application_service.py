"""Formal inspector applications. This module never uses candidate membership."""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import discord

from .database import InspectionDatabase
from .utils import utc_now, utc_now_iso

log = logging.getLogger(__name__)


class MemberApplicationRepo:
    def __init__(self, db: InspectionDatabase):
        self.db = db

    async def get_settings(self, guild_id: int) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM inspection_member_application_settings WHERE guild_id=?", (guild_id,))
        return row or {"guild_id": guild_id, "enabled": 0, "prerequisite_role_id": None,
                       "inspector_role_id": None, "approve_threshold": 1, "reject_threshold": 1,
                       "reject_cooldown_days": 1, "review_thread_id": None,
                       "pass_dm_template": None, "reject_dm_template": None}

    async def save_settings(self, guild_id: int, *, enabled: bool, prerequisite_role_id: int | None,
                            inspector_role_id: int | None, approve_threshold: int, reject_threshold: int,
                            reject_cooldown_days: int, review_thread_id: int | None,
                            pass_dm_template: str | None, reject_dm_template: str | None) -> dict[str, Any]:
        if min(approve_threshold, reject_threshold, reject_cooldown_days) < 1:
            raise ValueError("审核阈值和拒绝冷却天数至少为 1。")
        if enabled and not all((prerequisite_role_id, inspector_role_id, review_thread_id)):
            raise ValueError("启用时必须配置前置身份组、监察组身份组和审核子区。")
        now = utc_now_iso()
        await self.db.execute("""
            INSERT INTO inspection_member_application_settings
              (guild_id,enabled,prerequisite_role_id,inspector_role_id,approve_threshold,reject_threshold,
               reject_cooldown_days,review_thread_id,pass_dm_template,reject_dm_template,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET
              enabled=excluded.enabled, prerequisite_role_id=excluded.prerequisite_role_id,
              inspector_role_id=excluded.inspector_role_id, approve_threshold=excluded.approve_threshold,
              reject_threshold=excluded.reject_threshold, reject_cooldown_days=excluded.reject_cooldown_days,
              review_thread_id=excluded.review_thread_id, pass_dm_template=excluded.pass_dm_template,
              reject_dm_template=excluded.reject_dm_template, updated_at=excluded.updated_at
        """, (guild_id, int(enabled), prerequisite_role_id, inspector_role_id, approve_threshold,
                reject_threshold, reject_cooldown_days, review_thread_id, pass_dm_template,
                reject_dm_template, now, now))
        await self.db.commit()
        return await self.get_settings(guild_id)

    async def create_application(self, guild_id: int, user_id: int, display_name: str,
                                 reason: str, settings: dict[str, Any]) -> int:
        if not settings.get("enabled"):
            raise ValueError("当前暂停申请。")
        reason = (reason or "").strip()
        if not reason or len(reason) > 400:
            raise ValueError("申请理由必填，最多 400 字。")
        now = utc_now_iso()
        conn = self.db.require_conn()
        async with self.db.lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                async with conn.execute("""SELECT status,cooldown_until FROM inspection_member_applications
                    WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 1""", (guild_id, user_id)) as cur:
                    latest = await cur.fetchone()
                if latest and latest["status"] == "reviewing":
                    raise ValueError("你已有一份审核中的申请。")
                if latest and latest["status"] == "rejected" and latest["cooldown_until"] and latest["cooldown_until"] > now:
                    raise ValueError(f"拒绝冷却尚未结束：{latest['cooldown_until']}")
                snapshot = {key: settings.get(key) for key in (
                    "prerequisite_role_id", "inspector_role_id", "approve_threshold", "reject_threshold",
                    "reject_cooldown_days", "review_thread_id", "pass_dm_template", "reject_dm_template")}
                cur = await conn.execute("""INSERT INTO inspection_member_applications
                    (guild_id,user_id,display_name,reason,status,settings_snapshot_json,review_thread_id,submitted_at)
                    VALUES(?,?,?,?,?,?,?,?)""", (guild_id, user_id, display_name, reason, "reviewing",
                    json.dumps(snapshot, ensure_ascii=False), snapshot["review_thread_id"], now))
                app_id = int(cur.lastrowid)
                await cur.close()
                await conn.execute("""INSERT INTO inspection_member_application_events
                    (application_id,actor_id,event_type,created_at) VALUES(?,?,?,?)""",
                    (app_id, user_id, "submitted", now))
                await conn.commit()
                return app_id
            except Exception:
                await conn.rollback()
                raise

    async def get_application(self, app_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM inspection_member_applications WHERE id=?", (app_id,))
        if row:
            row["snapshot"] = json.loads(row["settings_snapshot_json"])
        return row

    async def latest_for_user(self, guild_id: int, user_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("""SELECT id FROM inspection_member_applications
            WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 1""", (guild_id, user_id))
        return await self.get_application(row["id"]) if row else None

    async def find_by_review_message(self, guild_id: int, message_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT id FROM inspection_member_applications WHERE guild_id=? AND review_message_id=?",
                                     (guild_id, message_id))
        return await self.get_application(row["id"]) if row else None

    async def list_votes(self, app_id: int) -> list[dict[str, Any]]:
        return await self.db.fetchall("SELECT * FROM inspection_member_application_votes WHERE application_id=? ORDER BY updated_at,reviewer_id", (app_id,))

    async def list_events(self, app_id: int) -> list[dict[str, Any]]:
        return await self.db.fetchall("SELECT * FROM inspection_member_application_events WHERE application_id=? ORDER BY id", (app_id,))

    async def vote(self, app_id: int, reviewer_id: int, choice: str, reason: str,
                   reviewer_name: str | None = None) -> dict[str, Any]:
        if choice not in ("同意", "拒绝"):
            raise ValueError("未知审核选择。")
        reason = (reason or "").strip()
        if choice == "拒绝" and not reason:
            raise ValueError("拒绝理由必须填写。")
        now = utc_now_iso()
        conn = self.db.require_conn()
        async with self.db.lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                async with conn.execute("SELECT * FROM inspection_member_applications WHERE id=?", (app_id,)) as cur:
                    app = await cur.fetchone()
                if not app or app["status"] != "reviewing":
                    raise ValueError("本次审核已结束。")
                if reviewer_id == app["user_id"]:
                    raise ValueError("不能审核自己的申请。")
                async with conn.execute("SELECT choice FROM inspection_member_application_votes WHERE application_id=? AND reviewer_id=?",
                                        (app_id, reviewer_id)) as cur:
                    old = await cur.fetchone()
                old_choice = old["choice"] if old else None
                await conn.execute("""INSERT INTO inspection_member_application_votes
                    (application_id,reviewer_id,reviewer_name,choice,reason,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(application_id,reviewer_id) DO UPDATE SET
                    reviewer_name=excluded.reviewer_name,choice=excluded.choice,reason=excluded.reason,
                    updated_at=excluded.updated_at""",
                    (app_id, reviewer_id, reviewer_name or str(reviewer_id), choice, reason, now, now))
                await conn.execute("""INSERT INTO inspection_member_application_events
                    (application_id,actor_id,event_type,old_choice,new_choice,reason,created_at)
                    VALUES(?,?,?,?,?,?,?)""", (app_id, reviewer_id, "vote_changed" if old else "vote_cast",
                                                old_choice, choice, reason, now))
                await conn.execute("UPDATE inspection_member_applications SET panel_dirty=1,panel_revision=panel_revision+1 WHERE id=?", (app_id,))
                counts = await self._counts(conn, app_id)
                snapshot = json.loads(app["settings_snapshot_json"])
                outcome = None
                if counts["同意"] >= snapshot["approve_threshold"]:
                    outcome = "approved"
                elif counts["拒绝"] >= snapshot["reject_threshold"]:
                    outcome = "rejected"
                if outcome:
                    cooldown = (utc_now() + timedelta(days=snapshot["reject_cooldown_days"])).isoformat() if outcome == "rejected" else None
                    await conn.execute("""UPDATE inspection_member_applications SET status=?,completed_at=?,cooldown_until=?,
                        role_grant_status=? WHERE id=? AND status='reviewing'""",
                        (outcome, now, cooldown, "pending" if outcome == "approved" else "not_required", app_id))
                    await conn.execute("""INSERT INTO inspection_member_application_events
                        (application_id,event_type,created_at) VALUES(?,?,?)""", (app_id, outcome, now))
                await conn.commit()
                return {"counts": counts, "completed": outcome, "old_choice": old_choice}
            except Exception:
                await conn.rollback()
                raise

    async def withdraw_vote(self, app_id: int, reviewer_id: int) -> bool:
        conn = self.db.require_conn()
        async with self.db.lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                async with conn.execute("SELECT status FROM inspection_member_applications WHERE id=?", (app_id,)) as cur:
                    app = await cur.fetchone()
                if not app or app["status"] != "reviewing":
                    raise ValueError("本次审核已结束。")
                async with conn.execute("SELECT choice,reason FROM inspection_member_application_votes WHERE application_id=? AND reviewer_id=?",
                                        (app_id, reviewer_id)) as cur:
                    old = await cur.fetchone()
                if old:
                    await conn.execute("DELETE FROM inspection_member_application_votes WHERE application_id=? AND reviewer_id=?", (app_id, reviewer_id))
                    await conn.execute("""INSERT INTO inspection_member_application_events
                        (application_id,actor_id,event_type,old_choice,reason,created_at) VALUES(?,?,?,?,?,?)""",
                        (app_id, reviewer_id, "vote_withdrawn", old["choice"], old["reason"], utc_now_iso()))
                    await conn.execute("UPDATE inspection_member_applications SET panel_dirty=1,panel_revision=panel_revision+1 WHERE id=?", (app_id,))
                await conn.commit()
                return old is not None
            except Exception:
                await conn.rollback()
                raise

    async def withdraw(self, app_id: int, user_id: int) -> bool:
        now = utc_now_iso()
        conn = self.db.require_conn()
        async with self.db.lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cur = await conn.execute("""UPDATE inspection_member_applications
                    SET status='withdrawn',completed_at=? WHERE id=? AND user_id=? AND status='reviewing'""",
                    (now, app_id, user_id))
                changed = cur.rowcount > 0
                await cur.close()
                if changed:
                    await conn.execute("""INSERT INTO inspection_member_application_events
                        (application_id,actor_id,event_type,created_at) VALUES(?,?,?,?)""",
                        (app_id, user_id, "withdrawn", now))
                    await conn.execute("UPDATE inspection_member_applications SET panel_dirty=1,panel_revision=panel_revision+1 WHERE id=?", (app_id,))
                await conn.commit()
                return changed
            except Exception:
                await conn.rollback()
                raise

    @staticmethod
    async def _counts(conn, app_id: int) -> dict[str, int]:
        async with conn.execute("""SELECT choice,COUNT(*) AS n FROM inspection_member_application_votes
            WHERE application_id=? GROUP BY choice""", (app_id,)) as cur:
            rows = await cur.fetchall()
        counts = {"同意": 0, "拒绝": 0}
        counts.update({row["choice"]: row["n"] for row in rows})
        return counts

    async def counts(self, app_id: int) -> dict[str, int]:
        return await self._counts(self.db.require_conn(), app_id)

    async def set_review_message(self, app_id: int, message_id: int) -> None:
        await self.db.execute("""UPDATE inspection_member_applications SET review_message_id=?
            WHERE id=? AND review_message_id IS NULL""", (message_id, app_id))
        await self.db.commit()

    async def mark_panel_synced(self, app_id: int, expected_revision: int | None = None) -> None:
        await self.db.execute("UPDATE inspection_member_applications SET panel_dirty=0 WHERE id=?" +
            (" AND panel_revision=?" if expected_revision is not None else ""),
            (app_id, expected_revision) if expected_revision is not None else (app_id,))
        await self.db.commit()

    async def pending_refresh(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("""SELECT id FROM inspection_member_applications
            WHERE review_message_id IS NOT NULL AND panel_dirty=1""")
        return [await self.get_application(row["id"]) for row in rows]

    async def claim_panel(self, app_id: int) -> str | None:
        now = utc_now()
        cur = await self.db.execute("""UPDATE inspection_member_applications SET panel_claimed_at=?
            WHERE id=? AND status='reviewing' AND
            (panel_claimed_at IS NULL OR panel_claimed_at<=?)""",
            (now.isoformat(), app_id, (now - timedelta(minutes=10)).isoformat()))
        changed = cur.rowcount > 0
        await cur.close()
        await self.db.commit()
        return now.isoformat() if changed else None

    async def release_panel(self, app_id: int, token: str) -> None:
        await self.db.execute("""UPDATE inspection_member_applications SET panel_claimed_at=NULL
            WHERE id=? AND panel_claimed_at=?""", (app_id, token))
        await self.db.commit()

    async def mark_reminder(self, app_id: int) -> bool:
        cur = await self.db.execute("""UPDATE inspection_member_applications SET reminder_sent=1
            WHERE id=? AND reminder_sent=0""", (app_id,))
        await self.db.commit()
        return cur.rowcount > 0

    async def claim_dm(self, app_id: int) -> bool:
        cur = await self.db.execute("""UPDATE inspection_member_applications SET dm_status='sending',dm_attempted_at=?
            WHERE id=? AND dm_status='pending'""", (utc_now_iso(), app_id))
        await self.db.commit()
        return cur.rowcount > 0

    async def set_dm_result(self, app_id: int, error: str | None) -> None:
        await self.db.execute("""UPDATE inspection_member_applications SET dm_status=?,dm_error=?,
            panel_dirty=1,panel_revision=panel_revision+1 WHERE id=?""",
                              ("failed" if error else "sent", error, app_id))
        if error:
            await self.add_event(app_id, None, "dm_failed", error)
        await self.db.commit()

    async def set_role_result(self, app_id: int, error: str | None) -> None:
        now = utc_now()
        cur = await self.db.execute("""UPDATE inspection_member_applications SET role_grant_status=?,
            role_grant_error=?,role_grant_attempted_at=?,role_grant_next_retry_at=?,
            panel_dirty=1,panel_revision=panel_revision+1 WHERE id=? AND status='approved'
            AND role_grant_status='sending'""",
            ("failed" if error else "success", error, now.isoformat(),
             (now + timedelta(minutes=10)).isoformat() if error else None, app_id))
        changed = cur.rowcount > 0
        await cur.close()
        if changed:
            await self.add_event(app_id, None, "role_grant_failed" if error else "role_grant_success", error)
        await self.db.commit()

    async def claim_role_grant(self, app_id: int) -> bool:
        now = utc_now()
        cur = await self.db.execute("""UPDATE inspection_member_applications SET role_grant_status='sending',
            role_grant_attempted_at=? WHERE id=? AND status='approved' AND
            (role_grant_status IN ('pending','failed') OR
             (role_grant_status='sending' AND role_grant_attempted_at<=?))""",
            (now.isoformat(), app_id, (now - timedelta(minutes=10)).isoformat()))
        changed = cur.rowcount > 0
        await cur.close()
        await self.db.commit()
        return changed

    async def add_event(self, app_id: int, actor_id: int | None, event_type: str, reason: str | None = None) -> None:
        await self.db.execute("""INSERT INTO inspection_member_application_events
            (application_id,actor_id,event_type,reason,created_at) VALUES(?,?,?,?,?)""",
            (app_id, actor_id, event_type, reason, utc_now_iso()))
        await self.db.commit()

    async def pending_panels(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("""SELECT id FROM inspection_member_applications WHERE status='reviewing'
            AND (review_message_id IS NULL OR reminder_sent=0)""")
        return [await self.get_application(row["id"]) for row in rows]

    async def pending_grants(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("""SELECT id FROM inspection_member_applications WHERE status='approved'
            AND ((role_grant_status IN ('pending','failed') AND
                  (role_grant_next_retry_at IS NULL OR role_grant_next_retry_at<=?))
              OR (role_grant_status='sending' AND role_grant_attempted_at<=?))""",
            (utc_now_iso(), (utc_now() - timedelta(minutes=10)).isoformat()))
        return [await self.get_application(row["id"]) for row in rows]

    async def pending_notifications(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("""SELECT id FROM inspection_member_applications
            WHERE status IN ('approved','rejected') AND dm_status='pending'""")
        return [await self.get_application(row["id"]) for row in rows]


class MemberApplicationService:
    def __init__(self, bot, repo: MemberApplicationRepo):
        self.bot = bot
        self.repo = repo

    @staticmethod
    def qualification_error(settings: dict, role_ids: set[int], latest: dict | None) -> str | None:
        if not settings.get("enabled"):
            return "当前暂停申请。"
        if int(settings.get("inspector_role_id") or 0) in role_ids:
            return "你已经是监察组成员，无需重复申请。"
        if int(settings.get("prerequisite_role_id") or 0) not in role_ids:
            return "你没有申请所需的前置身份组。"
        if latest and latest["status"] == "reviewing":
            return "你已有一份审核中的申请。"
        if latest and latest["status"] == "rejected" and latest.get("cooldown_until") and latest["cooldown_until"] > utc_now_iso():
            return f"拒绝冷却尚未结束：{latest['cooldown_until']}"
        return None

    def entry_view(self):
        from .member_application_views import MemberApplicationEntryView
        return MemberApplicationEntryView(self)

    def review_view(self, disabled: bool = False):
        from .member_application_views import MemberApplicationReviewView
        return MemberApplicationReviewView(self, disabled=disabled)

    async def get_thread(self, thread_id: int) -> discord.Thread | None:
        channel = self.bot.get_channel(int(thread_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(thread_id))
            except Exception:
                return None
        return channel if isinstance(channel, discord.Thread) else None

    async def open_application(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        settings = await self.repo.get_settings(interaction.guild.id)
        latest = await self.repo.latest_for_user(interaction.guild.id, interaction.user.id)
        block = self.qualification_error(settings, {role.id for role in interaction.user.roles}, latest)
        if block:
            await interaction.response.send_message(block, ephemeral=True)
            return
        from .member_application_views import ApplicationReasonModal
        await interaction.response.send_modal(ApplicationReasonModal(self))

    async def submit(self, interaction: discord.Interaction, reason: str) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        settings = await self.repo.get_settings(interaction.guild.id)
        latest = await self.repo.latest_for_user(interaction.guild.id, interaction.user.id)
        block = self.qualification_error(settings, {role.id for role in interaction.user.roles}, latest)
        if block:
            await interaction.response.send_message(block, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        app_id = None
        try:
            app_id = await self.repo.create_application(interaction.guild.id, interaction.user.id,
                interaction.user.display_name[:100], reason, settings)
            await self.ensure_panel(await self.repo.get_application(app_id))
            await interaction.edit_original_response(content=f"监察组申请 #{app_id} 已保存，正在审核。")
        except ValueError as exc:
            await interaction.edit_original_response(content=str(exc))
        except Exception as exc:
            log.exception("Inspection member application submission follow-up failed")
            if app_id is not None:
                await self.repo.add_event(app_id, None, "panel_send_failed", str(exc)[:400])
            await interaction.edit_original_response(content=f"申请已保存；审核子区暂不可用，将自动恢复：{str(exc)[:150]}")

    async def show_mine(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        app = await self.repo.latest_for_user(interaction.guild.id, interaction.user.id)
        if not app:
            await interaction.response.send_message("你还没有监察组申请。", ephemeral=True)
            return
        label = {"reviewing": "审核中", "approved": "已通过", "rejected": "已拒绝", "withdrawn": "已撤回"}.get(app["status"], app["status"])
        await interaction.response.send_message(f"监察组申请 #{app['id']}：{label}", ephemeral=True)

    async def withdraw(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        app = await self.repo.latest_for_user(interaction.guild.id, interaction.user.id)
        if not app or not await self.repo.withdraw(app["id"], interaction.user.id):
            await interaction.response.send_message("当前没有审核中的申请可撤回。", ephemeral=True)
            return
        await self.refresh_panel(app["id"])
        await interaction.response.send_message("申请已撤回，可以立即重新申请。", ephemeral=True)

    async def ensure_panel(self, app: dict | None) -> None:
        if not app or app["status"] != "reviewing":
            return
        token = await self.repo.claim_panel(app["id"])
        if not token:
            return
        try:
            await self._ensure_panel_impl(app)
        finally:
            await self.repo.release_panel(app["id"], token)

    async def _ensure_panel_impl(self, app: dict) -> None:
        thread = await self.get_thread(app["review_thread_id"])
        if thread is None:
            raise ValueError("审核子区暂不可用。")
        if thread.archived:
            raise ValueError("审核子区已归档。")
        from .member_application_embeds import review_embed
        message_id = app.get("review_message_id")
        if not message_id:
            # Recover a send that succeeded just before a process restart.
            async for message in thread.history(limit=None):
                if message.author.id == self.bot.user.id and message.embeds and any(
                    (embed.footer.text or "") == f"Inspection Member Application ID: {app['id']}" for embed in message.embeds):
                    message_id = message.id
                    break
        if not message_id:
            message = await thread.send(embed=review_embed(app, [], {"同意": 0, "拒绝": 0}),
                                        view=self.review_view(), nonce=f"im-panel-{app['id']}",
                                        allowed_mentions=discord.AllowedMentions.none())
            message_id = message.id
        await self.repo.set_review_message(app["id"], message_id)
        if not app["reminder_sent"]:
            marker = f"有新的监察组申请待审核（#{app['id']}）"
            already = False
            async for message in thread.history(limit=None):
                if message.author.id == self.bot.user.id and marker in (message.content or ""):
                    already = True
                    break
            if not already:
                await thread.send(f"<@&{app['snapshot']['inspector_role_id']}> {marker}",
                                  nonce=f"im-ping-{app['id']}",
                                  allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False))
            await self.repo.mark_reminder(app["id"])

    async def refresh_panel(self, app_id: int) -> None:
        app = await self.repo.get_application(app_id)
        if not app or not app.get("review_message_id"):
            return
        thread = await self.get_thread(app["review_thread_id"])
        if thread is None:
            return
        from .member_application_embeds import review_embed
        try:
            message = await thread.fetch_message(app["review_message_id"])
            await message.edit(embed=review_embed(app, await self.repo.list_votes(app_id), await self.repo.counts(app_id)),
                               view=self.review_view(disabled=app["status"] != "reviewing"),
                               allowed_mentions=discord.AllowedMentions.none())
            await self.repo.mark_panel_synced(app_id, app["panel_revision"])
        except Exception as exc:
            log.warning("Cannot refresh inspector review panel %s: %s", app_id, exc)
            await self.repo.add_event(app_id, None, "panel_refresh_failed", str(exc)[:400])

    async def _review_app(self, interaction: discord.Interaction, app_id: int | None = None) -> tuple[dict | None, str | None]:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return None, "请在服务器内使用。"
        app = await (self.repo.get_application(app_id) if app_id else self.repo.find_by_review_message(
            interaction.guild.id, interaction.message.id if interaction.message else 0))
        if not app or app["guild_id"] != interaction.guild.id:
            return None, "无法定位本次申请。"
        if app["status"] != "reviewing":
            return None, "本次审核已结束。"
        if interaction.user.bot or interaction.user.id == app["user_id"]:
            return None, "申请人或 Bot 不能审核本次申请。"
        if int(app["snapshot"]["inspector_role_id"]) not in {role.id for role in interaction.user.roles}:
            return None, "你当前没有本次审核所需的监察组身份组。"
        return app, None

    async def review_button(self, interaction: discord.Interaction, action: str) -> None:
        app, error = await self._review_app(interaction)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        if action in ("approve", "reject"):
            from .member_application_views import ReviewReasonModal
            await interaction.response.send_modal(ReviewReasonModal(self, "同意" if action == "approve" else "拒绝", app["id"]))
            return
        if action != "remove":
            await interaction.response.send_message("未知审核操作。", ephemeral=True)
            return
        try:
            changed = await self.repo.withdraw_vote(app["id"], interaction.user.id)
            await self.refresh_panel(app["id"])
            await interaction.response.send_message("已撤销投票。" if changed else "你尚未投票。", ephemeral=True)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)

    async def cast_vote(self, interaction: discord.Interaction, app_id: int, choice: str, reason: str) -> None:
        app, error = await self._review_app(interaction, app_id)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.repo.vote(app_id, interaction.user.id, choice, reason,
                                          interaction.user.display_name[:100])
            await self.refresh_panel(app_id)
            if result["completed"]:
                await self.process_result(await self.repo.get_application(app_id))
            await interaction.edit_original_response(content="审核意见已保存。")
        except ValueError as exc:
            await interaction.edit_original_response(content=str(exc))

    async def process_result(self, app: dict | None) -> None:
        if not app or app["status"] not in ("approved", "rejected"):
            return
        if await self.repo.claim_dm(app["id"]):
            try:
                user = self.bot.get_user(app["user_id"]) or await self.bot.fetch_user(app["user_id"])
                if app["status"] == "approved":
                    content = app["snapshot"].get("pass_dm_template") or "你的监察组正式成员申请已通过。"
                else:
                    content = app["snapshot"].get("reject_dm_template") or "你的监察组正式成员申请未通过。"
                    reasons = [vote["reason"] for vote in await self.repo.list_votes(app["id"])
                               if vote["choice"] == "拒绝" and vote["reason"]]
                    if reasons:
                        content += "\n\n拒绝理由：\n" + "\n".join(f"{i}. {reason}" for i, reason in enumerate(reasons, 1))
                await user.send(content[:2000], allowed_mentions=discord.AllowedMentions.none())
                await self.repo.set_dm_result(app["id"], None)
            except Exception as exc:
                await self.repo.set_dm_result(app["id"], str(exc)[:400])
        if app["status"] == "approved" and app["role_grant_status"] != "success":
            await self.grant_role(app)
        await self.refresh_panel(app["id"])

    async def grant_role(self, app: dict) -> None:
        if not await self.repo.claim_role_grant(app["id"]):
            return
        try:
            guild = self.bot.get_guild(app["guild_id"])
            if guild is None:
                raise ValueError("服务器暂不可用")
            role = guild.get_role(int(app["snapshot"]["inspector_role_id"]))
            if role is None:
                raise ValueError("目标身份组不存在")
            member = guild.get_member(app["user_id"]) or await guild.fetch_member(app["user_id"])
            if role not in member.roles:
                me = guild.me
                if me is None or not me.guild_permissions.manage_roles or me.top_role <= role:
                    raise ValueError("Bot 缺少 Manage Roles 或身份组层级不足")
                await member.add_roles(role, reason=f"监察组正式成员申请 #{app['id']} 审核通过")
            await self.repo.set_role_result(app["id"], None)
        except Exception as exc:
            await self.repo.set_role_result(app["id"], str(exc)[:400])
        await self.refresh_panel(app["id"])

    async def maintenance(self) -> None:
        for app in await self.repo.pending_panels():
            try:
                await self.ensure_panel(app)
            except Exception as exc:
                log.exception("Cannot restore inspector review panel %s", app["id"])
                await self.repo.add_event(app["id"], None, "panel_send_failed", str(exc)[:400])
        for app in await self.repo.pending_notifications():
            await self.process_result(app)
        for app in await self.repo.pending_grants():
            await self.grant_role(app)
        for app in await self.repo.pending_refresh():
            await self.refresh_panel(app["id"])
