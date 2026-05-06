from __future__ import annotations

import discord


def build_candidate_confirm_view(session_id: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="继续留任",
            style=discord.ButtonStyle.success,
            custom_id=f"insp_candidate_keep_{session_id}",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="主动退出",
            style=discord.ButtonStyle.danger,
            custom_id=f"insp_candidate_exit_{session_id}",
        )
    )
    return view


def build_case_invitation_view(case_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="愿意参与",
            style=discord.ButtonStyle.success,
            custom_id=f"insp_case_accept_{int(case_id)}",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="不参与",
            style=discord.ButtonStyle.secondary,
            custom_id=f"insp_case_decline_{int(case_id)}",
        )
    )
    return view


def build_vote_panel_view(case_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="诉求合理",
            style=discord.ButtonStyle.success,
            custom_id=f"insp_vote_yes_{int(case_id)}",
        )
    )
    view.add_item(
        discord.ui.Button(
            label="诉求不合理",
            style=discord.ButtonStyle.danger,
            custom_id=f"insp_vote_no_{int(case_id)}",
        )
    )
    return view
