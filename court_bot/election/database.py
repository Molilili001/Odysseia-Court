from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any, Iterable

from .constants import (
    BATCH_NOT_REQUIRED,
    PUBLICITY_REALTIME,
    PUBLIC_NOT_PUBLISHED,
    PUBLIC_PENDING,
    REG_ACTIVE,
    REG_REJECTED,
    REG_REVOKED,
    REG_WITHDRAWN,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_REGISTRATION,
    STATUS_REGISTRATION_ENDED,
    STATUS_SETUP,
    STATUS_VOTING,
    VOTE_MODE_UNIFIED,
)
from .time_utils import utc_now_iso

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS pe_elections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  vote_mode TEXT NOT NULL DEFAULT 'unified',
  publicity_mode TEXT NOT NULL,
  registration_channel_id INTEGER,
  voting_channel_id INTEGER,
  public_channel_id INTEGER NOT NULL,
  allowed_candidate_role_ids TEXT,
  allowed_voter_role_ids TEXT,
  registration_entry_message_id INTEGER,
  registration_entry_channel_id INTEGER,
  vote_message_id INTEGER,
  vote_id INTEGER,
  vote_max_selections INTEGER NOT NULL,
  alert_channel_id INTEGER,
  batch_publicity_status TEXT NOT NULL DEFAULT 'not_required',
  batch_publicity_error TEXT,
  schedule_timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
  registration_duration_minutes INTEGER NOT NULL,
  publicity_duration_minutes INTEGER NOT NULL,
  voting_duration_minutes INTEGER NOT NULL,
  registration_start_at TEXT NOT NULL,
  registration_end_at TEXT NOT NULL,
  voting_start_at TEXT NOT NULL,
  voting_end_at TEXT NOT NULL,
  publicity_published_at TEXT,
  created_by INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  result_json TEXT,
  void_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_pe_elections_guild_status ON pe_elections(guild_id, status);
CREATE INDEX IF NOT EXISTS idx_pe_elections_registration_start ON pe_elections(registration_start_at);
CREATE INDEX IF NOT EXISTS idx_pe_elections_registration_end ON pe_elections(registration_end_at);
CREATE INDEX IF NOT EXISTS idx_pe_elections_voting_start ON pe_elections(voting_start_at);
CREATE INDEX IF NOT EXISTS idx_pe_elections_voting_end ON pe_elections(voting_end_at);

