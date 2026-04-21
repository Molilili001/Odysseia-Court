# 颜色
COLOR_BLUE = 0x3498DB
COLOR_RED = 0xE74C3C
COLOR_YELLOW = 0xF1C40F
COLOR_GREEN = 0x2ECC71
COLOR_GRAY = 0x95A5A6
COLOR_ORANGE = 0xE67E22

# 可见性
VIS_PRIVATE = "private"
VIS_PUBLIC = "public"

# 案件状态
STATUS_UNDER_REVIEW = "under_review"
STATUS_NEEDS_MORE_EVIDENCE = "needs_more_evidence"
STATUS_REJECTED = "rejected"
STATUS_IN_SESSION = "in_session"
STATUS_AWAITING_CONTINUE = "awaiting_continue"
STATUS_AWAITING_JUDGEMENT = "awaiting_judgement"
STATUS_CLOSED = "closed"
STATUS_WITHDRAWN = "withdrawn"

# 角色（回合双方）
SIDE_COMPLAINANT = "complainant"
SIDE_DEFENDANT = "defendant"


ROUND_LABEL = {
    1: "初辩",
    2: "二辩",
    3: "终辩",
}


def round_label(round_number: int) -> str:
    return ROUND_LABEL.get(round_number, "追加辩诉")


def side_label(side: str) -> str:
    if side == SIDE_COMPLAINANT:
        return "投诉人"
    if side == SIDE_DEFENDANT:
        return "被投诉人"
    return side


# 回合发言控制（自主发言模式）
TURN_SPEAK_MINUTES = 10
TURN_MESSAGE_LIMIT = 10
