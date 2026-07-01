from __future__ import annotations

import re
from typing import Any

from .continuous_constants import CONT_MAX_FIELDS
from .text_utils import sanitize_public_text


def parse_continuous_fields_config(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text:
        raise ValueError("岗位列表不能为空。")
    parts = [part.strip() for part in re.split(r"[,，;；\n]+", text) if part.strip()]
    if not parts:
        raise ValueError("岗位列表不能为空。")
    if len(parts) > CONT_MAX_FIELDS:
        raise ValueError(f"岗位最多 {CONT_MAX_FIELDS} 个。")
    fields: list[str] = []
    seen: set[str] = set()
    for part in parts:
        name = part
        if ":" in part or "：" in part:
            sep = ":" if ":" in part else "："
            left, right = part.split(sep, 1)
            if right.strip().isdigit():
                name = left
        name = sanitize_public_text(name, max_len=80, fallback="").strip()
        if not name:
            raise ValueError("岗位名称不能为空。")
        if name in seen:
            raise ValueError(f"岗位名称重复：{name}")
        seen.add(name)
        fields.append(name)
    return fields


def calculate_application_result(
    *,
    yes_votes: int,
    no_votes: int,
    min_total_votes: int,
    approval_threshold_percent: float,
) -> dict[str, Any]:
    yes = max(0, int(yes_votes or 0))
    no = max(0, int(no_votes or 0))
    total = yes + no
    ratio = (yes / total * 100.0) if total else 0.0
    passed = total >= int(min_total_votes or 0) and ratio >= float(approval_threshold_percent or 0)
    return {
        "passed": passed,
        "yes_votes": yes,
        "no_votes": no,
        "total_votes": total,
        "approval_ratio_percent": ratio,
        "min_total_votes": int(min_total_votes or 0),
        "approval_threshold_percent": float(approval_threshold_percent or 0),
    }


def calculate_support_collection_result(
    *,
    support_votes: int,
    support_target_votes: int,
) -> dict[str, Any]:
    support = max(0, int(support_votes or 0))
    target = max(1, int(support_target_votes or 0))
    return {
        "passed": support >= target,
        "support_votes": support,
        "total_votes": support,
        "support_target_votes": target,
    }