CREATE TABLE IF NOT EXISTS pe_fields (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  election_id INTEGER NOT NULL,
  field_key TEXT NOT NULL,
  name TEXT NOT NULL,
  winner_count INTEGER NOT NULL,
  sort_order INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(election_id, field_key),
  UNIQUE(election_id, sort_order),
  FOREIGN KEY(election_id) REFERENCES pe_elections(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pe_registrations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  election_id INTEGER NOT NULL,
  guild_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  display_name TEXT NOT NULL,
  selected_field_keys TEXT NOT NULL,
  self_intro TEXT,
  status TEXT NOT NULL,
  registered_at TEXT NOT NULL,
  last_modified_at TEXT NOT NULL,
  public_message_id INTEGER,
  public_channel_id INTEGER,
  public_sync_status TEXT NOT NULL DEFAULT 'pending',
  public_sync_error TEXT,
  rejected_reason TEXT,
  rejected_by INTEGER,
  rejected_at TEXT,
  revoked_reason TEXT,
  revoked_by INTEGER,
  revoked_at TEXT,
  UNIQUE(election_id, user_id),
  FOREIGN KEY(election_id) REFERENCES pe_elections(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pe_registrations_election_status ON pe_registrations(election_id, status);
CREATE INDEX IF NOT EXISTS idx_pe_registrations_guild_user ON pe_registrations(guild_id, user_id);

CREATE TABLE IF NOT EXISTS pe_votes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  election_id INTEGER NOT NULL,
  guild_id INTEGER NOT NULL,
  message_id INTEGER,
  channel_id INTEGER,
  max_selections INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  closed_at TEXT,
  FOREIGN KEY(election_id) REFERENCES pe_elections(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pe_vote_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  vote_id INTEGER NOT NULL,
  election_id INTEGER NOT NULL,
  voter_id INTEGER NOT NULL,
  selected_user_ids TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(vote_id, voter_id),
  FOREIGN KEY(vote_id) REFERENCES pe_votes(id) ON DELETE CASCADE,
  FOREIGN KEY(election_id) REFERENCES pe_elections(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pe_vote_records_election ON pe_vote_records(election_id);

CREATE TABLE IF NOT EXISTS pe_vote_invalidations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  election_id INTEGER NOT NULL,
  vote_id INTEGER,
  voter_id INTEGER NOT NULL,
  operator_id INTEGER NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(election_id, voter_id),
  FOREIGN KEY(election_id) REFERENCES pe_elections(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pe_audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  election_id INTEGER,
  guild_id INTEGER NOT NULL,
  operator_id INTEGER,
  action TEXT NOT NULL,
  detail_json TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(election_id) REFERENCES pe_elections(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pe_audit_logs_election ON pe_audit_logs(election_id, created_at);
CREATE INDEX IF NOT EXISTS idx_pe_audit_logs_guild ON pe_audit_logs(guild_id, created_at);
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



class ElectionRepo:
    def __init__(self, db):
        self.lock = asyncio.Lock()
        self.db = db

    async def ensure_schema(self) -> None:
        if self.db.conn is None:
            raise RuntimeError("DB not connected")
        await self.db.conn.executescript(SCHEMA_SQL)
        await self.db.conn.commit()
        await self._safe_migrate()

    async def _safe_migrate(self) -> None:
        # For old partially-created pe_elections during development.
        alter_sqls = [
            "ALTER TABLE pe_elections ADD COLUMN allowed_candidate_role_ids TEXT",
            "ALTER TABLE pe_elections ADD COLUMN allowed_voter_role_ids TEXT",
            "ALTER TABLE pe_elections ADD COLUMN registration_entry_channel_id INTEGER",
            "ALTER TABLE pe_elections ADD COLUMN alert_channel_id INTEGER",
            "ALTER TABLE pe_elections ADD COLUMN batch_publicity_status TEXT NOT NULL DEFAULT 'not_required'",
            "ALTER TABLE pe_elections ADD COLUMN batch_publicity_error TEXT",
            "ALTER TABLE pe_elections ADD COLUMN schedule_timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai'",
            "ALTER TABLE pe_elections ADD COLUMN registration_duration_minutes INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE pe_elections ADD COLUMN publicity_duration_minutes INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE pe_elections ADD COLUMN voting_duration_minutes INTEGER NOT NULL DEFAULT 0",
        ]
        for sql in alter_sqls:
            try:
                await self.db.conn.execute(sql)
            except sqlite3.OperationalError as exc:
                text = str(exc).lower()
                if "duplicate column" in text or "already exists" in text:
                    continue
                if "no such table" in text:
                    continue
                raise
        await self.db.conn.commit()

    # ---------- serialization helpers ----------
    @staticmethod
    def decode_role_ids(value: str | None) -> list[int]:
        raw = _json_loads(value, [])
        return [int(x) for x in raw or []]

    @staticmethod
    def decode_field_keys(value: str | None) -> list[str]:
        raw = _json_loads(value, [])
        return [str(x) for x in raw or []]

    @staticmethod
    def encode_list(values: Iterable[Any]) -> str:
        return _json_dumps(list(values))

    # ---------- create / read ----------
    async def create_election(
        self,
        *,
        guild_id: int,
        name: str,
        publicity_mode: str,
        registration_channel_id: int,
        voting_channel_id: int,
        public_channel_id: int,
        alert_channel_id: int | None,
        allowed_candidate_role_ids: list[int],
        allowed_voter_role_ids: list[int],
        vote_max_selections: int,
        registration_duration_minutes: int,
        publicity_duration_minutes: int,
        voting_duration_minutes: int,
        registration_start_at: str,
        registration_end_at: str,
        voting_start_at: str,
        voting_end_at: str,
        created_by: int,
        fields: list[tuple[str, int]],
        initial_status: str = STATUS_SETUP,
    ) -> int:
        now = utc_now_iso()
        batch_status = BATCH_NOT_REQUIRED if publicity_mode == PUBLICITY_REALTIME else "pending"
        if self.db.conn is None:
            raise RuntimeError("DB not connected")
        async with self.lock:
            try:
                await self.db.conn.execute("BEGIN IMMEDIATE")
                cur = await self.db.conn.execute(
                    """
                    INSERT INTO pe_elections(
                      guild_id, name, status, vote_mode, publicity_mode,
                      registration_channel_id, voting_channel_id, public_channel_id,
                      allowed_candidate_role_ids, allowed_voter_role_ids, vote_max_selections, alert_channel_id,
                      batch_publicity_status, schedule_timezone,
                      registration_duration_minutes, publicity_duration_minutes, voting_duration_minutes,
                      registration_start_at, registration_end_at, voting_start_at, voting_end_at,
                      created_by, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        int(guild_id),
                        name,
                        initial_status,
                        VOTE_MODE_UNIFIED,
                        publicity_mode,
                        int(registration_channel_id),
                        int(voting_channel_id),
                        int(public_channel_id),
                        _json_dumps([int(x) for x in allowed_candidate_role_ids]),
                        _json_dumps([int(x) for x in allowed_voter_role_ids]),
                        int(vote_max_selections),
                        int(alert_channel_id) if alert_channel_id else None,
                        batch_status,
                        "Asia/Shanghai",
                        int(registration_duration_minutes),
                        int(publicity_duration_minutes),
                        int(voting_duration_minutes),
                        registration_start_at,
                        registration_end_at,
                        voting_start_at,
                        voting_end_at,
                        int(created_by),
                        now,
                        now,
                    ),
                )
                election_id = int(cur.lastrowid)
                await cur.close()
                for idx, (field_name, winner_count) in enumerate(fields, start=1):
                    await self.db.conn.execute(
                        """
                        INSERT INTO pe_fields(election_id, field_key, name, winner_count, sort_order, created_at)
                        VALUES(?,?,?,?,?,?)
                        """,
                        (election_id, f"field_{idx}", field_name, int(winner_count), idx, now),
                    )
                await self.db.conn.execute(
                    "INSERT INTO pe_audit_logs(election_id, guild_id, operator_id, action, detail_json, created_at) VALUES(?,?,?,?,?,?)",
                    (election_id, int(guild_id), int(created_by), "election_created", _json_dumps({"name": name, "publicity_mode": publicity_mode}), now),
                )
                await self.db.conn.commit()
                return election_id
            except Exception:
                await self.db.conn.rollback()
                raise

    async def get_election(self, election_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM pe_elections WHERE id=?", (int(election_id),))
        return dict(row) if row else None

    async def list_elections(self, guild_id: int, *, include_completed: bool = False, limit: int = 20) -> list[dict[str, Any]]:
        statuses = () if include_completed else (STATUS_COMPLETED, STATUS_CANCELLED)
        if include_completed:
            rows = await self.db.fetchall(
                "SELECT * FROM pe_elections WHERE guild_id=? ORDER BY id DESC LIMIT ?",
                (int(guild_id), int(limit)),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM pe_elections WHERE guild_id=? AND status NOT IN (?,?) ORDER BY id DESC LIMIT ?",
                (int(guild_id), STATUS_COMPLETED, STATUS_CANCELLED, int(limit)),
            )
        return [dict(r) for r in rows]

    async def list_active_elections_all(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """
            SELECT * FROM pe_elections
            WHERE status IN (?,?,?,?)
            ORDER BY id ASC
            """,
            (STATUS_SETUP, STATUS_REGISTRATION, STATUS_REGISTRATION_ENDED, STATUS_VOTING),
        )
        return [dict(r) for r in rows]

    async def find_by_entry_message(self, guild_id: int, message_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM pe_elections WHERE guild_id=? AND registration_entry_message_id=? ORDER BY id DESC LIMIT 1",
            (int(guild_id), int(message_id)),
        )
        return dict(row) if row else None

    async def find_by_vote_message(self, guild_id: int, message_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM pe_elections WHERE guild_id=? AND vote_message_id=? ORDER BY id DESC LIMIT 1",
            (int(guild_id), int(message_id)),
        )
        return dict(row) if row else None


    async def resolve_election(self, guild_id: int, election_id: int | None = None) -> dict[str, Any]:
        if election_id is not None:
            election = await self.get_election(int(election_id))
            if not election or int(election["guild_id"]) != int(guild_id):
                raise ValueError("未找到该募选，或该募选不属于当前服务器。")
            return election
        rows = await self.list_elections(int(guild_id), include_completed=False, limit=5)
        if not rows:
            raise ValueError("当前服务器没有未完成募选，请先创建。")
        if len(rows) > 1:
            ids = "、".join(str(r["id"]) for r in rows)
            raise ValueError(f"当前有多场未完成募选，请明确填写募选ID。可选：{ids}")
        return rows[0]

    async def list_fields(self, election_id: int) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM pe_fields WHERE election_id=? ORDER BY sort_order ASC",
            (int(election_id),),
        )
        return [dict(r) for r in rows]

    async def get_field_names_by_key(self, election_id: int) -> dict[str, str]:
        fields = await self.list_fields(election_id)
        return {str(f["field_key"]): str(f["name"]) for f in fields}

    # ---------- status / messages ----------
    async def set_election_status(self, election_id: int, status: str, *, completed_at: str | None = None, void_reason: str | None = None) -> None:
        await self.db.execute_close(
            "UPDATE pe_elections SET status=?, completed_at=COALESCE(?, completed_at), void_reason=COALESCE(?, void_reason), updated_at=? WHERE id=?",
            (status, completed_at, void_reason, utc_now_iso(), int(election_id)),
        )

    async def set_registration_entry_message(self, election_id: int, message_id: int, channel_id: int | None = None) -> None:
        await self.db.execute_close(
            "UPDATE pe_elections SET registration_entry_message_id=?, registration_entry_channel_id=COALESCE(?, registration_entry_channel_id), updated_at=? WHERE id=?",
            (int(message_id), int(channel_id) if channel_id else None, utc_now_iso(), int(election_id)),
        )

    async def set_vote_message(self, election_id: int, vote_id: int, channel_id: int, message_id: int) -> None:
        await self.db.execute_close(
            "UPDATE pe_elections SET vote_id=?, vote_message_id=?, updated_at=? WHERE id=?",
            (int(vote_id), int(message_id), utc_now_iso(), int(election_id)),
        )

    async def set_vote_closed_at(self, vote_id: int) -> None:
        await self.db.execute_close("UPDATE pe_votes SET closed_at=? WHERE id=?", (utc_now_iso(), int(vote_id)))

    async def set_result(self, election_id: int, result: dict[str, Any], *, void_reason: str | None = None) -> None:
        await self.db.execute_close(
            "UPDATE pe_elections SET result_json=?, void_reason=?, completed_at=?, updated_at=? WHERE id=?",
            (_json_dumps(result), void_reason, utc_now_iso(), utc_now_iso(), int(election_id)),
        )

    async def set_batch_publicity_status(self, election_id: int, status: str, error: str | None = None, *, published_at: str | None = None) -> None:
        await self.db.execute_close(
            "UPDATE pe_elections SET batch_publicity_status=?, batch_publicity_error=?, publicity_published_at=COALESCE(?, publicity_published_at), updated_at=? WHERE id=?",
            (status, error, published_at, utc_now_iso(), int(election_id)),
        )

    async def set_allowed_candidate_role_ids(self, election_id: int, role_ids: list[int]) -> None:
        await self.db.execute_close(
            "UPDATE pe_elections SET allowed_candidate_role_ids=?, updated_at=? WHERE id=?",
            (_json_dumps([int(x) for x in role_ids]), utc_now_iso(), int(election_id)),
        )

    async def set_allowed_voter_role_ids(self, election_id: int, role_ids: list[int]) -> None:
        await self.db.execute_close(
            "UPDATE pe_elections SET allowed_voter_role_ids=?, updated_at=? WHERE id=?",
            (_json_dumps([int(x) for x in role_ids]), utc_now_iso(), int(election_id)),
        )

    # ---------- registrations ----------
    async def get_registration(self, election_id: int, user_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT * FROM pe_registrations WHERE election_id=? AND user_id=?",
            (int(election_id), int(user_id)),
        )
        return dict(row) if row else None

    async def get_registration_by_id(self, registration_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM pe_registrations WHERE id=?", (int(registration_id),))
        return dict(row) if row else None

    async def upsert_registration(
        self,
        *,
        election: dict[str, Any],
        user_id: int,
        display_name: str,
        selected_field_keys: list[str],
        self_intro: str | None,
        is_re_register_after_withdraw: bool = False,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        election_id = int(election["id"])
        public_status = PUBLIC_PENDING if election.get("publicity_mode") == PUBLICITY_REALTIME else PUBLIC_NOT_PUBLISHED
        if self.db.conn is None:
            raise RuntimeError("DB not connected")
        async with self.lock:
            try:
                await self.db.conn.execute("BEGIN IMMEDIATE")
                cur = await self.db.conn.execute(
                    "SELECT * FROM pe_registrations WHERE election_id=? AND user_id=?",
                    (election_id, int(user_id)),
                )
                row = await cur.fetchone()
                await cur.close()
                existing = dict(row) if row else None
                if existing is None:
                    cur = await self.db.conn.execute(
                        """
                        INSERT INTO pe_registrations(
                          election_id, guild_id, user_id, display_name, selected_field_keys, self_intro,
                          status, registered_at, last_modified_at, public_sync_status
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            election_id,
                            int(election["guild_id"]),
                            int(user_id),
                            display_name,
                            _json_dumps(selected_field_keys),
                            self_intro,
                            REG_ACTIVE,
                            now,
                            now,
                            public_status,
                        ),
                    )
                    reg_id = int(cur.lastrowid)
                    await cur.close()
                else:
                    registered_at = now if existing.get("status") == REG_WITHDRAWN and is_re_register_after_withdraw else existing.get("registered_at") or now
                    await self.db.conn.execute(
                        """
                        UPDATE pe_registrations
                        SET display_name=?, selected_field_keys=?, self_intro=?, status=?, registered_at=?, last_modified_at=?, public_sync_status=?, public_sync_error=NULL
                        WHERE election_id=? AND user_id=?
                        """,
                        (
                            display_name,
                            _json_dumps(selected_field_keys),
                            self_intro,
                            REG_ACTIVE,
                            registered_at,
                            now,
                            public_status if not existing.get("public_message_id") else PUBLIC_PENDING,
                            election_id,
                            int(user_id),
                        ),
                    )
                    reg_id = int(existing["id"])
                cur = await self.db.conn.execute("SELECT * FROM pe_registrations WHERE id=?", (reg_id,))
                saved = await cur.fetchone()
                await cur.close()
                await self.db.conn.commit()
                return dict(saved) if saved else {}
            except Exception:
                await self.db.conn.rollback()
                raise

    async def list_registrations(self, election_id: int, *, statuses: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            rows = await self.db.fetchall(
                f"SELECT * FROM pe_registrations WHERE election_id=? AND status IN ({placeholders}) ORDER BY registered_at ASC, user_id ASC",
                (int(election_id), *statuses),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM pe_registrations WHERE election_id=? ORDER BY registered_at ASC, user_id ASC",
                (int(election_id),),
            )
        return [dict(r) for r in rows]

    async def list_active_registrations(self, election_id: int) -> list[dict[str, Any]]:
        return await self.list_registrations(election_id, statuses=(REG_ACTIVE,))

    async def update_registration_public_message(
        self,
        registration_id: int,
        *,
        channel_id: int | None,
        message_id: int | None,
        status: str,
        error: str | None = None,
    ) -> None:
        await self.db.execute_close(
            """
            UPDATE pe_registrations
            SET public_channel_id=COALESCE(?, public_channel_id), public_message_id=COALESCE(?, public_message_id),
                public_sync_status=?, public_sync_error=?, last_modified_at=?
            WHERE id=?
            """,
            (channel_id, message_id, status, error, utc_now_iso(), int(registration_id)),
        )

    async def set_registration_status(
        self,
        *,
        election_id: int,
        user_id: int,
        status: str,
        reason: str | None = None,
        operator_id: int | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        fields: list[str] = ["status=?", "last_modified_at=?", "public_sync_status=?"]
        params: list[Any] = [status, now, PUBLIC_PENDING]
        if status == REG_REJECTED:
            fields += ["rejected_reason=?", "rejected_by=?", "rejected_at=?"]
            params += [reason, operator_id, now]
        if status == REG_REVOKED:
            fields += ["revoked_reason=?", "revoked_by=?", "revoked_at=?"]
            params += [reason, operator_id, now]
        params += [int(election_id), int(user_id)]
        await self.db.execute_close(
            f"UPDATE pe_registrations SET {', '.join(fields)} WHERE election_id=? AND user_id=?",
            tuple(params),
        )
        row = await self.get_registration(int(election_id), int(user_id))
        if not row:
            raise ValueError("未找到报名记录。")
        return row

    async def count_registrations_by_status(self, election_id: int) -> dict[str, int]:
        rows = await self.db.fetchall(
            "SELECT status, COUNT(*) AS n FROM pe_registrations WHERE election_id=? GROUP BY status",
            (int(election_id),),
        )
        return {str(r["status"]): int(r["n"]) for r in rows}

    # ---------- votes ----------
    async def create_vote(self, election: dict[str, Any]) -> int:
        existing_id = int(election.get("vote_id") or 0)
        if existing_id:
            return existing_id
        now = utc_now_iso()
        return await self.db.insert_and_get_id(
            "INSERT INTO pe_votes(election_id, guild_id, max_selections, created_at) VALUES(?,?,?,?)",
            (int(election["id"]), int(election["guild_id"]), int(election["vote_max_selections"]), now),
        )

    async def get_vote(self, vote_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM pe_votes WHERE id=?", (int(vote_id),))
        return dict(row) if row else None

    async def has_vote_record(self, vote_id: int, voter_id: int) -> bool:
        row = await self.db.fetchone(
            "SELECT 1 FROM pe_vote_records WHERE vote_id=? AND voter_id=? LIMIT 1",
            (int(vote_id), int(voter_id)),
        )
        return row is not None

    async def is_vote_invalidated(self, election_id: int, voter_id: int) -> bool:
        row = await self.db.fetchone(
            "SELECT 1 FROM pe_vote_invalidations WHERE election_id=? AND voter_id=? LIMIT 1",
            (int(election_id), int(voter_id)),
        )
        return row is not None

    async def add_vote_record(self, *, vote_id: int, election_id: int, voter_id: int, selected_user_ids: list[int]) -> None:
        if self.db.conn is None:
            raise RuntimeError("DB not connected")
        async with self.lock:
            try:
                await self.db.conn.execute("BEGIN IMMEDIATE")
                cur = await self.db.conn.execute(
                    "SELECT 1 FROM pe_vote_invalidations WHERE election_id=? AND voter_id=? LIMIT 1",
                    (int(election_id), int(voter_id)),
                )
                invalidated = await cur.fetchone()
                await cur.close()
                if invalidated is not None:
                    raise ValueError("你的投票记录已被管理员作废，不能重新投票。")
                cur = await self.db.conn.execute(
                    "SELECT 1 FROM pe_vote_records WHERE vote_id=? AND voter_id=? LIMIT 1",
                    (int(vote_id), int(voter_id)),
                )
                existing = await cur.fetchone()
                await cur.close()
                if existing is not None:
                    raise ValueError("你已经投过票，投票后不能更改。")
                await self.db.conn.execute(
                    "INSERT INTO pe_vote_records(vote_id, election_id, voter_id, selected_user_ids, created_at) VALUES(?,?,?,?,?)",
                    (int(vote_id), int(election_id), int(voter_id), _json_dumps([str(int(x)) for x in selected_user_ids]), utc_now_iso()),
                )
                await self.db.conn.commit()
            except Exception:
                await self.db.conn.rollback()
                raise

    async def list_vote_records(self, election_id: int) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM pe_vote_records WHERE election_id=? ORDER BY id ASC", (int(election_id),))
        return [dict(r) for r in rows]

    async def count_vote_records(self, election_id: int) -> int:
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM pe_vote_records WHERE election_id=?", (int(election_id),))
        return int(row["n"] if row else 0)

    async def invalidate_vote(self, *, election_id: int, voter_id: int, operator_id: int, reason: str | None = None) -> None:
        election = await self.get_election(election_id)
        vote_id = int(election.get("vote_id") or 0) if election else 0
        if self.db.conn is None:
            raise RuntimeError("DB not connected")
        async with self.lock:
            try:
                await self.db.conn.execute("BEGIN IMMEDIATE")
                await self.db.conn.execute(
                    "DELETE FROM pe_vote_records WHERE election_id=? AND voter_id=?",
                    (int(election_id), int(voter_id)),
                )
                await self.db.conn.execute(
                    "INSERT OR IGNORE INTO pe_vote_invalidations(election_id, vote_id, voter_id, operator_id, reason, created_at) VALUES(?,?,?,?,?,?)",
                    (int(election_id), vote_id or None, int(voter_id), int(operator_id), reason, utc_now_iso()),
                )
                await self.db.conn.commit()
            except Exception:
                await self.db.conn.rollback()
                raise

    # ---------- audit ----------
    async def log(self, election_id: int | None, guild_id: int, operator_id: int | None, action: str, detail: dict[str, Any] | None = None) -> None:
        await self.db.execute_close(
            "INSERT INTO pe_audit_logs(election_id, guild_id, operator_id, action, detail_json, created_at) VALUES(?,?,?,?,?,?)",
            (int(election_id) if election_id else None, int(guild_id), int(operator_id) if operator_id else None, action, _json_dumps(detail or {}), utc_now_iso()),
        )

    async def list_audit_logs(self, guild_id: int, election_id: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
        if election_id is not None:
            rows = await self.db.fetchall(
                "SELECT * FROM pe_audit_logs WHERE guild_id=? AND election_id=? ORDER BY id DESC LIMIT ?",
                (int(guild_id), int(election_id), int(limit)),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM pe_audit_logs WHERE guild_id=? ORDER BY id DESC LIMIT ?",
                (int(guild_id), int(limit)),
            )
        return [dict(r) for r in rows]
