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


def _parse_int_env(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = default
    else:
        value = int(raw.strip())

    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


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

    # 运行资源控制（适合小型 VPS 常驻运行）
    max_message_cache: int
    archive_concurrency: int
    archive_media_budget_mb: int  # 0 表示不限制，保持完整离线归档能力
    archive_single_image_max_mb: int  # 0 表示不限制，保持完整离线归档能力


def load_config() -> Config:
    load_dotenv()

    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN 未设置，请在 .env 中填写")

    command_guild_id = _parse_int(os.getenv("COMMAND_GUILD_ID"))

    db_path = os.getenv("DB_PATH", "data/court.db").strip() or "data/court.db"

    max_message_cache = _parse_int_env("BOT_MAX_MESSAGE_CACHE", 200, minimum=50, maximum=2000)
    archive_concurrency = _parse_int_env("ARCHIVE_CONCURRENCY", 1, minimum=1, maximum=3)
    archive_media_budget_mb = _parse_int_env("ARCHIVE_MEDIA_BUDGET_MB", 0, minimum=0, maximum=4096)
    archive_single_image_max_mb = _parse_int_env("ARCHIVE_SINGLE_IMAGE_MAX_MB", 0, minimum=0, maximum=4096)

    return Config(
        token=token,
        command_guild_id=command_guild_id,
        db_path=db_path,
        max_message_cache=max_message_cache,
        archive_concurrency=archive_concurrency,
        archive_media_budget_mb=archive_media_budget_mb,
        archive_single_image_max_mb=archive_single_image_max_mb,
    )
