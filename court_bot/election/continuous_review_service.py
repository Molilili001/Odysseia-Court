"""Optional, snapshot based qualification review before public continuous voting."""
from __future__ import annotations

import json
import logging
import asyncio
import io
import re
from datetime import timedelta
from typing import Any

import discord

from .continuous_constants import (
    CONT_APP_REVIEW_AREA_PENDING, CONT_APP_REVIEWING, CONT_APP_REVIEW_TIMEOUT,
    CONT_APP_REVIEW_PUBLISH_PENDING, CONT_APP_REVIEW_REJECTED, CONT_APP_VOTING,
    CONT_APP_WITHDRAWN, CONT_MODE_SUPPORT,
)
from .continuous_database import ContinuousApplicationRepo
from .time_utils import parse_iso, utc_now, utc_now_iso

log = logging.getLogger(__name__)


class ContinuousReviewRepo:
    def __init__(self, continuous_repo: ContinuousApplicationRepo):
        self.continuous = continuous_repo
        self.db = continuous_repo.db
        self.lock = continuous_repo.lock

    async def get_settings(self, config_id: int) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM pe_continuous_review_settings WHERE config_id=?", (config_id,))
        if not row:
            return {"config_id": config_id, "enabled": 0}
        value = dict(row)
        value["reviewer_role_ids"] = json.loads(value["reviewer_role_ids"])
        return value

    async def save_settings(self, config_id: int, *, enabled: bool, approve_threshold: int,
                            reject_threshold: int, reviewer_role_ids: list[int], review_channel_id: int,
                            pass_dm_template: str, reject_dm_template: str, archive_channel_id: int) -> dict[str, Any]:
        if min(approve_threshold, reject_threshold) < 1:
            raise ValueError("同意和拒绝阈值都必须至少为 1。")
        if enabled and not all((reviewer_role_ids, review_channel_id, pass_dm_template and pass_dm_template.strip(),
                                reject_dm_template and reject_dm_template.strip(), archive_channel_id)):
            raise ValueError("启用审核时必须填写身份组、频道、两个私信模板和归档位置。")
        await self.db.execute_close("""INSERT INTO pe_continuous_review_settings
            (config_id,enabled,approve_threshold,reject_threshold,reviewer_role_ids,review_channel_id,
             pass_dm_template,reject_dm_template,archive_channel_id,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(config_id) DO UPDATE SET enabled=excluded.enabled,
            approve_threshold=excluded.approve_threshold,reject_threshold=excluded.reject_threshold,
            reviewer_role_ids=excluded.reviewer_role_ids,review_channel_id=excluded.review_channel_id,
            pass_dm_template=excluded.pass_dm_template,reject_dm_template=excluded.reject_dm_template,
            archive_channel_id=excluded.archive_channel_id,updated_at=excluded.updated_at""",
            (config_id, int(enabled), approve_threshold, reject_threshold, json.dumps(reviewer_role_ids),
             review_channel_id, pass_dm_template, reject_dm_template, archive_channel_id, utc_now_iso()))
        return await self.get_settings(config_id)

    async def get_review(self, app_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM pe_continuous_reviews WHERE application_id=?", (app_id,))
        if row:
            value = dict(row)
            value["snapshot"] = json.loads(value["snapshot_json"])
            return value
        return None

    async def mark_panel_synced(self, app_id: int, expected_revision: int | None = None) -> None:
        await self.db.execute_close("UPDATE pe_continuous_reviews SET panel_dirty=0 WHERE application_id=?" +
            (" AND panel_revision=?" if expected_revision is not None else ""),
            (app_id, expected_revision) if expected_revision is not None else (app_id,))

    async def dirty_panels(self) -> list[int]:
        rows = await self.db.fetchall("SELECT application_id FROM pe_continuous_reviews WHERE panel_message_id IS NOT NULL AND panel_dirty=1")
        return [row["application_id"] for row in rows]

    @staticmethod
    async def _dirty(conn, app_id: int) -> None:
        await conn.execute("""UPDATE pe_continuous_reviews SET panel_dirty=1,
            panel_revision=panel_revision+1 WHERE application_id=?""", (app_id,))

    async def get_by_panel(self, guild_id: int, message_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("""SELECT r.application_id FROM pe_continuous_reviews r
            JOIN pe_continuous_applications a ON a.id=r.application_id
            WHERE a.guild_id=? AND r.panel_message_id=?""", (guild_id, message_id))
        return await self.get_review(row["application_id"]) if row else None

    async def set_thread(self, app_id: int, thread_id: int) -> None:
        await self.db.execute_close("UPDATE pe_continuous_reviews SET thread_id=? WHERE application_id=? AND thread_id IS NULL",
                                    (thread_id, app_id))

    async def claim_work(self, app_id: int, kind: str) -> str | None:
        column = {"thread": "thread_claimed_at", "publish": "publish_claimed_at",
                  "archive": "archive_claimed_at", "log": "thread_log_claimed_at"}.get(kind)
        if column is None:
            raise ValueError("未知后台任务。")
        now = utc_now()
        async with self.lock:
            cur = await self.db.conn.execute(f"""UPDATE pe_continuous_reviews SET {column}=?
                WHERE application_id=? AND ({column} IS NULL OR {column}<=?)""",
                (now.isoformat(), app_id, (now - timedelta(minutes=10)).isoformat()))
            changed = cur.rowcount > 0
            await cur.close()
            await self.db.conn.commit()
        return now.isoformat() if changed else None

    async def release_work(self, app_id: int, kind: str, token: str | None = None) -> None:
        column = {"thread": "thread_claimed_at", "publish": "publish_claimed_at",
                  "archive": "archive_claimed_at", "log": "thread_log_claimed_at"}.get(kind)
        if column is None:
            raise ValueError("未知后台任务。")
        async with self.lock:
            await self.db.conn.execute(f"UPDATE pe_continuous_reviews SET {column}=NULL WHERE application_id=?" +
                (f" AND {column}=?" if token else ""), (app_id, token) if token else (app_id,))
            await self.db.conn.commit()

    async def start_review(self, app_id: int, thread_id: int, panel_message_id: int) -> bool:
        now = utc_now()
        async with self.lock:
            try:
                await self.db.conn.execute("BEGIN IMMEDIATE")
                cur = await self.db.conn.execute("""UPDATE pe_continuous_applications SET status=?,updated_at=?
                    WHERE id=? AND status=?""", (CONT_APP_REVIEWING, now.isoformat(), app_id, CONT_APP_REVIEW_AREA_PENDING))
                changed = cur.rowcount > 0
                await cur.close()
                if changed:
                    await self.db.conn.execute("""UPDATE pe_continuous_reviews SET thread_id=?,panel_message_id=?,
                        review_started_at=?,review_deadline_at=? WHERE application_id=?""",
                        (thread_id, panel_message_id, now.isoformat(), (now + timedelta(hours=48)).isoformat(), app_id))
                    await self._event(self.db.conn, app_id, None, "review_started", detail={"thread_id": thread_id})
                await self.db.conn.commit()
            except Exception:
                await self.db.conn.rollback()
                raise
        return changed

    async def mark_reminder(self, app_id: int) -> None:
        await self.db.execute_close("UPDATE pe_continuous_reviews SET reminder_sent=1 WHERE application_id=?", (app_id,))

    async def votes(self, app_id: int) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM pe_continuous_review_votes WHERE application_id=? ORDER BY updated_at,reviewer_id", (app_id,))
        return [dict(row) for row in rows]

    async def unlogged_thread_events(self, app_id: int) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("""SELECT e.* FROM pe_continuous_review_events e
            JOIN pe_continuous_reviews r ON r.application_id=e.application_id
            LEFT JOIN pe_continuous_review_thread_logs l ON l.event_id=e.id
            WHERE e.application_id=? AND e.event_type IN
              ('vote_cast','vote_changed','vote_withdrawn','manual_approve','manual_reject','withdrawn')
              AND (e.event_type!='withdrawn' OR r.archive_locked=0)
              AND l.event_id IS NULL ORDER BY e.id""", (app_id,))
        return [dict(row) for row in rows]

    async def mark_thread_event_logged(self, event_id: int, message_id: int) -> None:
        await self.db.execute_close("""INSERT OR IGNORE INTO pe_continuous_review_thread_logs(event_id,message_id)
            VALUES(?,?)""", (event_id, message_id))

    async def events(self, app_id: int) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM pe_continuous_review_events WHERE application_id=? ORDER BY id", (app_id,))
        return [dict(row) for row in rows]

    @staticmethod
    async def _event(conn, app_id: int, actor_id: int | None, event_type: str,
                     old_choice: str | None = None, new_choice: str | None = None,
                     reason: str | None = None, detail: dict | None = None) -> None:
        await conn.execute("""INSERT INTO pe_continuous_review_events
            (application_id,actor_id,event_type,old_choice,new_choice,reason,detail_json,created_at)
            VALUES(?,?,?,?,?,?,?,?)""", (app_id, actor_id, event_type, old_choice, new_choice,
                                        reason, json.dumps(detail or {}, ensure_ascii=False), utc_now_iso()))

    async def add_event(self, app_id: int, actor_id: int | None, event_type: str,
                        reason: str | None = None, detail: dict | None = None) -> None:
        await self._event(self.db.conn, app_id, actor_id, event_type, reason=reason, detail=detail)
        await self.db.conn.commit()

    @staticmethod
    async def _counts(conn, app_id: int) -> dict[str, int]:
        async with conn.execute("""SELECT choice,COUNT(*) AS n FROM pe_continuous_review_votes
            WHERE application_id=? GROUP BY choice""", (app_id,)) as cur:
            rows = await cur.fetchall()
        counts = {"yes": 0, "no": 0}
        counts.update({row["choice"]: row["n"] for row in rows})
        return counts

    async def counts(self, app_id: int) -> dict[str, int]:
        return await self._counts(self.db.conn, app_id)

    async def vote(self, app_id: int, reviewer_id: int, choice: str, reason: str,
                   reviewer_name: str | None = None) -> dict[str, Any]:
        if choice not in ("yes", "no"):
            raise ValueError("未知审核选项。")
        reason = (reason or "").strip()
        if choice == "no" and not reason:
            raise ValueError("拒绝理由必须填写。")
        conn = self.db.conn
        async with self.lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                async with conn.execute("""SELECT a.user_id,a.status,r.snapshot_json,r.review_deadline_at,r.threshold_choice
                    FROM pe_continuous_applications a JOIN pe_continuous_reviews r ON a.id=r.application_id
                    WHERE a.id=?""", (app_id,)) as cur:
                    review = await cur.fetchone()
                if not review or review["status"] != CONT_APP_REVIEWING:
                    raise ValueError("本次审核已结束。")
                if review["threshold_choice"]:
                    await conn.rollback()
                    return {"threshold_choice": None, "locked": True}
                if review["review_deadline_at"] <= utc_now_iso():
                    raise ValueError("审核已到期。")
                if reviewer_id == review["user_id"]:
                    raise ValueError("不能审核自己的申请。")
                async with conn.execute("SELECT choice FROM pe_continuous_review_votes WHERE application_id=? AND reviewer_id=?",
                                        (app_id, reviewer_id)) as cur:
                    old = await cur.fetchone()
                old_choice = old["choice"] if old else None
                now = utc_now_iso()
                await conn.execute("""INSERT INTO pe_continuous_review_votes
                    (application_id,reviewer_id,reviewer_name,choice,reason,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(application_id,reviewer_id) DO UPDATE SET
                    reviewer_name=excluded.reviewer_name,choice=excluded.choice,reason=excluded.reason,
                    updated_at=excluded.updated_at""",
                    (app_id, reviewer_id, reviewer_name or str(reviewer_id), choice, reason, now, now))
                await self._event(conn, app_id, reviewer_id, "vote_changed" if old else "vote_cast",
                                  old_choice, choice, reason,
                                  {"reviewer_name": reviewer_name or str(reviewer_id)})
                counts = await self._counts(conn, app_id)
                snapshot = json.loads(review["snapshot_json"])
                threshold_choice = None
                if counts["yes"] >= snapshot["approve_threshold"]:
                    threshold_choice = "yes"
                elif counts["no"] >= snapshot["reject_threshold"]:
                    threshold_choice = "no"
                if threshold_choice:
                    await conn.execute("UPDATE pe_continuous_reviews SET threshold_choice=? WHERE application_id=? AND threshold_choice IS NULL",
                                       (threshold_choice, app_id))
                await self._dirty(conn, app_id)
                await conn.commit()
                return {"threshold_choice": threshold_choice, "old_choice": old_choice, "counts": counts}
            except Exception:
                await conn.rollback()
                raise

    async def withdraw_vote(self, app_id: int, reviewer_id: int) -> dict | None:
        conn = self.db.conn
        async with self.lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                async with conn.execute("""SELECT a.status,r.review_deadline_at,r.threshold_choice FROM pe_continuous_applications a
                    JOIN pe_continuous_reviews r ON a.id=r.application_id WHERE a.id=?""", (app_id,)) as cur:
                    review = await cur.fetchone()
                if not review or review["status"] != CONT_APP_REVIEWING or review["threshold_choice"]:
                    raise ValueError("本次审核已结束。")
                if review["review_deadline_at"] <= utc_now_iso():
                    raise ValueError("审核已到期。")
                async with conn.execute("SELECT reviewer_name,choice,reason FROM pe_continuous_review_votes WHERE application_id=? AND reviewer_id=?",
                                        (app_id, reviewer_id)) as cur:
                    old = await cur.fetchone()
                if old:
                    await conn.execute("DELETE FROM pe_continuous_review_votes WHERE application_id=? AND reviewer_id=?", (app_id, reviewer_id))
                    await self._event(conn, app_id, reviewer_id, "vote_withdrawn", old["choice"],
                                      reason=old["reason"], detail={"reviewer_name": old["reviewer_name"]})
                    await self._dirty(conn, app_id)
                await conn.commit()
                return dict(old) if old else None
            except Exception:
                await conn.rollback()
                raise

    async def settle(self, app_id: int, *, manual_choice: str | None = None,
                     operator_id: int | None = None, reason: str | None = None) -> bool:
        conn = self.db.conn
        async with self.lock:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                async with conn.execute("""SELECT a.status,a.config_id,r.threshold_choice FROM pe_continuous_applications a
                    JOIN pe_continuous_reviews r ON a.id=r.application_id WHERE a.id=?""", (app_id,)) as cur:
                    row = await cur.fetchone()
                if not row or row["status"] not in ((CONT_APP_REVIEWING, CONT_APP_REVIEW_TIMEOUT) if manual_choice else (CONT_APP_REVIEWING,)):
                    await conn.rollback()
                    return False
                if manual_choice and row["threshold_choice"] and manual_choice != row["threshold_choice"]:
                    await conn.rollback()
                    return False
                choice = row["threshold_choice"] or manual_choice
                if choice not in ("yes", "no"):
                    await conn.rollback()
                    return False
                async with conn.execute("SELECT cooldown_minutes FROM pe_continuous_configs WHERE id=?", (row["config_id"],)) as cur:
                    config = await cur.fetchone()
                now = utc_now()
                status = CONT_APP_REVIEW_PUBLISH_PENDING if choice == "yes" else CONT_APP_REVIEW_REJECTED
                cooldown = (now + timedelta(minutes=config["cooldown_minutes"])).isoformat() if choice == "no" else None
                cur = await conn.execute("""UPDATE pe_continuous_applications SET status=?,reapply_locked=?,
                    cooldown_until=?,closed_at=CASE WHEN ?='no' THEN ? ELSE NULL END,updated_at=?
                    WHERE id=? AND status=?""", (status, int(choice == "no"), cooldown, choice,
                                               now.isoformat(), now.isoformat(), app_id, row["status"]))
                changed = cur.rowcount > 0
                await cur.close()
                if changed:
                    await conn.execute("""UPDATE pe_continuous_reviews SET outcome=?,completed_at=? WHERE application_id=?""",
                                       (choice, now.isoformat(), app_id))
                    await self._dirty(conn, app_id)
                    await self._event(conn, app_id, operator_id, "manual_approve" if manual_choice == "yes" else
                                      "manual_reject" if manual_choice == "no" else "approved" if choice == "yes" else "rejected",
                                      reason=reason)
                await conn.commit()
                return changed
            except Exception:
                await conn.rollback()
                raise

    async def timeout_due(self) -> list[int]:
        rows = await self.db.fetchall("""SELECT r.application_id FROM pe_continuous_reviews r
            JOIN pe_continuous_applications a ON a.id=r.application_id
            WHERE a.status=? AND r.threshold_choice IS NULL AND r.review_deadline_at<=?""",
            (CONT_APP_REVIEWING, utc_now_iso()))
        ids = []
        for row in rows:
            async with self.lock:
                cur = await self.db.conn.execute("""UPDATE pe_continuous_applications SET status=?,updated_at=?
                    WHERE id=? AND status=?""", (CONT_APP_REVIEW_TIMEOUT, utc_now_iso(), row["application_id"], CONT_APP_REVIEWING))
                if cur.rowcount:
                    ids.append(row["application_id"])
                    await self._event(self.db.conn, row["application_id"], None, "timed_out")
                    await self._dirty(self.db.conn, row["application_id"])
                await cur.close()
                await self.db.conn.commit()
        return ids

    async def withdraw(self, app_id: int, user_id: int, cooldown_until: str) -> bool:
        async with self.lock:
            cur = await self.db.conn.execute("""UPDATE pe_continuous_applications
                SET status=?,cooldown_until=?,closed_at=?,updated_at=? WHERE id=? AND user_id=?
                AND status IN (?,?,?,?)
                AND (status!=? OR NOT EXISTS (
                    SELECT 1 FROM pe_continuous_reviews r WHERE r.application_id=? AND r.threshold_choice IS NOT NULL
                ))""",
                (CONT_APP_WITHDRAWN, cooldown_until, utc_now_iso(), utc_now_iso(), app_id, user_id,
                 CONT_APP_REVIEW_AREA_PENDING, CONT_APP_REVIEWING, CONT_APP_REVIEW_TIMEOUT,
                 CONT_APP_REVIEW_PUBLISH_PENDING, CONT_APP_REVIEWING, app_id))
            changed = cur.rowcount > 0
            await cur.close()
            if changed:
                await self.db.conn.execute("UPDATE pe_continuous_reviews SET outcome='withdrawn',completed_at=? WHERE application_id=?",
                                           (utc_now_iso(), app_id))
                await self._event(self.db.conn, app_id, user_id, "withdrawn")
                await self._dirty(self.db.conn, app_id)
            await self.db.conn.commit()
        return changed

    async def unlock_reapplication(self, app_id: int) -> bool:
        async with self.lock:
            cur = await self.db.conn.execute("""UPDATE pe_continuous_applications SET reapply_locked=0,cooldown_until=NULL
                WHERE id=? AND status=? AND reapply_locked=1""", (app_id, CONT_APP_REVIEW_REJECTED))
            changed = cur.rowcount > 0
            await cur.close()
            if changed:
                await self._event(self.db.conn, app_id, None, "reapplication_unlocked")
            await self.db.conn.commit()
        return changed

    async def public_vote_published(self, app_id: int, channel_id: int, message_id: int,
                                    duration_minutes: int) -> bool:
        async with self.lock:
            now = utc_now()
            cur = await self.db.conn.execute("""UPDATE pe_continuous_applications SET status=?,vote_channel_id=?,
                vote_message_id=?,voting_end_at=?,updated_at=? WHERE id=? AND status=? AND vote_message_id IS NULL""",
                (CONT_APP_VOTING, channel_id, message_id, (now + timedelta(minutes=duration_minutes)).isoformat(),
                 now.isoformat(), app_id, CONT_APP_REVIEW_PUBLISH_PENDING))
            changed = cur.rowcount > 0
            await cur.close()
            if changed:
                await self._event(self.db.conn, app_id, None, "public_vote_published", detail={"message_id": message_id})
                await self._dirty(self.db.conn, app_id)
            await self.db.conn.commit()
        return changed

    async def mark_dm(self, app_id: int, error: str | None) -> bool:
        async with self.lock:
            cur = await self.db.conn.execute("""UPDATE pe_continuous_reviews SET dm_status=?,dm_error=?
                WHERE application_id=? AND dm_status='sending'""", ("failed" if error else "sent", error, app_id))
            changed = cur.rowcount > 0
            await cur.close()
            if changed:
                await self._event(self.db.conn, app_id, None, "dm_failed" if error else "dm_sent", reason=error)
                await self._dirty(self.db.conn, app_id)
            await self.db.conn.commit()
        return changed

    async def claim_dm(self, app_id: int) -> bool:
        async with self.lock:
            cur = await self.db.conn.execute("""UPDATE pe_continuous_reviews SET dm_status='sending'
                WHERE application_id=? AND dm_status='pending'""", (app_id,))
            changed = cur.rowcount > 0
            await cur.close()
            await self.db.conn.commit()
        return changed

    async def mark_archive(self, app_id: int, message_id: int) -> None:
        await self.db.execute_close("""UPDATE pe_continuous_reviews SET archive_message_id=?,archive_error=NULL,
            panel_dirty=1,panel_revision=panel_revision+1 WHERE application_id=? AND archive_message_id IS NULL""",
                                    (message_id, app_id))

    async def mark_archive_locked(self, app_id: int) -> None:
        await self.db.execute_close("""UPDATE pe_continuous_reviews SET archive_locked=1,archive_error=NULL
            WHERE application_id=? AND archive_message_id IS NOT NULL""",
                                    (app_id,))

    async def archive_error(self, app_id: int, error: str) -> None:
        await self.db.execute_close("UPDATE pe_continuous_reviews SET archive_error=? WHERE application_id=?", (error[:400], app_id))
        await self._dirty(self.db.conn, app_id)
        await self.db.conn.commit()
        await self.add_event(app_id, None, "archive_failed", error[:400])

    async def clear_archive_error(self, app_id: int) -> None:
        await self.db.execute_close("""UPDATE pe_continuous_reviews SET archive_error=NULL,
            panel_dirty=1,panel_revision=panel_revision+1 WHERE application_id=? AND archive_error IS NOT NULL""",
            (app_id,))

    async def pending(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("""SELECT a.id FROM pe_continuous_applications a
            JOIN pe_continuous_reviews r ON r.application_id=a.id
            WHERE a.status IN (?,?,?,?) OR (a.status IN (?,?) AND r.archive_locked=0)
            ORDER BY a.id""", (CONT_APP_REVIEW_AREA_PENDING, CONT_APP_REVIEWING,
                CONT_APP_REVIEW_TIMEOUT, CONT_APP_REVIEW_PUBLISH_PENDING,
                CONT_APP_REVIEW_REJECTED, CONT_APP_WITHDRAWN))
        return [await self.continuous.get_application(row["id"]) for row in rows]


class ContinuousReviewService:
    def __init__(self, bot, continuous_service, repo: ContinuousReviewRepo):
        self.bot = bot
        self.continuous = continuous_service
        self.repo = repo
        self._vote_locks: dict[int, asyncio.Lock] = {}

    def view(self, disabled: bool = False):
        from .continuous_review_views import ContinuousReviewView
        return ContinuousReviewView(self, disabled=disabled)

    async def _get_channel(self, channel_id: int):
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except Exception:
                return None
        return channel

    async def ensure_thread(self, app: dict) -> None:
        if app["status"] != CONT_APP_REVIEW_AREA_PENDING:
            return
        token = await self.repo.claim_work(app["id"], "thread")
        if not token:
            return
        try:
            await self._ensure_thread_impl(app)
        finally:
            await self.repo.release_work(app["id"], "thread", token)

    async def _ensure_thread_impl(self, app: dict) -> None:
        if app["status"] != CONT_APP_REVIEW_AREA_PENDING:
            return
        review = await self.repo.get_review(app["id"])
        snapshot = review["snapshot"]
        parent = await self._get_channel(snapshot["review_channel_id"])
        if not isinstance(parent, discord.TextChannel):
            raise ValueError("审核频道暂不可用。")
        from .text_utils import sanitize_public_text
        safe_name = sanitize_public_text(app['display_name'], max_len=65, fallback=str(app['user_id']))
        safe_name = re.sub(r"[\x00-\x1f\x7f]+", " ", safe_name)
        safe_name = " ".join(safe_name.split()) or str(app['user_id'])
        name = f"审核-{app['id']}-{safe_name}"
        thread = await self._get_channel(review["thread_id"]) if review["thread_id"] else None
        if not isinstance(thread, discord.Thread):
            thread = next((item for item in parent.threads if item.name == name), None)
        if not isinstance(thread, discord.Thread):
            async for item in parent.archived_threads(limit=None):
                if item.name == name:
                    thread = item
                    break
        if not isinstance(thread, discord.Thread):
            thread = await parent.create_thread(name=name, type=discord.ChannelType.public_thread,
                                                 auto_archive_duration=1440)
        await self.repo.set_thread(app["id"], thread.id)
        panel_id = review["panel_message_id"]
        if not panel_id:
            async for message in thread.history(limit=None):
                if message.author.id == self.bot.user.id and message.embeds and any(
                    (embed.footer.text or "") == f"Continuous Review Application ID: {app['id']}" for embed in message.embeds):
                    panel_id = message.id
                    break
        if not panel_id:
            from .continuous_review_embeds import review_embed
            message = await thread.send(embed=review_embed(app, review, {"yes": 0, "no": 0}),
                                        view=self.view(), nonce=f"pe-cr-panel-{app['id']}",
                                        allowed_mentions=discord.AllowedMentions.none())
            panel_id = message.id
        await self.repo.start_review(app["id"], thread.id, panel_id)
        review = await self.repo.get_review(app["id"])
        await self._send_reminder(app, review, thread)
        await self.refresh_panel(app["id"])

    async def _send_reminder(self, app: dict, review: dict, thread: discord.Thread) -> None:
        if review["reminder_sent"]:
            return
        marker = f"有新的常态申请待审核（#{app['id']}）"
        already = False
        async for message in thread.history(limit=None):
            if message.author.id == self.bot.user.id and marker in (message.content or ""):
                already = True
                break
        if not already:
            mentions = " ".join(f"<@&{role_id}>" for role_id in review["snapshot"]["reviewer_role_ids"])
            await thread.send(f"{mentions} {marker}",
                              nonce=f"pe-cr-ping-{app['id']}",
                              allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False))
        await self.repo.mark_reminder(app["id"])

    async def refresh_panel(self, app_id: int) -> bool:
        app = await self.repo.continuous.get_application(app_id)
        review = await self.repo.get_review(app_id)
        if not app or not review or not review["panel_message_id"] or not review["thread_id"]:
            return False
        thread = await self._get_channel(review["thread_id"])
        if not isinstance(thread, discord.Thread):
            return False
        from .continuous_review_embeds import review_embed
        try:
            message = await thread.fetch_message(review["panel_message_id"])
            await message.edit(embed=review_embed(app, review, await self.repo.counts(app_id)),
                               view=self.view(disabled=app["status"] != CONT_APP_REVIEWING or bool(review["threshold_choice"])),
                               allowed_mentions=discord.AllowedMentions.none())
            await self.repo.mark_panel_synced(app_id, review["panel_revision"])
            return True
        except Exception as exc:
            log.warning("Cannot refresh continuous review panel %s: %s", app_id, exc)
            return False

    async def _eligible(self, interaction: discord.Interaction, app_id: int | None = None) -> tuple[dict | None, dict | None, str | None]:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return None, None, "请在服务器内使用。"
        review = await (self.repo.get_review(app_id) if app_id else self.repo.get_by_panel(
            interaction.guild.id, interaction.message.id if interaction.message else 0))
        app = await self.repo.continuous.get_application(review["application_id"]) if review else None
        if not app or app["guild_id"] != interaction.guild.id:
            return None, None, "无法定位本次审核。"
        if app["status"] != CONT_APP_REVIEWING or review["threshold_choice"]:
            return None, None, "本次审核已结束。"
        if review["review_deadline_at"] <= utc_now_iso():
            return None, None, "本次审核已超时。"
        if interaction.user.bot or interaction.user.id == app["user_id"]:
            return None, None, "申请人或 Bot 不能审核自己的申请。"
        role_ids = {role.id for role in interaction.user.roles}
        if not role_ids.intersection(review["snapshot"]["reviewer_role_ids"]):
            return None, None, "你当前没有本次审核所需的审核员身份组。"
        return app, review, None

    async def review_button(self, interaction: discord.Interaction, action: str) -> None:
        app, review, error = await self._eligible(interaction)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        if action in ("yes", "no"):
            from .continuous_review_views import ContinuousReviewReasonModal
            await interaction.response.send_modal(ContinuousReviewReasonModal(self, app["id"], action))
            return
        if action != "remove":
            await interaction.response.send_message("未知审核操作。", ephemeral=True)
            return
        lock = self._vote_locks.setdefault(app["id"], asyncio.Lock())
        async with lock:
            try:
                old = await self.repo.withdraw_vote(app["id"], interaction.user.id)
                if old:
                    await self.flush_thread_logs(app["id"])
                    await self.refresh_panel(app["id"])
                await interaction.response.send_message("已撤销。" if old else "你尚未投票。", ephemeral=True)
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)

    async def cast_vote(self, interaction: discord.Interaction, app_id: int, choice: str, reason: str) -> None:
        app, review, error = await self._eligible(interaction, app_id)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        lock = self._vote_locks.setdefault(app_id, asyncio.Lock())
        async with lock:
            try:
                result = await self.repo.vote(app_id, interaction.user.id, choice, reason,
                                              interaction.user.display_name[:100])
                if result.get("locked"):
                    await interaction.edit_original_response(content="本次审核正在结算，不能继续投票。")
                    return
                await self.flush_thread_logs(app_id)
                await self.refresh_panel(app_id)
                if result["threshold_choice"]:
                    await self.repo.settle(app_id)
                    await self.process_result(app_id)
                await interaction.edit_original_response(content="审核意见已保存。")
            except ValueError as exc:
                await interaction.edit_original_response(content=str(exc))
            except Exception as exc:
                log.exception("Continuous review vote follow-up failed")
                await interaction.edit_original_response(content=f"审核意见已保存；后续操作将自动恢复：{str(exc)[:150]}")

    @staticmethod
    def _thread_event_content(event: dict) -> str:
        detail = json.loads(event.get("detail_json") or "{}")
        actor = detail.get("reviewer_name") or str(event.get("actor_id") or "系统")
        choice = {"yes": "同意", "no": "拒绝", None: "无"}
        kind = event["event_type"]
        if kind == "vote_cast":
            body = f"{actor} 提交审核意见\n选择：{choice.get(event['new_choice'], event['new_choice'])}\n理由：{event.get('reason') or '（无）'}"
        elif kind == "vote_changed":
            body = f"{actor} 修改审核意见\n{choice.get(event['old_choice'])} → {choice.get(event['new_choice'])}\n理由：{event.get('reason') or '（无）'}"
        elif kind == "vote_withdrawn":
            body = f"{actor} 撤销审核意见\n原选择：{choice.get(event['old_choice'])}"
        elif kind in ("manual_approve", "manual_reject"):
            body = f"管理员 {actor} 手动{'通过' if kind == 'manual_approve' else '拒绝'}\n理由：{event.get('reason') or '（无）'}"
        else:
            body = f"申请人 {actor} 撤回申请"
        return f"{body}\n时间：{event['created_at']}\n审核事件 #{event['id']}"

    async def flush_thread_logs(self, app_id: int) -> bool:
        pending = await self.repo.unlogged_thread_events(app_id)
        if not pending:
            return True
        token = await self.repo.claim_work(app_id, "log")
        if not token:
            return False
        try:
            review = await self.repo.get_review(app_id)
            thread = await self._get_channel(review["thread_id"]) if review and review["thread_id"] else None
            if not isinstance(thread, discord.Thread):
                return False
            messages = [message async for message in thread.history(limit=None)]
            for event in await self.repo.unlogged_thread_events(app_id):
                marker = f"审核事件 #{event['id']}"
                existing = next((message for message in messages
                    if message.author.id == self.bot.user.id and marker in (message.content or "")), None)
                if existing is None:
                    existing = await thread.send(self._thread_event_content(event)[:2000],
                        nonce=f"pe-cr-log-{event['id']}", allowed_mentions=discord.AllowedMentions.none())
                await self.repo.mark_thread_event_logged(event["id"], existing.id)
            return True
        except Exception as exc:
            await self.repo.add_event(app_id, None, "thread_log_failed", str(exc)[:400])
            return False
        finally:
            await self.repo.release_work(app_id, "log", token)

    async def manual(self, app_id: int, choice: str, operator_id: int, reason: str | None) -> bool:
        if choice == "no" and not (reason or "").strip():
            raise ValueError("手动拒绝必须填写理由。")
        review = await self.repo.get_review(app_id)
        if not review:
            raise ValueError("该申请没有前置审核记录。")
        changed = await self.repo.settle(app_id, manual_choice=choice, operator_id=operator_id, reason=reason)
        if changed:
            await self.flush_thread_logs(app_id)
            await self.process_result(app_id)
        return changed

    async def process_result(self, app_id: int) -> None:
        app = await self.repo.continuous.get_application(app_id)
        review = await self.repo.get_review(app_id)
        if not app or not review or app["status"] not in (CONT_APP_REVIEW_PUBLISH_PENDING, CONT_APP_REVIEW_REJECTED, CONT_APP_WITHDRAWN):
            return
        await self.refresh_panel(app_id)
        if app["status"] != CONT_APP_WITHDRAWN and await self.repo.claim_dm(app_id):
            try:
                user = self.bot.get_user(app["user_id"]) or await self.bot.fetch_user(app["user_id"])
                template = review["snapshot"]["pass_dm_template"] if review["outcome"] == "yes" else review["snapshot"]["reject_dm_template"]
                content = template + f"\n\n申请 ID：{app_id}\n申请岗位：{app['field_name']}\n原始申请：{app['self_intro']}"
                if review["outcome"] == "no":
                    reasons = [vote["reason"] for vote in await self.repo.votes(app_id) if vote["choice"] == "no" and vote["reason"]]
                    manual_reasons = [event["reason"] for event in await self.repo.events(app_id) if event["event_type"] == "manual_reject" and event["reason"]]
                    if reasons or manual_reasons:
                        content += "\n\n拒绝理由：\n" + "\n".join(f"{i}. {value}" for i, value in enumerate(manual_reasons + reasons, 1))
                await user.send(content[:2000], allowed_mentions=discord.AllowedMentions.none())
                await self.repo.mark_dm(app_id, None)
            except Exception as exc:
                await self.repo.mark_dm(app_id, str(exc)[:400])
        await self.refresh_panel(app_id)
        await self.archive(app_id)
        review = await self.repo.get_review(app_id)
        if app["status"] == CONT_APP_REVIEW_PUBLISH_PENDING and review["archive_locked"]:
            await self.publish_public_vote(app_id)

    async def archive(self, app_id: int) -> None:
        token = await self.repo.claim_work(app_id, "archive")
        if not token:
            return
        try:
            await self._archive_impl(app_id)
        finally:
            await self.repo.release_work(app_id, "archive", token)

    async def _archive_impl(self, app_id: int) -> None:
        app = await self.repo.continuous.get_application(app_id)
        review = await self.repo.get_review(app_id)
        if not review or not review["outcome"] or review["archive_locked"]:
            return
        thread = await self._get_channel(review["thread_id"]) if review["thread_id"] else None
        if review["thread_id"] and not isinstance(thread, discord.Thread):
            await self.repo.archive_error(app_id, "审核 Thread 暂不可用")
            return
        if thread and not await self.flush_thread_logs(app_id):
            await self.repo.archive_error(app_id, "审核实名记录待补发")
            return
        if not review["archive_message_id"]:
            archive_channel = await self._get_channel(review["snapshot"]["archive_channel_id"])
            if not isinstance(archive_channel, (discord.TextChannel, discord.Thread)):
                await self.repo.archive_error(app_id, "归档位置暂不可用")
                return
            marker = f"常态审核归档 #{app_id}"
            archive_message_id = None
            try:
                async for message in archive_channel.history(limit=None, after=parse_iso(app["submitted_at"])):
                    if message.author.id == self.bot.user.id and marker in (message.content or ""):
                        archive_message_id = message.id
                        break
                if not archive_message_id:
                    from .continuous_review_archive import build_review_html
                    messages = [message async for message in thread.history(limit=None, oldest_first=True)] if thread else []
                    data = build_review_html(app, review, await self.repo.votes(app_id), await self.repo.events(app_id), messages)
                    message = await archive_channel.send(marker, file=discord.File(io.BytesIO(data), filename=f"continuous-review-{app_id}.html"),
                        nonce=f"pe-cr-archive-{app_id}",
                        allowed_mentions=discord.AllowedMentions.none())
                    archive_message_id = message.id
                await self.repo.mark_archive(app_id, archive_message_id)
            except Exception as exc:
                await self.repo.archive_error(app_id, str(exc))
                return
        if thread is None:
            await self.repo.mark_archive_locked(app_id)
            return
        await self.repo.clear_archive_error(app_id)
        if not await self.refresh_panel(app_id) or (await self.repo.get_review(app_id))["panel_dirty"]:
            await self.repo.archive_error(app_id, "审核面板暂不可编辑，等待权限恢复")
            return
        try:
            if not thread.locked or not thread.archived:
                await thread.edit(locked=True, archived=True, reason=f"常态审核 #{app_id} 已归档")
            await self.repo.mark_archive_locked(app_id)
        except Exception as exc:
            await self.repo.archive_error(app_id, str(exc))

    async def publish_public_vote(self, app_id: int) -> bool:
        review = await self.repo.get_review(app_id)
        if not review or not review["archive_locked"]:
            return False
        token = await self.repo.claim_work(app_id, "publish")
        if not token:
            return False
        try:
            return await self._publish_public_vote_impl(app_id)
        finally:
            await self.repo.release_work(app_id, "publish", token)

    async def _publish_public_vote_impl(self, app_id: int) -> bool:
        app = await self.repo.continuous.get_application(app_id)
        if not app or app["status"] != CONT_APP_REVIEW_PUBLISH_PENDING:
            return False
        config = await self.repo.continuous.get_config(app["config_id"])
        channel = await self._get_channel(config["voting_channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return False
        message_id = app["vote_message_id"]
        sent_message = None
        persisted = False
        try:
            if not message_id:
                async for message in channel.history(limit=None, after=parse_iso(app["submitted_at"])):
                    if message.author.id == self.bot.user.id and message.embeds and any(
                        f"Application ID: {app_id}｜" in (embed.footer.text or "") for embed in message.embeds):
                        message_id = message.id
                        break
            if not message_id:
                from .continuous_embeds import build_continuous_application_embed
                msg = await channel.send(embed=build_continuous_application_embed(config, app),
                    view=self.continuous.vote_view(self.continuous._config_mode(config)),
                    nonce=f"pe-cr-public-{app_id}",
                    allowed_mentions=discord.AllowedMentions.none())
                message_id = msg.id
                sent_message = msg
            changed = await self.repo.public_vote_published(app_id, channel.id, message_id, config["voting_duration_minutes"])
            if changed:
                persisted = True
                try:
                    published = await self.repo.continuous.get_application(app_id)
                    await self.continuous._edit_vote_message(config, published)
                except Exception:
                    log.exception("Cannot refresh newly published public vote for %s", app_id)
            elif sent_message is not None:
                await sent_message.delete()
            return changed
        except Exception as exc:
            if sent_message is not None and not persisted:
                try:
                    await sent_message.delete()
                except Exception:
                    pass
            await self.repo.add_event(app_id, None, "public_vote_publish_failed", str(exc)[:400])
            return False

    async def withdraw(self, app: dict) -> bool:
        config = await self.repo.continuous.get_config(app["config_id"])
        cooldown = (utc_now() + timedelta(minutes=config["cooldown_minutes"])).isoformat()
        changed = await self.repo.withdraw(app["id"], app["user_id"], cooldown)
        if changed:
            review = await self.repo.get_review(app["id"])
            if review["thread_id"] and app["status"] != CONT_APP_REVIEW_PUBLISH_PENDING:
                await self.flush_thread_logs(app["id"])
            await self.refresh_panel(app["id"])
            if app["status"] != CONT_APP_REVIEW_PUBLISH_PENDING:
                await self.archive(app["id"])
            else:
                try:
                    user = self.bot.get_user(app["user_id"]) or await self.bot.fetch_user(app["user_id"])
                    await user.send("你的常态申请已撤回，后续公众投票发布已取消。")
                except Exception:
                    pass
        return changed

    async def retry_grant_role(self, app: dict) -> None:
        if app["status"] != "approved" or app["role_grant_status"] == "success" or not app["role_grant_role_id"]:
            return
        now = utc_now()
        async with self.repo.lock:
            cur = await self.repo.db.conn.execute("""UPDATE pe_continuous_applications
                SET role_grant_status='sending',role_grant_next_retry_at=?
                WHERE id=? AND status='approved' AND
                (role_grant_status IN ('pending','failed') OR
                 (role_grant_status='sending' AND role_grant_next_retry_at<=?))""",
                ((now + timedelta(minutes=10)).isoformat(), app["id"], now.isoformat()))
            claimed = cur.rowcount > 0
            await cur.close()
            await self.repo.db.conn.commit()
        if not claimed:
            return
        try:
            guild = self.bot.get_guild(app["guild_id"])
            if guild is None:
                raise ValueError("服务器暂不可用")
            role = guild.get_role(app["role_grant_role_id"])
            if role is None:
                raise ValueError("通过后身份组已删除")
            member = guild.get_member(app["user_id"]) or await guild.fetch_member(app["user_id"])
            if role not in member.roles:
                me = guild.me
                if me is None or not me.guild_permissions.manage_roles or me.top_role <= role:
                    raise ValueError("Bot 缺少 Manage Roles 或身份组层级不足")
                await member.add_roles(role, reason=f"常态支持票申请 #{app['id']} 已通过")
            error = None
        except Exception as exc:
            error = str(exc)[:400]
        await self.repo.db.execute_close("""UPDATE pe_continuous_applications SET role_grant_status=?,role_grant_error=?,
            role_grant_next_retry_at=? WHERE id=? AND status='approved' AND role_grant_status='sending'""",
            ("failed" if error else "success", error,
             (utc_now() + timedelta(minutes=10)).isoformat() if error else None, app["id"]))

    async def maintenance(self) -> None:
        for app_id in await self.repo.timeout_due():
            await self.refresh_panel(app_id)
        for app in await self.repo.pending():
            try:
                if app["status"] == CONT_APP_REVIEW_AREA_PENDING:
                    await self.ensure_thread(app)
                    continue
                if app["status"] == CONT_APP_REVIEWING:
                    review = await self.repo.get_review(app["id"])
                    await self.flush_thread_logs(app["id"])
                    if review["threshold_choice"]:
                        await self.refresh_panel(app["id"])
                        await self.repo.settle(app["id"])
                        await self.process_result(app["id"])
                        continue
                    thread = await self._get_channel(review["thread_id"]) if review["thread_id"] else None
                    if isinstance(thread, discord.Thread):
                        if thread.archived and not thread.locked:
                            await thread.edit(archived=False, reason=f"常态审核 #{app['id']} 尚未结束")
                        if not review["reminder_sent"]:
                            token = await self.repo.claim_work(app["id"], "thread")
                            if token:
                                try:
                                    await self._send_reminder(app, review, thread)
                                finally:
                                    await self.repo.release_work(app["id"], "thread", token)
                    continue
                if app["status"] == CONT_APP_REVIEW_TIMEOUT:
                    review = await self.repo.get_review(app["id"])
                    await self.flush_thread_logs(app["id"])
                    thread = await self._get_channel(review["thread_id"]) if review["thread_id"] else None
                    if isinstance(thread, discord.Thread) and thread.archived and not thread.locked:
                        await thread.edit(archived=False, reason=f"常态审核 #{app['id']} 超时待人工处理")
                    continue
                await self.process_result(app["id"])
            except Exception:
                log.exception("Continuous review maintenance failed for %s", app["id"])
        rows = await self.repo.db.fetchall("""SELECT * FROM pe_continuous_applications WHERE status='approved'
            AND role_grant_role_id IS NOT NULL AND
            ((role_grant_status IN ('pending','failed') AND
              (role_grant_next_retry_at IS NULL OR role_grant_next_retry_at<=?))
             OR (role_grant_status='sending' AND role_grant_next_retry_at<=?))""",
            (utc_now_iso(), utc_now_iso()))
        for row in rows:
            await self.retry_grant_role(dict(row))
        for app_id in await self.repo.dirty_panels():
            await self.refresh_panel(app_id)
