from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any, Iterable

from .continuous_constants import (
    CONT_APP_APPROVED,
    CONT_APP_REJECTED,
    CONT_APP_VOTING,
    CONT_CONFIG_ACTIVE,
    CONT_MODE_APPROVAL,
    CONT_MODE_SUPPORT,
    CONT_VOTE_CHOICES,
    CONT_VOTE_NO,
    CONT_VOTE_SUPPORT,
    CONT_VOTE_YES,
)
from .continuous_logic import calculate_application_result, calculate_support_collection_result
from .time_utils import utc_now_iso

CONTINUOUS_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS pe_continuous_configs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  entry_channel_id INTEGER NOT NULL,
  voting_channel_id INTEGER NOT NULL,
  public_channel_id INTEGER NOT NULL,
  allowed_application_role_ids TEXT,
  allowed_voter_role_ids TEXT,
  entry_message_id INTEGER,
  mode TEXT NOT NULL DEFAULT 'approval_vote',
  min_total_votes INTEGER NOT NULL,
  approval_threshold_percent REAL NOT NULL,
  support_target_votes INTEGER,
  voting_duration_minutes INTEGER NOT NULL,
  cooldown_minutes INTEGER NOT NULL,
  created_by INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pe_cont_configs_guild_status ON pe_continuous_configs(guild_id, status);
CREATE INDEX IF NOT EXISTS idx_pe_cont_configs_entry_msg ON pe_continuous_configs(guild_id, entry_message_id);

