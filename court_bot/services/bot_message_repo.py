from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager

from .db import Database, utc_now_iso


class BotMessageRepo:
    """Independent message permissions and recoverable edit history on Court's DB."""

    def __init__(self, db: Database, *, transaction_locks: tuple[asyncio.Lock, ...] = ()):
        self.db = db
        # Database helpers commit the shared connection. Wait for existing
        # business transactions before using them, and acquire each lock once.
        self._transaction_locks = tuple(dict.fromkeys(transaction_locks))
        # Repositories sharing the same Database also share the JSON update lock.
        if not hasattr(db, '_bot_message_settings_lock'):
            db._bot_message_settings_lock = asyncio.Lock()
        self._settings_lock = db._bot_message_settings_lock

    @asynccontextmanager
    async def _write_scope(self):
        async with AsyncExitStack() as stack:
            for lock in self._transaction_locks:
                await stack.enter_async_context(lock)
            yield

    async def init_schema(self) -> None:
        async with self._write_scope():
            await self.db.execute_close('''
                CREATE TABLE IF NOT EXISTS bot_message_settings (
                    guild_id INTEGER PRIMARY KEY,
                    allowed_role_ids TEXT NOT NULL DEFAULT '[]'
                )
            ''')
            await self.db.execute_close('''
                CREATE TABLE IF NOT EXISTS bot_message_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    operator_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    before_content TEXT NOT NULL,
                    before_embeds TEXT NOT NULL,
                    after_content TEXT NOT NULL,
                    after_embeds TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'applied'))
                )
            ''')
            await self.db.execute_close('''
                CREATE INDEX IF NOT EXISTS idx_bot_message_history_target
                ON bot_message_history(guild_id, channel_id, message_id, status, id)
            ''')
            await self.db.execute_close('''
                CREATE TRIGGER IF NOT EXISTS bot_message_history_prune
                AFTER UPDATE OF status ON bot_message_history
                WHEN NEW.status = 'applied'
                BEGIN
                    DELETE FROM bot_message_history
                    WHERE id IN (
                        SELECT id FROM bot_message_history
                        WHERE guild_id = NEW.guild_id AND channel_id = NEW.channel_id
                            AND message_id = NEW.message_id AND status = 'applied'
                        ORDER BY id DESC LIMIT -1 OFFSET 30
                    );
                END
            ''')

    async def get_allowed_roles(self, guild_id: int) -> set[int]:
        row = await self.db.fetchone(
            'SELECT allowed_role_ids FROM bot_message_settings WHERE guild_id=?',
            (guild_id,),
        )
        return {int(role_id) for role_id in json.loads(row['allowed_role_ids'])} if row else set()

    async def _change_role(self, guild_id: int, role_id: int, *, add: bool) -> bool:
        async with self._write_scope():
            async with self._settings_lock:
                roles = await self.get_allowed_roles(guild_id)
                if (role_id in roles) == add:
                    return False
                if add:
                    roles.add(role_id)
                else:
                    roles.remove(role_id)
                await self.db.execute_close('''
                    INSERT INTO bot_message_settings(guild_id, allowed_role_ids) VALUES(?, ?)
                    ON CONFLICT(guild_id) DO UPDATE SET allowed_role_ids=excluded.allowed_role_ids
                ''', (guild_id, json.dumps(sorted(roles))))
                return True

    async def add_role(self, guild_id: int, role_id: int) -> bool:
        return await self._change_role(guild_id, role_id, add=True)

    async def remove_role(self, guild_id: int, role_id: int) -> bool:
        return await self._change_role(guild_id, role_id, add=False)

    async def begin(
        self, guild_id: int, channel_id: int, message_id: int, operator_id: int,
        action: str, before: dict, after: dict,
    ) -> int:
        async with self._write_scope():
            return await self.db.insert_and_get_id('''
                INSERT INTO bot_message_history(
                    guild_id, channel_id, message_id, operator_id, action,
                    before_content, before_embeds, after_content, after_embeds, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (guild_id, channel_id, message_id, operator_id, action,
                  before['content'], json.dumps(before['embeds'], ensure_ascii=False),
                  after['content'], json.dumps(after['embeds'], ensure_ascii=False), utc_now_iso()))

    async def finish(self, history_id: int) -> None:
        async with self._write_scope():
            await self.db.execute_close(
                "UPDATE bot_message_history SET status='applied' WHERE id=? AND status='pending'",
                (history_id,),
            )

    async def discard(self, history_id: int) -> None:
        async with self._write_scope():
            await self.db.execute_close(
                "DELETE FROM bot_message_history WHERE id=? AND status='pending'", (history_id,),
            )

    async def _records(self, guild_id: int, channel_id: int, message_id: int, status: str) -> list[dict]:
        rows = await self.db.fetchall('''
            SELECT * FROM bot_message_history
            WHERE guild_id=? AND channel_id=? AND message_id=? AND status=? ORDER BY id DESC
        ''', (guild_id, channel_id, message_id, status))
        records = []
        for row in rows:
            record = dict(row)
            record['before_embeds'] = json.loads(record['before_embeds'])
            record['after_embeds'] = json.loads(record['after_embeds'])
            records.append(record)
        return records

    async def history(self, guild_id: int, channel_id: int, message_id: int) -> list[dict]:
        return await self._records(guild_id, channel_id, message_id, 'applied')

    async def pending(self, guild_id: int, channel_id: int, message_id: int) -> list[dict]:
        return await self._records(guild_id, channel_id, message_id, 'pending')

