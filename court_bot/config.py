from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv


def _parse_int(value: str | None) -> Optional[int]:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    return int(value)


def _parse_int_set(value: str | None) -> set[int]:
    if not value:
        return set()
    out: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        out.add(int(part))
    return out


@dataclass(frozen=True)
class Config:
    token: str

    # 指令同步（开发期建议设置为你的服务器 ID）
    command_guild_id: Optional[int]

    # 数据库
    db_path: str


def load_config() -> Config:
    load_dotenv()

    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN 未设置，请在 .env 中填写")

    command_guild_id = _parse_int(os.getenv("COMMAND_GUILD_ID"))

    db_path = os.getenv("DB_PATH", "data/court.db").strip() or "data/court.db"

    return Config(
        token=token,
        command_guild_id=command_guild_id,
        db_path=db_path,
    )
