from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS guild_settings (
  guild_id INTEGER PRIMARY KEY,

  -- JSON array string，例如：[123,456]
  admin_role_ids TEXT NOT NULL,

  review_channel_id INTEGER,
  court_category_id INTEGER,
  -- 旧字段（已弃用）：公开 Forum 频道
  public_forum_channel_id INTEGER,
  judge_panel_channel_id INTEGER,
  audit_log_channel_id INTEGER,

  -- 新增：公开案件观众身份组（只读）
  audience_role_id INTEGER,
  -- 新增：归档频道（仅管理可见）
  archive_channel_id INTEGER,

  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_guild_settings_updated_at ON guild_settings(updated_at);

CREATE TABLE IF NOT EXISTS cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER NOT NULL,

  complainant_id INTEGER NOT NULL,
  defendant_id INTEGER NOT NULL,

  requested_visibility TEXT NOT NULL,
  approved_visibility TEXT,

  status TEXT NOT NULL,
  status_reason TEXT,

  rule_text TEXT NOT NULL,
  description TEXT NOT NULL,

  review_channel_id INTEGER,
  review_message_id INTEGER,

  court_channel_id INTEGER,
  court_thread_id INTEGER,
  court_panel_message_id INTEGER,

  judge_panel_channel_id INTEGER,
  judge_panel_message_id INTEGER,

  current_round INTEGER NOT NULL DEFAULT 1,
  current_side TEXT NOT NULL DEFAULT 'complainant',

  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(status);
CREATE INDEX IF NOT EXISTS idx_cases_court_channel ON cases(court_channel_id);
CREATE INDEX IF NOT EXISTS idx_cases_court_thread ON cases(court_thread_id);

CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL,
  provider_id INTEGER NOT NULL,

  type TEXT NOT NULL,
  label TEXT,
  url TEXT,
  content_type TEXT,
  size INTEGER,

  note TEXT,

  created_at TEXT NOT NULL,

  FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_evidence_case_id ON evidence(case_id);

CREATE TABLE IF NOT EXISTS statements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL,
  round INTEGER NOT NULL,
  side TEXT NOT NULL,
  content TEXT NOT NULL,
  submitted_by INTEGER NOT NULL,
  message_id INTEGER,
  created_at TEXT NOT NULL,

  FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_statements_case_id ON statements(case_id);

CREATE TABLE IF NOT EXISTS judgements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL,
  decision TEXT NOT NULL,
  penalty TEXT NOT NULL,
  operator_id INTEGER NOT NULL,
  published_message_id INTEGER,
  created_at TEXT NOT NULL,

  FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_judgements_case_id ON judgements(case_id);

CREATE TABLE IF NOT EXISTS case_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  operator_id INTEGER,
  meta_json TEXT,
  created_at TEXT NOT NULL,

  FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_case_logs_case_id ON case_logs(case_id);

CREATE TABLE IF NOT EXISTS continue_state (
  case_id INTEGER PRIMARY KEY,
  panel_message_id INTEGER,
  complainant_choice TEXT,
  defendant_choice TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_continue_state_case_id ON continue_state(case_id);

CREATE TABLE IF NOT EXISTS turn_state (
  case_id INTEGER PRIMARY KEY,
  channel_id INTEGER NOT NULL,
  speaker_id INTEGER NOT NULL,
  expires_at TEXT NOT NULL,
  msg_count INTEGER NOT NULL DEFAULT 0,
  msg_limit INTEGER NOT NULL DEFAULT 10,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_turn_state_expires_at ON turn_state(expires_at);
CREATE INDEX IF NOT EXISTS idx_turn_state_case_id ON turn_state(case_id);

CREATE TABLE IF NOT EXISTS archive_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL,
  guild_id INTEGER NOT NULL,
  archive_channel_id INTEGER NOT NULL,
  summary_message_id INTEGER,
  file_message_ids TEXT,
  mode TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_archive_records_case_id ON archive_records(case_id);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        if self.conn is not None:
            return
        self.conn = await aiosqlite.connect(self.path)
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
            raise RuntimeError("DB not connected")
        await self.conn.executescript(SCHEMA_SQL)
        await self.conn.commit()

        # -------------------- 轻量迁移（兼容旧库） --------------------
        # SQLite 的 CREATE TABLE IF NOT EXISTS 不会为旧表补列，因此这里做安全 ALTER。
        # 若列已存在会抛 duplicate column name，忽略即可。
        alter_sqls = [
            "ALTER TABLE guild_settings ADD COLUMN audience_role_id INTEGER",
            "ALTER TABLE guild_settings ADD COLUMN archive_channel_id INTEGER",
        ]
        for sql in alter_sqls:
            try:
                await self.conn.execute(sql)
            except sqlite3.OperationalError as e:
                if "duplicate column name" in str(e):
                    continue
                # 兼容某些 SQLite 版本的报错文案
                if "already exists" in str(e):
                    continue
                raise
        await self.conn.commit()

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Cursor:
        if self.conn is None:
            raise RuntimeError("DB not connected")
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur

    async def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> Optional[sqlite3.Row]:
        if self.conn is None:
            raise RuntimeError("DB not connected")
        cur = await self.conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row

    async def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        if self.conn is None:
            raise RuntimeError("DB not connected")
        cur = await self.conn.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return list(rows)


@dataclass
class Case:
    id: int


class CaseRepo:
    def __init__(self, db: Database):
        self.db = db

    async def create_case(
        self,
        *,
        guild_id: int,
        complainant_id: int,
        defendant_id: int,
        requested_visibility: str,
        rule_text: str,
        description: str,
    ) -> int:
        now = utc_now_iso()
        cur = await self.db.execute(
            """
            INSERT INTO cases(
              guild_id,
              complainant_id, defendant_id,
              requested_visibility,
              status,
              rule_text, description,
              created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                guild_id,
                complainant_id,
                defendant_id,
                requested_visibility,
                "under_review",
                rule_text,
                description,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)

    async def get_case(self, case_id: int) -> Optional[dict]:
        row = await self.db.fetchone("SELECT * FROM cases WHERE id=?", (case_id,))
        return dict(row) if row else None

    async def find_case_by_space_id(self, guild_id: int, channel_or_thread_id: int) -> Optional[dict]:
        row = await self.db.fetchone(
            """
            SELECT * FROM cases
            WHERE guild_id=? AND (court_channel_id=? OR court_thread_id=?)
            ORDER BY id DESC LIMIT 1
            """,
            (guild_id, channel_or_thread_id, channel_or_thread_id),
        )
        return dict(row) if row else None

    async def list_evidence(self, case_id: int) -> list[dict]:
        rows = await self.db.fetchall("SELECT * FROM evidence WHERE case_id=? ORDER BY id ASC", (case_id,))
        return [dict(r) for r in rows]

    async def add_evidence(
        self,
        *,
        case_id: int,
        provider_id: int,
        ev_type: str,
        label: str | None,
        url: str | None,
        content_type: str | None = None,
        size: int | None = None,
        note: str | None = None,
    ) -> int:
        now = utc_now_iso()
        cur = await self.db.execute(
            """
            INSERT INTO evidence(case_id, provider_id, type, label, url, content_type, size, note, created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (case_id, provider_id, ev_type, label, url, content_type, size, note, now),
        )
        return int(cur.lastrowid)

    async def set_review_message(self, case_id: int, channel_id: int, message_id: int) -> None:
        await self.db.execute(
            "UPDATE cases SET review_channel_id=?, review_message_id=?, updated_at=? WHERE id=?",
            (channel_id, message_id, utc_now_iso(), case_id),
        )

    async def set_status(self, case_id: int, status: str, reason: str | None = None) -> None:
        await self.db.execute(
            "UPDATE cases SET status=?, status_reason=?, updated_at=? WHERE id=?",
            (status, reason, utc_now_iso(), case_id),
        )

    async def approve_case(self, case_id: int, approved_visibility: str) -> None:
        await self.db.execute(
            "UPDATE cases SET status=?, approved_visibility=?, updated_at=? WHERE id=?",
            ("in_session", approved_visibility, utc_now_iso(), case_id),
        )

    async def set_court_space(
        self,
        case_id: int,
        *,
        court_channel_id: int | None,
        court_thread_id: int | None,
    ) -> None:
        await self.db.execute(
            """
            UPDATE cases
            SET court_channel_id=?, court_thread_id=?, updated_at=?
            WHERE id=?
            """,
            (court_channel_id, court_thread_id, utc_now_iso(), case_id),
        )

    async def set_court_panel_message(self, case_id: int, message_id: int) -> None:
        await self.db.execute(
            "UPDATE cases SET court_panel_message_id=?, updated_at=? WHERE id=?",
            (message_id, utc_now_iso(), case_id),
        )


    async def clear_court_space(self, case_id: int) -> None:
        await self.db.execute(
            "UPDATE cases SET court_channel_id=NULL, court_thread_id=NULL, court_panel_message_id=NULL, updated_at=? WHERE id=?",
            (utc_now_iso(), case_id),
        )

    async def add_statement(
        self,
        *,
        case_id: int,
        round_number: int,
        side: str,
        content: str,
        submitted_by: int,
        message_id: int | None,
    ) -> int:
        now = utc_now_iso()
        cur = await self.db.execute(
            """
            INSERT INTO statements(case_id, round, side, content, submitted_by, message_id, created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (case_id, round_number, side, content, submitted_by, message_id, now),
        )
        return int(cur.lastrowid)

    async def advance_turn(self, case_id: int) -> dict:
        case = await self.get_case(case_id)
        if not case:
            raise RuntimeError("case not found")

        current_round = int(case.get("current_round") or 1)
        current_side = case.get("current_side") or "complainant"

        if current_side == "complainant":
            next_round = current_round
            next_side = "defendant"
            next_status = "in_session"
        else:
            next_round = current_round + 1
            next_side = "complainant"
            # 第 3 轮（及之后）结束后暂停，让双方决定是否继续辩诉
            if current_round >= 3:
                next_status = "awaiting_continue"
            else:
                next_status = "in_session"

        await self.db.execute(
            """
            UPDATE cases
            SET status=?, current_round=?, current_side=?, updated_at=?
            WHERE id=?
            """,
            (next_status, next_round, next_side, utc_now_iso(), case_id),
        )
        updated = await self.get_case(case_id)
        if not updated:
            raise RuntimeError("case not found after update")
        return updated

    async def set_judge_panel_message(self, case_id: int, channel_id: int, message_id: int) -> None:
        await self.db.execute(
            "UPDATE cases SET judge_panel_channel_id=?, judge_panel_message_id=?, updated_at=? WHERE id=?",
            (channel_id, message_id, utc_now_iso(), case_id),
        )

    async def create_judgement(
        self,
        *,
        case_id: int,
        decision: str,
        penalty: str,
        operator_id: int,
        published_message_id: int | None,
    ) -> int:
        now = utc_now_iso()
        cur = await self.db.execute(
            """
            INSERT INTO judgements(case_id, decision, penalty, operator_id, published_message_id, created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (case_id, decision, penalty, operator_id, published_message_id, now),
        )
        return int(cur.lastrowid)


    async def get_latest_judgement(self, case_id: int) -> Optional[dict]:
        row = await self.db.fetchone(
            "SELECT * FROM judgements WHERE case_id=? ORDER BY id DESC LIMIT 1",
            (case_id,),
        )
        return dict(row) if row else None

    async def log(self, case_id: int, action: str, operator_id: int | None, meta: dict | None = None) -> None:
        now = utc_now_iso()
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        await self.db.execute(
            """
            INSERT INTO case_logs(case_id, action, operator_id, meta_json, created_at)
            VALUES(?,?,?,?,?)
            """,
            (case_id, action, operator_id, meta_json, now),
        )

    async def get_latest_log_by_action(self, case_id: int, action: str) -> Optional[dict]:
        row = await self.db.fetchone(
            "SELECT * FROM case_logs WHERE case_id=? AND action=? ORDER BY id DESC LIMIT 1",
            (case_id, action),
        )
        if not row:
            return None
        data = dict(row)
        meta_json = data.get("meta_json")
        if meta_json:
            try:
                data["meta"] = json.loads(meta_json)
            except Exception:
                data["meta"] = None
        else:
            data["meta"] = None
        return data

    async def list_cases_for_restore(self) -> list[dict]:
        rows = await self.db.fetchall(
            """
            SELECT * FROM cases
            WHERE status IN ('under_review','needs_more_evidence','in_session','awaiting_continue','awaiting_judgement','closed','withdrawn')
              AND (status NOT IN ('closed','withdrawn') OR court_channel_id IS NOT NULL)
            """
        )
        return [dict(r) for r in rows]

    # -------------------- 继续/结束辩诉（双方同意机制） --------------------

    async def get_continue_state(self, case_id: int) -> Optional[dict]:
        row = await self.db.fetchone("SELECT * FROM continue_state WHERE case_id=?", (case_id,))
        return dict(row) if row else None

    async def upsert_continue_state(
        self,
        *,
        case_id: int,
        panel_message_id: int,
        complainant_choice: str | None = None,
        defendant_choice: str | None = None,
    ) -> None:
        now = utc_now_iso()
        await self.db.execute(
            """
            INSERT INTO continue_state(case_id, panel_message_id, complainant_choice, defendant_choice, created_at, updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(case_id) DO UPDATE SET
              panel_message_id=excluded.panel_message_id,
              complainant_choice=excluded.complainant_choice,
              defendant_choice=excluded.defendant_choice,
              updated_at=excluded.updated_at
            """,
            (case_id, panel_message_id, complainant_choice, defendant_choice, now, now),
        )

    async def set_continue_choice(self, *, case_id: int, side: str, choice: str) -> dict:
        """side: complainant/defendant; choice: continue/end"""

        state = await self.get_continue_state(case_id)
        if not state:
            # 未创建面板时先创建一个空状态（panel_message_id 由调用方后续覆盖）
            await self.upsert_continue_state(case_id=case_id, panel_message_id=0)

        now = utc_now_iso()
        if side == 'complainant':
            await self.db.execute(
                "UPDATE continue_state SET complainant_choice=?, updated_at=? WHERE case_id=?",
                (choice, now, case_id),
            )
        else:
            await self.db.execute(
                "UPDATE continue_state SET defendant_choice=?, updated_at=? WHERE case_id=?",
                (choice, now, case_id),
            )

        updated = await self.get_continue_state(case_id)
        if not updated:
            raise RuntimeError('continue_state not found')
        return updated

    async def clear_continue_state(self, case_id: int) -> None:
        await self.db.execute("DELETE FROM continue_state WHERE case_id=?", (case_id,))

    # -------------------- 发言权窗口（turn_state） --------------------

    async def get_turn_state(self, case_id: int) -> Optional[dict]:
        row = await self.db.fetchone("SELECT * FROM turn_state WHERE case_id=?", (case_id,))
        return dict(row) if row else None

    async def upsert_turn_state(
        self,
        *,
        case_id: int,
        channel_id: int,
        speaker_id: int,
        expires_at: str,
        msg_count: int = 0,
        msg_limit: int = 10,
    ) -> None:
        now = utc_now_iso()
        await self.db.execute(
            """
            INSERT INTO turn_state(case_id, channel_id, speaker_id, expires_at, msg_count, msg_limit, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(case_id) DO UPDATE SET
              channel_id=excluded.channel_id,
              speaker_id=excluded.speaker_id,
              expires_at=excluded.expires_at,
              msg_count=excluded.msg_count,
              msg_limit=excluded.msg_limit,
              updated_at=excluded.updated_at
            """,
            (case_id, channel_id, speaker_id, expires_at, msg_count, msg_limit, now, now),
        )

    async def increment_turn_msg_count(self, case_id: int, *, delta: int = 1) -> Optional[int]:
        await self.db.execute(
            "UPDATE turn_state SET msg_count = msg_count + ?, updated_at=? WHERE case_id=?",
            (delta, utc_now_iso(), case_id),
        )
        st = await self.get_turn_state(case_id)
        return int(st["msg_count"]) if st else None

    async def set_turn_msg_count(self, case_id: int, *, msg_count: int) -> None:
        await self.db.execute(
            "UPDATE turn_state SET msg_count=?, updated_at=? WHERE case_id=?",
            (msg_count, utc_now_iso(), case_id),
        )

    async def clear_turn_state(self, case_id: int) -> None:
        await self.db.execute("DELETE FROM turn_state WHERE case_id=?", (case_id,))

    async def list_expired_turn_states(self, *, now_iso: str | None = None) -> list[dict]:
        now = now_iso or utc_now_iso()
        rows = await self.db.fetchall(
            "SELECT * FROM turn_state WHERE expires_at <= ? ORDER BY expires_at ASC",
            (now,),
        )
        return [dict(r) for r in rows]



class GuildSettingsRepo:
    """服务器级配置。

    用于避免把频道/分类/Forum 等写死在 .env 里。
    管理员可通过 `/类脑大法庭 设置` 在服务器内完成配置，存入 SQLite。
    """

    def __init__(self, db: Database):
        self.db = db

    async def get_settings(self, guild_id: int) -> Optional[dict]:
        row = await self.db.fetchone("SELECT * FROM guild_settings WHERE guild_id=?", (guild_id,))
        if not row:
            return None

        data = dict(row)
        try:
            data["admin_role_ids"] = set(json.loads(data.get("admin_role_ids") or "[]"))
        except Exception:
            data["admin_role_ids"] = set()
        return data

    async def upsert_settings(
        self,
        *,
        guild_id: int,
        admin_role_ids: list[int],
        review_channel_id: int,
        court_category_id: int,
        public_forum_channel_id: int | None = None,
        judge_panel_channel_id: int,
        audit_log_channel_id: int | None,
        audience_role_id: int | None = None,
        archive_channel_id: int | None = None,
    ) -> None:
        now = utc_now_iso()
        admin_role_ids_json = json.dumps(sorted(set(admin_role_ids)), ensure_ascii=False)

        await self.db.execute(
            """
            INSERT INTO guild_settings(
              guild_id,
              admin_role_ids,
              review_channel_id,
              court_category_id,
              public_forum_channel_id,
              judge_panel_channel_id,
              audit_log_channel_id,
              audience_role_id,
              archive_channel_id,
              created_at,
              updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(guild_id) DO UPDATE SET
              admin_role_ids=excluded.admin_role_ids,
              review_channel_id=excluded.review_channel_id,
              court_category_id=excluded.court_category_id,
              public_forum_channel_id=excluded.public_forum_channel_id,
              judge_panel_channel_id=excluded.judge_panel_channel_id,
              audit_log_channel_id=excluded.audit_log_channel_id,
              audience_role_id=excluded.audience_role_id,
              archive_channel_id=excluded.archive_channel_id,
              updated_at=excluded.updated_at
            """,
            (
                guild_id,
                admin_role_ids_json,
                review_channel_id,
                court_category_id,
                public_forum_channel_id,
                judge_panel_channel_id,
                audit_log_channel_id,
                audience_role_id,
                archive_channel_id,
                now,
                now,
            ),
        )