CREATE TABLE IF NOT EXISTS pe_continuous_fields (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  config_id INTEGER NOT NULL,
  field_key TEXT NOT NULL,
  name TEXT NOT NULL,
  sort_order INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(config_id, field_key),
  UNIQUE(config_id, name),
  FOREIGN KEY(config_id) REFERENCES pe_continuous_configs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pe_continuous_applications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  config_id INTEGER NOT NULL,
  guild_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  display_name TEXT NOT NULL,
  username TEXT,
  field_key TEXT NOT NULL,
  field_name TEXT NOT NULL,
  self_intro TEXT NOT NULL,
  status TEXT NOT NULL,
  vote_channel_id INTEGER,
  vote_message_id INTEGER,
  submitted_at TEXT NOT NULL,
  voting_end_at TEXT NOT NULL,
  closed_at TEXT,
  cooldown_until TEXT,
  result_json TEXT,
  status_reason TEXT,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(config_id) REFERENCES pe_continuous_configs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pe_cont_apps_config_status ON pe_continuous_applications(config_id, status);
CREATE INDEX IF NOT EXISTS idx_pe_cont_apps_user_status ON pe_continuous_applications(config_id, user_id, status);
CREATE INDEX IF NOT EXISTS idx_pe_cont_apps_due ON pe_continuous_applications(status, voting_end_at);
CREATE INDEX IF NOT EXISTS idx_pe_cont_apps_vote_msg ON pe_continuous_applications(guild_id, vote_message_id);

CREATE TABLE IF NOT EXISTS pe_continuous_vote_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  application_id INTEGER NOT NULL,
  config_id INTEGER NOT NULL,
  guild_id INTEGER NOT NULL,
  voter_id INTEGER NOT NULL,
  choice TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(application_id, voter_id),
  FOREIGN KEY(application_id) REFERENCES pe_continuous_applications(id) ON DELETE CASCADE,
  FOREIGN KEY(config_id) REFERENCES pe_continuous_configs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pe_cont_vote_app_choice ON pe_continuous_vote_records(application_id, choice);
"""


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


class ContinuousApplicationRepo:
    def __init__(self, db):
        self.db = db
        self.lock = asyncio.Lock()

    async def ensure_schema(self) -> None:
        if self.db.conn is None:
            raise RuntimeError("DB not connected")
        await self.db.conn.executescript(CONTINUOUS_SCHEMA_SQL)
        alter_sqls = [
            "ALTER TABLE pe_continuous_configs ADD COLUMN mode TEXT NOT NULL DEFAULT 'approval_vote'",
            "ALTER TABLE pe_continuous_configs ADD COLUMN support_target_votes INTEGER",
        ]
        for sql in alter_sqls:
            try:
                await self.db.conn.execute(sql)
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "duplicate column name" in message or "already exists" in message:
                    continue
                raise
        await self.db.conn.commit()

    @staticmethod
    def encode_list(values: Iterable[Any]) -> str:
        return _json_dumps(list(values))

    @staticmethod
    def decode_role_ids(value: str | None) -> list[int]:
        raw = _json_loads(value, [])
        return [int(item) for item in raw or []]

    @staticmethod
    def decode_result(value: str | None) -> dict[str, Any]:
        raw = _json_loads(value, {})
        return dict(raw or {})

    async def create_config(
        self,
        *,
        guild_id: int,
        name: str,
        entry_channel_id: int,
        voting_channel_id: int,
        public_channel_id: int,
        allowed_application_role_ids: list[int],
        allowed_voter_role_ids: list[int],
        min_total_votes: int,
        approval_threshold_percent: float,
        voting_duration_minutes: int,
        cooldown_minutes: int,
        created_by: int,
        fields: list[str],
        mode: str = CONT_MODE_APPROVAL,
        support_target_votes: int | None = None,
    ) -> int:
        now = utc_now_iso()
        mode = str(mode or CONT_MODE_APPROVAL)
        if mode not in (CONT_MODE_APPROVAL, CONT_MODE_SUPPORT):
            raise ValueError("未知常态申请模式。")
        if mode == CONT_MODE_SUPPORT and int(support_target_votes or 0) < 1:
            raise ValueError("支持票收集模式必须设置大于 0 的支持目标票数。")
        if mode == CONT_MODE_APPROVAL and support_target_votes is not None:
            raise ValueError("同意/反对投票模式不使用支持目标票数。")
        if self.db.conn is None:
            raise RuntimeError("DB not connected")
        async with self.lock:
            try:
                await self.db.conn.execute("BEGIN IMMEDIATE")
                cur = await self.db.conn.execute(
                    """
                    INSERT INTO pe_continuous_configs(
                      guild_id, name, status, entry_channel_id, voting_channel_id, public_channel_id,
                      allowed_application_role_ids, allowed_voter_role_ids,
                      mode, min_total_votes, approval_threshold_percent, support_target_votes, voting_duration_minutes, cooldown_minutes,
                      created_by, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        int(guild_id),
                        name,
                        CONT_CONFIG_ACTIVE,
                        int(entry_channel_id),
                        int(voting_channel_id),
                        int(public_channel_id),
                        _json_dumps([int(x) for x in allowed_application_role_ids]),
                        _json_dumps([int(x) for x in allowed_voter_role_ids]),
                        mode,
                        int(min_total_votes),
                        float(approval_threshold_percent),
                        int(support_target_votes) if support_target_votes is not None else None,
                        int(voting_duration_minutes),
                        int(cooldown_minutes),
                        int(created_by),
                        now,
                        now,
                    ),
                )
                config_id = int(cur.lastrowid)
                await cur.close()
                for idx, field_name in enumerate(fields, start=1):
                    await self.db.conn.execute(
                        """
                        INSERT INTO pe_continuous_fields(config_id, field_key, name, sort_order, created_at)
                        VALUES(?,?,?,?,?)
                        """,
                        (config_id, f"field_{idx}", field_name, idx, now),
                    )
                await self.db.conn.commit()
                return config_id
            except Exception:
                await self.db.conn.rollback()
                raise

    async def get_config(self, config_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM pe_continuous_configs WHERE id=?", (int(config_id),))
        return dict(row) if row else None

    async def list_configs(self, guild_id: int, *, include_archived: bool = False, limit: int = 25) -> list[dict[str, Any]]:
        if include_archived:
            rows = await self.db.fetchall(
                "SELECT * FROM pe_continuous_configs WHERE guild_id=? ORDER BY id DESC LIMIT ?",
                (int(guild_id), int(limit)),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM pe_continuous_configs WHERE guild_id=? AND status=? ORDER BY id DESC LIMIT ?",
                (int(guild_id), CONT_CONFIG_ACTIVE, int(limit)),
            )
        return [dict(row) for row in rows]

    async def resolve_config(self, guild_id: int, config_id: int | None = None) -> dict[str, Any]:
        if config_id is not None:
            config = await self.get_config(int(config_id))
            if not config or int(config["guild_id"]) != int(guild_id):
                raise ValueError("未找到该常态申请配置，或该配置不属于当前服务器。")
            return config
        rows = await self.list_configs(int(guild_id), include_archived=False, limit=5)
        if not rows:
            raise ValueError("当前服务器没有常态申请配置，请先创建。")
        if len(rows) > 1:
            ids = "、".join(str(row["id"]) for row in rows)
            raise ValueError(f"当前有多个常态申请配置，请明确填写配置ID。可选：{ids}")
        return rows[0]

    async def list_fields(self, config_id: int) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM pe_continuous_fields WHERE config_id=? ORDER BY sort_order ASC",
            (int(config_id),),
        )
        return [dict(row) for row in rows]

    async def set_entry_message(self, config_id: int, message_id: int, channel_id: int | None = None) -> None:
        await self.db.execute_close(
            """
            UPDATE pe_continuous_configs
            SET entry_message_id=?, entry_channel_id=COALESCE(?, entry_channel_id), updated_at=?
            WHERE id=?
            """,
            (int(message_id), int(channel_id) if channel_id else None, utc_now_iso(), int(config_id)),
        )

    async def find_config_by_entry_message(self, guild_id: int, message_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            """
            SELECT * FROM pe_continuous_configs
            WHERE guild_id=? AND entry_message_id=?
            ORDER BY id DESC LIMIT 1
            """,
            (int(guild_id), int(message_id)),
        )
        return dict(row) if row else None

    async def create_application(
        self,
        *,
        config: dict[str, Any],
        user_id: int,
        display_name: str,
        username: str,
        field_key: str,
        field_name: str,
        self_intro: str,
        voting_end_at: str,
    ) -> int:
        now = utc_now_iso()
        if self.db.conn is None:
            raise RuntimeError("DB not connected")
        async with self.lock:
            try:
                await self.db.conn.execute("BEGIN IMMEDIATE")
                cur = await self.db.conn.execute(
                    """
                    SELECT id, status, cooldown_until
                    FROM pe_continuous_applications
                    WHERE config_id=? AND user_id=?
                      AND (
                        status IN (?, ?)
                        OR (cooldown_until IS NOT NULL AND cooldown_until>?)
                      )
                    ORDER BY id DESC LIMIT 1
                    """,
                    (int(config["id"]), int(user_id), CONT_APP_VOTING, CONT_APP_APPROVED, now),
                )
                blocker = await cur.fetchone()
                await cur.close()
                if blocker:
                    status = str(blocker["status"] or "")
                    cooldown_until = blocker["cooldown_until"]
                    if status == CONT_APP_VOTING:
                        raise ValueError(f"你已有进行中的申请（Application ID: {blocker['id']}），不能重复申请。")
                    if status == CONT_APP_APPROVED:
                        raise ValueError("你已经在该常态申请中通过，不能重复申请；如需退出通过名单，请点击入口里的『退出申请』。")
                    raise ValueError(f"你仍在冷却期内，冷却结束：{cooldown_until}。")

                cur = await self.db.conn.execute(
                    """
                    INSERT INTO pe_continuous_applications(
                      config_id, guild_id, user_id, display_name, username,
                      field_key, field_name, self_intro, status,
                      submitted_at, voting_end_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        int(config["id"]),
                        int(config["guild_id"]),
                        int(user_id),
                        display_name,
                        username,
                        str(field_key),
                        field_name,
                        self_intro,
                        CONT_APP_VOTING,
                        now,
                        voting_end_at,
                        now,
                    ),
                )
                application_id = int(cur.lastrowid)
                await cur.close()
                await self.db.conn.commit()
                return application_id
            except Exception:
                await self.db.conn.rollback()
                raise

    async def get_application(self, application_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM pe_continuous_applications WHERE id=?", (int(application_id),))
        return dict(row) if row else None

    async def find_application_by_vote_message(self, guild_id: int, message_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            """
            SELECT * FROM pe_continuous_applications
            WHERE guild_id=? AND vote_message_id=?
            ORDER BY id DESC LIMIT 1
            """,
            (int(guild_id), int(message_id)),
        )
        return dict(row) if row else None

    async def set_application_vote_message(self, application_id: int, channel_id: int, message_id: int) -> None:
        await self.db.execute_close(
            """
            UPDATE pe_continuous_applications
            SET vote_channel_id=?, vote_message_id=?, updated_at=?
            WHERE id=?
            """,
            (int(channel_id), int(message_id), utc_now_iso(), int(application_id)),
        )

    async def set_application_status(
        self,
        application_id: int,
        status: str,
        *,
        reason: str | None = None,
        cooldown_until: str | None = None,
        result: dict[str, Any] | None = None,
        closed_at: str | None = None,
        expected_status: str | None = None,
        require_not_expired: bool = False,
    ) -> bool:
        now = utc_now_iso()
        if self.db.conn is None:
            raise RuntimeError("DB not connected")
        where = "WHERE id=?"
        params: list[Any] = [
            status,
            reason,
            cooldown_until,
            _json_dumps(result or {}),
            closed_at or now,
            now,
            int(application_id),
        ]
        if expected_status is not None:
            where += " AND status=?"
            params.append(str(expected_status))
        if require_not_expired:
            where += " AND voting_end_at>?"
            params.append(now)
        async with self.lock:
            cur = await self.db.conn.execute(
                f"""
                UPDATE pe_continuous_applications
                SET status=?, status_reason=?, cooldown_until=COALESCE(?, cooldown_until),
                    result_json=?, closed_at=COALESCE(?, closed_at), updated_at=?
                {where}
                """,
                tuple(params),
            )
            changed = cur.rowcount > 0
            await cur.close()
            await self.db.conn.commit()
            return changed

    async def get_active_application(self, config_id: int, user_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            """
            SELECT * FROM pe_continuous_applications
            WHERE config_id=? AND user_id=? AND status=?
            ORDER BY id DESC LIMIT 1
            """,
            (int(config_id), int(user_id), CONT_APP_VOTING),
        )
        return dict(row) if row else None

    async def get_approved_application(self, config_id: int, user_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            """
            SELECT * FROM pe_continuous_applications
            WHERE config_id=? AND user_id=? AND status=?
            ORDER BY id DESC LIMIT 1
            """,
            (int(config_id), int(user_id), CONT_APP_APPROVED),
        )
        return dict(row) if row else None

    async def get_latest_user_application(self, config_id: int, user_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            """
            SELECT * FROM pe_continuous_applications
            WHERE config_id=? AND user_id=?
            ORDER BY id DESC LIMIT 1
            """,
            (int(config_id), int(user_id)),
        )
        return dict(row) if row else None

    async def get_active_cooldown(self, config_id: int, user_id: int, now_iso: str) -> str | None:
        row = await self.db.fetchone(
            """
            SELECT cooldown_until FROM pe_continuous_applications
            WHERE config_id=? AND user_id=? AND cooldown_until IS NOT NULL AND cooldown_until>?
            ORDER BY cooldown_until DESC LIMIT 1
            """,
            (int(config_id), int(user_id), now_iso),
        )
        return str(row["cooldown_until"]) if row else None

    async def list_due_applications(self, now_iso: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """
            SELECT * FROM pe_continuous_applications
            WHERE status=? AND voting_end_at<=?
            ORDER BY voting_end_at ASC, id ASC
            """,
            (CONT_APP_VOTING, now_iso),
        )
        return [dict(row) for row in rows]

    async def list_due_applications_for_config(self, config_id: int, now_iso: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """
            SELECT * FROM pe_continuous_applications
            WHERE config_id=? AND status=? AND voting_end_at<=?
            ORDER BY voting_end_at ASC, id ASC
            """,
            (int(config_id), CONT_APP_VOTING, now_iso),
        )
        return [dict(row) for row in rows]

    async def list_open_applications(self, config_id: int) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """
            SELECT * FROM pe_continuous_applications
            WHERE config_id=? AND status=?
            ORDER BY voting_end_at ASC, id ASC
            """,
            (int(config_id), CONT_APP_VOTING),
        )
        return [dict(row) for row in rows]

    async def find_approved_application(self, *, config_id: int, user_id: int, field_name: str | None = None) -> dict[str, Any] | None:
        if field_name:
            row = await self.db.fetchone(
                """
                SELECT * FROM pe_continuous_applications
                WHERE config_id=? AND user_id=? AND status=? AND field_name=?
                ORDER BY id DESC LIMIT 1
                """,
                (int(config_id), int(user_id), CONT_APP_APPROVED, field_name),
            )
        else:
            row = await self.db.fetchone(
                """
                SELECT * FROM pe_continuous_applications
                WHERE config_id=? AND user_id=? AND status=?
                ORDER BY id DESC LIMIT 1
                """,
                (int(config_id), int(user_id), CONT_APP_APPROVED),
            )
        return dict(row) if row else None

    async def upsert_vote_record(self, *, application: dict[str, Any], voter_id: int, choice: str) -> dict[str, Any]:
        if choice not in CONT_VOTE_CHOICES:
            raise ValueError("未知投票选项。")
        now = utc_now_iso()
        if self.db.conn is None:
            raise RuntimeError("DB not connected")
        async with self.lock:
            try:
                await self.db.conn.execute("BEGIN IMMEDIATE")
                cur = await self.db.conn.execute(
                    "SELECT * FROM pe_continuous_applications WHERE id=?",
                    (int(application["id"]),),
                )
                fresh = await cur.fetchone()
                await cur.close()
                if not fresh:
                    raise ValueError("未找到该申请。")
                if str(fresh["status"] or "") != CONT_APP_VOTING:
                    raise ValueError("该申请投票已经结束。")
                if str(fresh["voting_end_at"] or "") <= now:
                    raise ValueError("该申请投票已到期。")

                await self.db.conn.execute(
                    """
                    INSERT INTO pe_continuous_vote_records(application_id, config_id, guild_id, voter_id, choice, created_at, updated_at)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(application_id, voter_id)
                    DO UPDATE SET choice=excluded.choice, updated_at=excluded.updated_at
                    """,
                    (
                        int(fresh["id"]),
                        int(fresh["config_id"]),
                        int(fresh["guild_id"]),
                        int(voter_id),
                        choice,
                        now,
                        now,
                    ),
                )
                cur = await self.db.conn.execute(
                    "SELECT * FROM pe_continuous_vote_records WHERE application_id=? AND voter_id=?",
                    (int(fresh["id"]), int(voter_id)),
                )
                record = await cur.fetchone()
                await cur.close()
                if not record:
                    raise ValueError("投票记录写入失败。")
                await self.db.conn.commit()
                return dict(record)
            except Exception:
                await self.db.conn.rollback()
                raise

    async def delete_vote_record(self, *, application: dict[str, Any], voter_id: int, choice: str | None = None) -> bool:
        now = utc_now_iso()
        if self.db.conn is None:
            raise RuntimeError("DB not connected")
        async with self.lock:
            try:
                await self.db.conn.execute("BEGIN IMMEDIATE")
                cur = await self.db.conn.execute(
                    "SELECT * FROM pe_continuous_applications WHERE id=?",
                    (int(application["id"]),),
                )
                fresh = await cur.fetchone()
                await cur.close()
                if not fresh:
                    raise ValueError("未找到该申请。")
                if str(fresh["status"] or "") != CONT_APP_VOTING:
                    raise ValueError("该申请投票已经结束。")
                if str(fresh["voting_end_at"] or "") <= now:
                    raise ValueError("该申请投票已到期。")

                if choice is None:
                    cur = await self.db.conn.execute(
                        "DELETE FROM pe_continuous_vote_records WHERE application_id=? AND voter_id=?",
                        (int(fresh["id"]), int(voter_id)),
                    )
                else:
                    cur = await self.db.conn.execute(
                        "DELETE FROM pe_continuous_vote_records WHERE application_id=? AND voter_id=? AND choice=?",
                        (int(fresh["id"]), int(voter_id), str(choice)),
                    )
                changed = cur.rowcount > 0
                await cur.close()
                await self.db.conn.commit()
                return changed
            except Exception:
                await self.db.conn.rollback()
                raise

    async def finalize_voting_application(
        self,
        application_id: int,
        *,
        min_total_votes: int,
        approval_threshold_percent: float,
        cooldown_until_if_rejected: str | None,
        mode: str = CONT_MODE_APPROVAL,
        support_target_votes: int | None = None,
        reject_when_unmet: bool = True,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        now = utc_now_iso()
        if self.db.conn is None:
            raise RuntimeError("DB not connected")
        async with self.lock:
            try:
                await self.db.conn.execute("BEGIN IMMEDIATE")
                cur = await self.db.conn.execute(
                    "SELECT * FROM pe_continuous_applications WHERE id=?",
                    (int(application_id),),
                )
                application = await cur.fetchone()
                await cur.close()
                if not application or str(application["status"] or "") != CONT_APP_VOTING:
                    await self.db.conn.rollback()
                    return None

                cur = await self.db.conn.execute(
                    """
                    SELECT choice, COUNT(*) AS n
                    FROM pe_continuous_vote_records
                    WHERE application_id=?
                    GROUP BY choice
                    """,
                    (int(application_id),),
                )
                rows = await cur.fetchall()
                await cur.close()
                counts = {CONT_VOTE_YES: 0, CONT_VOTE_NO: 0, CONT_VOTE_SUPPORT: 0}
                for row in rows:
                    counts[str(row["choice"])] = int(row["n"])
                mode = str(mode or CONT_MODE_APPROVAL)
                if mode == CONT_MODE_SUPPORT:
                    result = calculate_support_collection_result(
                        support_votes=counts.get(CONT_VOTE_SUPPORT, 0),
                        support_target_votes=int(support_target_votes or 0),
                    )
                    result["mode"] = CONT_MODE_SUPPORT
                    if not result["passed"] and not reject_when_unmet:
                        await self.db.conn.rollback()
                        return None
                else:
                    result = calculate_application_result(
                        yes_votes=counts.get(CONT_VOTE_YES, 0),
                        no_votes=counts.get(CONT_VOTE_NO, 0),
                        min_total_votes=int(min_total_votes or 0),
                        approval_threshold_percent=float(approval_threshold_percent or 0),
                    )
                    result["mode"] = CONT_MODE_APPROVAL
                status = CONT_APP_APPROVED if result["passed"] else CONT_APP_REJECTED
                reason = "达到通过条件" if result["passed"] else "未达到通过条件"
                cooldown_until = None if result["passed"] else cooldown_until_if_rejected
                cur = await self.db.conn.execute(
                    """
                    UPDATE pe_continuous_applications
                    SET status=?, status_reason=?, cooldown_until=COALESCE(?, cooldown_until),
                        result_json=?, closed_at=COALESCE(?, closed_at), updated_at=?
                    WHERE id=? AND status=?
                    """,
                    (
                        status,
                        reason,
                        cooldown_until,
                        _json_dumps(result),
                        now,
                        now,
                        int(application_id),
                        CONT_APP_VOTING,
                    ),
                )
                changed = cur.rowcount > 0
                await cur.close()
                if not changed:
                    await self.db.conn.rollback()
                    return None
                cur = await self.db.conn.execute(
                    "SELECT * FROM pe_continuous_applications WHERE id=?",
                    (int(application_id),),
                )
                updated = await cur.fetchone()
                await cur.close()
                if not updated:
                    raise ValueError("申请结算后读取失败。")
                await self.db.conn.commit()
                return dict(updated), result
            except Exception:
                await self.db.conn.rollback()
                raise

    async def get_vote_record(self, application_id: int, voter_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM pe_continuous_vote_records WHERE application_id=? AND voter_id=?",
            (int(application_id), int(voter_id)),
        )
        return dict(row) if row else None

    async def count_votes(self, application_id: int) -> dict[str, int]:
        rows = await self.db.fetchall(
            """
            SELECT choice, COUNT(*) AS n
            FROM pe_continuous_vote_records
            WHERE application_id=?
            GROUP BY choice
            """,
            (int(application_id),),
        )
        counts = {CONT_VOTE_YES: 0, CONT_VOTE_NO: 0, CONT_VOTE_SUPPORT: 0}
        for row in rows:
            counts[str(row["choice"])] = int(row["n"])
        counts["total"] = counts.get(CONT_VOTE_YES, 0) + counts.get(CONT_VOTE_NO, 0) + counts.get(CONT_VOTE_SUPPORT, 0)
        return counts

    async def list_vote_records(self, application_id: int, *, choice: str | None = None) -> list[dict[str, Any]]:
        if choice is None:
            rows = await self.db.fetchall(
                """
                SELECT * FROM pe_continuous_vote_records
                WHERE application_id=?
                ORDER BY updated_at ASC, id ASC
                """,
                (int(application_id),),
            )
        else:
            rows = await self.db.fetchall(
                """
                SELECT * FROM pe_continuous_vote_records
                WHERE application_id=? AND choice=?
                ORDER BY updated_at ASC, id ASC
                """,
                (int(application_id), str(choice)),
            )
        return [dict(row) for row in rows]

    async def list_approved_applications(
        self,
        *,
        guild_id: int,
        config_id: int | None = None,
        field_name: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where = ["a.guild_id=?", "a.status=?"]
        params: list[Any] = [int(guild_id), CONT_APP_APPROVED]
        if config_id is not None:
            where.append("a.config_id=?")
            params.append(int(config_id))
        if field_name:
            where.append("a.field_name=?")
            params.append(str(field_name))
        params.append(int(limit))
        rows = await self.db.fetchall(
            f"""
            SELECT a.*, c.name AS config_name
            FROM pe_continuous_applications a
            JOIN pe_continuous_configs c ON c.id=a.config_id
            WHERE {' AND '.join(where)}
            ORDER BY a.field_name ASC, a.closed_at ASC, a.id ASC
            LIMIT ?
            """,
            tuple(params),
        )
        return [dict(row) for row in rows]

    async def count_open_by_config(self, config_id: int) -> int:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM pe_continuous_applications WHERE config_id=? AND status=?",
            (int(config_id), CONT_APP_VOTING),
        )
        return int(row["n"] if row else 0)

    async def count_approved_by_config(self, config_id: int) -> int:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM pe_continuous_applications WHERE config_id=? AND status=?",
            (int(config_id), CONT_APP_APPROVED),
        )
        return int(row["n"] if row else 0)
