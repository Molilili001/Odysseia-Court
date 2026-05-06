from __future__ import annotations

import re

MENTION_RE = re.compile(r"<@!?\d+>|<@&\d+>|<#\d+>")
FORBIDDEN_MENTION_RE = re.compile(r"<@!?\d+>|<@&\d+>|@everyone|@here", re.IGNORECASE)


def contains_forbidden_mention(value: object) -> bool:
    return bool(FORBIDDEN_MENTION_RE.search(str(value or "")))


def sanitize_public_text(value: object, *, max_len: int | None = None, fallback: str = "未填写") -> str:
    """Sanitize text before posting to public election embeds.

    Prevent public candidate statements from rendering mentions or mass pings.
    """

    text = str(value or "").strip()
    if not text:
        text = fallback
    text = MENTION_RE.sub(lambda m: m.group(0).replace("<", "‹").replace(">", "›"), text)
    text = text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    text = text.replace("<", "‹").replace(">", "›")
    if max_len is not None and len(text) > max_len:
        return text[: max(0, max_len - 1)] + "…"
    return text


def compact(value: object, *, max_len: int = 100) -> str:
    text = sanitize_public_text(value, max_len=max_len, fallback="")
    return re.sub(r"\s+", " ", text).strip()


def split_lines_for_embed(lines: list[str], *, max_chars: int = 3800) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        addition = len(line) + 1
        if current and size + addition > max_chars:
            chunks.append("\n".join(current))
            current = []
            size = 0
        current.append(line)
        size += addition
    if current:
        chunks.append("\n".join(current))
    return chunks or ["（无）"]
