from __future__ import annotations

from dataclasses import dataclass

# 数据库与后台任务默认值
INSPECTION_DB_PATH = "data/inspection.sqlite"
DEFAULT_RETENTION_DAYS = 30
CONFIRM_GRACE_DAYS = 7
CANDIDATE_MAINTENANCE_INTERVAL_HOURS = 1
CASE_MAINTENANCE_INTERVAL_MINUTES = 1

# 设置 key
SETTING_CANDIDATE_ROLE_ID = "candidate_role_id"
SETTING_ADMIN_NOTICE_CHANNEL_ID = "admin_notice_channel_id"
SETTING_DISCUSSION_CATEGORY_ID = "discussion_category_id"
SETTING_VERDICT_CHANNEL_ID = "verdict_channel_id"
SETTING_RETENTION_DAYS = "retention_days"
SETTING_ARCHIVE_CHANNEL_ID = "archive_channel_id"

REQUIRED_SETTING_KEYS = (
    SETTING_CANDIDATE_ROLE_ID,
    SETTING_ADMIN_NOTICE_CHANNEL_ID,
    SETTING_DISCUSSION_CATEGORY_ID,
    SETTING_VERDICT_CHANNEL_ID,
    SETTING_RETENTION_DAYS,
)

# 候补状态
CANDIDATE_ACTIVE = "active"
CANDIDATE_CONFIRM_DM_FAILED = "confirm_dm_failed"
CANDIDATE_REMOVED = "removed"
CANDIDATE_SELF_EXIT = "self_exit"

ACTIVE_CANDIDATE_STATUSES = (CANDIDATE_ACTIVE, CANDIDATE_CONFIRM_DM_FAILED)

# 案件状态
CASE_COLLECTING_RESPONSES = "collecting_responses"
CASE_BAN_PENDING = "ban_pending"
CASE_BLOCKED_INSUFFICIENT_RESPONSES = "blocked_insufficient_responses"
CASE_ACTIVE_DISCUSSION = "active_discussion"
CASE_VOTING = "voting"
CASE_VERDICT_PUBLISHED = "verdict_published"
CASE_CANCELLED = "cancelled"

OPEN_CASE_STATUSES = (
    CASE_COLLECTING_RESPONSES,
    CASE_BAN_PENDING,
    CASE_ACTIVE_DISCUSSION,
    CASE_VOTING,
)

# 响应状态
RESPONSE_INVITED = "invited"
RESPONSE_WILLING = "willing"
RESPONSE_DECLINED = "declined"
RESPONSE_DM_FAILED = "dm_failed"
RESPONSE_SELECTED = "selected"
RESPONSE_NOT_SELECTED = "not_selected"
RESPONSE_BANNED = "banned"

# 临时监察成员状态
CASE_MEMBER_SELECTED = "selected"
CASE_MEMBER_REPLACED = "replaced"

# Ban 方
BAN_SIDE_COMPLAINANT = "complainant"
BAN_SIDE_DEFENDANT = "defendant"

# 投票
VOTE_YES = "yes"
VOTE_NO = "no"
VERDICT_REASONABLE = "诉求合理"
VERDICT_UNREASONABLE = "诉求不合理"
VERDICT_NO_MAJORITY = "未形成多数"

# Slash command choice values
MEMBER_OP_ADD = "add"
MEMBER_OP_REMOVE = "remove"
MEMBER_OP_SELF_EXIT = "self_exit"
MEMBER_OP_LIST = "list"
MEMBER_OP_CONFIRM = "confirm"

CASE_OP_STATUS = "status"
CASE_OP_BAN_DRAW = "ban_draw"
CASE_OP_DRAW = "draw"
CASE_OP_REPLACE = "replace"
CASE_OP_CANCEL = "cancel"

ARCHIVE_ACTION_ONLY = "archive_only"
ARCHIVE_ACTION_LOCK = "archive_lock"
ARCHIVE_ACTION_DELETE = "archive_delete"
ARCHIVABLE_CASE_STATUSES = (CASE_VERDICT_PUBLISHED, CASE_CANCELLED)


@dataclass(frozen=True)
class BanRule:
    slots_per_side: int
    minimum_remaining: int


def ban_rule_for_willing_count(willing_count: int) -> BanRule:
    """根据愿意参与人数计算每方 Ban 位与 Ban 后最低保留人数。"""

    if willing_count >= 8:
        return BanRule(slots_per_side=2, minimum_remaining=4)
    if willing_count >= 5:
        return BanRule(slots_per_side=1, minimum_remaining=3)
    return BanRule(slots_per_side=0, minimum_remaining=3)


def draw_size_for_available_count(available_count: int) -> int:
    """根据可抽取人数计算临时监察组人数；最终人数必须为单数。"""

    if available_count >= 7:
        return 7
    if available_count >= 5:
        return 5
    if available_count >= 3:
        return 3
    return 0
