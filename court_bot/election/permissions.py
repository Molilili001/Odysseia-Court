from __future__ import annotations

import discord


def is_election_admin(member: discord.Member | discord.User | discord.abc.User) -> bool:
    """Initial election admin rule: Administrator or Manage Guild.

    This intentionally does not reuse court admin_role_ids or inspection owner/admin logic.
    """

    if not isinstance(member, discord.Member):
        return False
    perms = member.guild_permissions
    return bool(perms.administrator or perms.manage_guild)


def has_any_role(member: discord.Member, role_ids: list[int] | tuple[int, ...] | set[int]) -> bool:
    if not role_ids:
        return True
    allowed = {int(role_id) for role_id in role_ids}
    return any(role.id in allowed for role in member.roles)


def can_vote(member: discord.Member, allowed_voter_role_ids: list[int] | tuple[int, ...] | set[int]) -> bool:
    """OR rule: no configured roles means all members; otherwise any role matches."""

    return has_any_role(member, allowed_voter_role_ids)


def can_register(member: discord.Member, allowed_candidate_role_ids: list[int] | tuple[int, ...] | set[int]) -> bool:
    """OR rule: no configured roles means all members; otherwise any role matches."""

    return has_any_role(member, allowed_candidate_role_ids)


def missing_candidate_role_message(allowed_role_ids: list[int] | tuple[int, ...] | set[int]) -> str:
    if not allowed_role_ids:
        return ""
    roles = "、".join(f"<@&{int(role_id)}>" for role_id in allowed_role_ids)
    return f"你没有本次募选报名资格；需要拥有以下任意一个身份组：{roles}。"


def missing_voter_role_message(allowed_role_ids: list[int] | tuple[int, ...] | set[int]) -> str:
    if not allowed_role_ids:
        return ""
    roles = "、".join(f"<@&{int(role_id)}>" for role_id in allowed_role_ids)
    return f"你没有本次募选投票资格；需要拥有以下任意一个身份组：{roles}。"
