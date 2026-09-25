from __future__ import annotations

import asyncio
import os
import sqlite3
from typing import Any, Optional, Sequence

import aiosqlite

from .constants import DEFAULT_RETENTION_DAYS, SETTING_ARCHIVE_CHANNEL_ID, SETTING_RETENTION_DAYS
from .utils import utc_now_iso


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS inspection_settings (
  guild_id INTEGER NOT NULL,
  key TEXT NOT NULL,
  value TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (guild_id, key)
);

CREATE TABLE IF NOT EXISTS inspection_candidates (
  guild_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  remove_reason TEXT,
  confirm_session_id TEXT,
  next_confirm_at TEXT,
  confirm_deadline_at TEXT,
  last_confirmed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (guild_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_inspection_candidates_status_due
  ON inspection_candidates(status, next_confirm_at, confirm_deadline_at);

CREATE TABLE IF NOT EXISTS inspection_cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  description TEXT NOT NULL,
  complainant_statement TEXT NOT NULL,
  defendant_statement TEXT NOT NULL,
  material_link TEXT,
  response_deadline_at TEXT NOT NULL,
  ban_deadline_at TEXT NOT NULL,
  vote_deadline_at TEXT,
  discussion_channel_id INTEGER,
  vote_panel_message_id INTEGER,
  verdict TEXT,
  no_ban_timeout_notified INTEGER NOT NULL DEFAULT 0,
  created_by INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  closed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_inspection_cases_guild_status
  ON inspection_cases(guild_id, status);
CREATE INDEX IF NOT EXISTS idx_inspection_cases_response_due
  ON inspection_cases(status, response_deadline_at);
CREATE INDEX IF NOT EXISTS idx_inspection_cases_ban_due
  ON inspection_cases(status, ban_deadline_at);
CREATE INDEX IF NOT EXISTS idx_inspection_cases_vote_due
  ON inspection_cases(status, vote_deadline_at);
CREATE INDEX IF NOT EXISTS idx_inspection_cases_channel
  ON inspection_cases(discussion_channel_id);

CREATE TABLE IF NOT EXISTS inspection_case_responses (
  case_id INTEGER NOT NULL,
  guild_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  responded_at TEXT,
  dm_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (case_id, user_id),
  FOREIGN KEY(case_id) REFERENCES inspection_cases(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_inspection_case_responses_case_status
  ON inspection_case_responses(case_id, status);

CREATE TABLE IF NOT EXISTS inspection_case_bans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL,
  guild_id INTEGER NOT NULL,
  side TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  operator_id INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(case_id) REFERENCES inspection_cases(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_inspection_case_bans_case
  ON inspection_case_bans(case_id);

CREATE TABLE IF NOT EXISTS inspection_case_members (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL,
  guild_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  replaced_by INTEGER,
  replace_reason TEXT,
  selected_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(case_id) REFERENCES inspection_cases(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_inspection_case_members_case_status
  ON inspection_case_members(case_id, status);
CREATE INDEX IF NOT EXISTS idx_inspection_case_members_user
  ON inspection_case_members(case_id, user_id);

CREATE TABLE IF NOT EXISTS inspection_votes (
  case_id INTEGER NOT NULL,
  guild_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  vote TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (case_id, user_id),
  FOREIGN KEY(case_id) REFERENCES inspection_cases(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_inspection_votes_case
  ON inspection_votes(case_id);

CREATE TABLE IF NOT EXISTS inspection_case_archives (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL,
  guild_id INTEGER NOT NULL,
  archive_channel_id INTEGER NOT NULL,
  archive_message_id INTEGER NOT NULL,
  archive_mode TEXT NOT NULL,
  filename TEXT NOT NULL,
  action TEXT NOT NULL,
  operator_id INTEGER,
  warnings_json TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(case_id) REFERENCES inspection_cases(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_inspection_case_archives_case
  ON inspection_case_archives(case_id, created_at);

CREATE TABLE IF NOT EXISTS inspection_member_application_settings (
  guild_id INTEGER PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 0,
  prerequisite_role_id INTEGER,
  inspector_role_id INTEGER,
  approve_threshold INTEGER NOT NULL DEFAULT 1,
  reject_threshold INTEGER NOT NULL DEFAULT 1,
  reject_cooldown_days INTEGER NOT NULL DEFAULT 1,
  review_thread_id INTEGER,
  pass_dm_template TEXT,
  reject_dm_template TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inspection_member_applications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  display_name TEXT NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL,
  settings_snapshot_json TEXT NOT NULL,
  review_message_id INTEGER,
  panel_claimed_at TEXT,
  panel_dirty INTEGER NOT NULL DEFAULT 1,
  panel_revision INTEGER NOT NULL DEFAULT 0,
  review_thread_id INTEGER NOT NULL,
  reminder_sent INTEGER NOT NULL DEFAULT 0,
  submitted_at TEXT NOT NULL,
  completed_at TEXT,
  cooldown_until TEXT,
  dm_status TEXT NOT NULL DEFAULT 'pending',
  dm_error TEXT,
  dm_attempted_at TEXT,
  role_grant_status TEXT NOT NULL DEFAULT 'not_required',
  role_grant_error TEXT,
  role_grant_next_retry_at TEXT,
  role_grant_attempted_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_inspection_member_one_open
  ON inspection_member_applications(guild_id, user_id) WHERE status='reviewing';
CREATE INDEX IF NOT EXISTS idx_inspection_member_retry
  ON inspection_member_applications(status, role_grant_status, role_grant_next_retry_at);

CREATE TABLE IF NOT EXISTS inspection_member_application_votes (
  application_id INTEGER NOT NULL,
  reviewer_id INTEGER NOT NULL,
  reviewer_name TEXT NOT NULL,
  choice TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(application_id, reviewer_id),
  FOREIGN KEY(application_id) REFERENCES inspection_member_applications(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS inspection_member_application_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  application_id INTEGER NOT NULL,
  actor_id INTEGER,
  event_type TEXT NOT NULL,
  old_choice TEXT,
  new_choice TEXT,
  reason TEXT,
  detail_json TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(application_id) REFERENCES inspection_member_applications(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_inspection_member_events_app
  ON inspection_member_application_events(application_id, id);
"""


class InspectionDatabase:
    """监察组独立 SQLite 数据库连接。"""

    def __init__(self, path: str):
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None
        self.lock = asyncio.Lock()

    async def connect(self) -> None:
        if self.conn is not None:
            return
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # 使用 autocommit，避免多个协程在同一连接上用“隐式事务”相互串入。
        # 需要原子性的多语句操作由服务层显式 BEGIN，并持有 self.lock。
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        await self.conn.execute("PRAGMA foreign_keys = ON")
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn is None:
            return
        await self.conn.close()
        self.conn = None

    async def init_schema(self) -> None:
        if self.conn is None:
            raise RuntimeError("Inspection DB not connected")
        await self.conn.executescript(SCHEMA_SQL)
        await self.conn.commit()

    def require_conn(self) -> aiosqlite.Connection:
        if self.conn is None:
            raise RuntimeError("Inspection DB not connected")
        return self.conn

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Cursor:
        conn = self.require_conn()
        async with self.lock:
            return await conn.execute(sql, params)

    async def executemany(self, sql: str, params: Sequence[Sequence[Any]]) -> aiosqlite.Cursor:
        conn = self.require_conn()
        async with self.lock:
            return await conn.executemany(sql, params)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[dict[str, Any]]:
        conn = self.require_conn()
        async with self.lock:
            async with conn.execute(sql, params) as cur:
                row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        conn = self.require_conn()
        async with self.lock:
            async with conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def commit(self) -> None:
        async with self.lock:
            await self.require_conn().commit()

    async def rollback(self) -> None:
        async with self.lock:
            await self.require_conn().rollback()


async def ensure_default_settings(db: InspectionDatabase, guild_id: int) -> None:
    """确保某服务器至少有默认留任周期设置与可选归档频道设置。"""

    now = utc_now_iso()
    await db.executemany(
        """
        INSERT INTO inspection_settings(guild_id, key, value, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, key) DO NOTHING
        """,
        (
            (int(guild_id), SETTING_RETENTION_DAYS, str(DEFAULT_RETENTION_DAYS), now, now),
            (int(guild_id), SETTING_ARCHIVE_CHANNEL_ID, "", now, now),
        ),
    )
    await db.commit()
