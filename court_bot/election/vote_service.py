from __future__ import annotations

import discord

from .constants import STATUS_VOTING
from .database import ElectionRepo
from .embeds import build_vote_candidate_list_embeds, build_vote_entry_embed, build_vote_page_embed
from .permissions import can_vote, missing_voter_role_message
from .views import VoteEntryView, VoteSelectionView


class VoteService:
    def __init__(self, bot, repo: ElectionRepo):
        self.bot = bot
        self.repo = repo

    async def _registrations_with_field_names(self, election_id: int, registrations: list[dict]) -> list[dict]:
        field_names = await self.repo.get_field_names_by_key(int(election_id))
        enriched: list[dict] = []
        for reg in registrations:
            row = dict(reg)
            selected_keys = self.repo.decode_field_keys(row.get("selected_field_keys"))
            row["field_names"] = [field_names.get(key, key) for key in selected_keys]
            enriched.append(row)
        return enriched

    async def create_vote_panel(self, election: dict, *, channel: discord.TextChannel | None = None) -> discord.Message | None:
        active_regs = await self.repo.list_active_registrations(int(election["id"]))
        if not active_regs:
            return None
        active_regs = await self._registrations_with_field_names(int(election["id"]), active_regs)
        vote_id = await self.repo.create_vote(election)
        if channel is None:
            channel = self.bot.get_channel(int(election.get("voting_channel_id") or 0))
            if channel is None:
                channel = await self.bot.fetch_channel(int(election.get("voting_channel_id") or 0))
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("无法读取投票频道。")
        candidate_list_embeds = build_vote_candidate_list_embeds(election, active_regs)
        msg = await channel.send(
            embeds=[build_vote_entry_embed(election, len(active_regs), guild=channel.guild), *candidate_list_embeds[:1]],
            view=VoteEntryView(cog=self.bot.get_cog("ElectionCog")),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        for embed in candidate_list_embeds[1:]:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        await self.repo.set_vote_message(int(election["id"]), int(vote_id), int(channel.id), int(msg.id))
        # Store channel/message also in pe_votes.
        await self.repo.db.execute_close(
            "UPDATE pe_votes SET channel_id=?, message_id=? WHERE id=?",
            (int(channel.id), int(msg.id), int(vote_id)),
        )
        return msg

    async def open_vote_selection(self, interaction: discord.Interaction, election: dict) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        if election.get("status") != STATUS_VOTING:
            await interaction.response.send_message("当前不在投票阶段。", ephemeral=True)
            return
        allowed_roles = self.repo.decode_role_ids(election.get("allowed_voter_role_ids"))
        if not can_vote(interaction.user, allowed_roles):
            await interaction.response.send_message(missing_voter_role_message(allowed_roles) or "你没有本次募选投票资格。", ephemeral=True)
            return
        vote_id = int(election.get("vote_id") or 0)
        if not vote_id:
            await interaction.response.send_message("投票器尚未初始化，请联系管理员。", ephemeral=True)
            return
        if await self.repo.is_vote_invalidated(int(election["id"]), int(interaction.user.id)):
            await interaction.response.send_message("你的投票记录已被管理员作废，不能重新投票。", ephemeral=True)
            return
        if await self.repo.has_vote_record(vote_id, int(interaction.user.id)):
            await interaction.response.send_message("你已经投过票，投票后不能更改。", ephemeral=True)
            return
        candidates = await self.repo.list_active_registrations(int(election["id"]))
        if not candidates:
            await interaction.response.send_message("当前没有有效候选人。", ephemeral=True)
            return
        candidates = await self._registrations_with_field_names(int(election["id"]), candidates)
        view = VoteSelectionView(cog=self.bot.get_cog("ElectionCog"), election=election, candidates=candidates, voter_id=int(interaction.user.id))
        await interaction.response.send_message(
            embed=build_vote_page_embed(election, 0, view.total_pages, 0, view.max_selectable, view.page_candidates()),
            view=view,
            ephemeral=True,
        )

    async def confirm_vote(self, interaction: discord.Interaction, *, election: dict, selected_user_ids: list[int]) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("请在服务器内使用。", ephemeral=True)
            return
        fresh = await self.repo.get_election(int(election["id"]))
        if not fresh or fresh.get("status") != STATUS_VOTING:
            await interaction.response.send_message("当前不在投票阶段。", ephemeral=True)
            return
        allowed_roles = self.repo.decode_role_ids(fresh.get("allowed_voter_role_ids"))
        if not can_vote(interaction.user, allowed_roles):
            await interaction.response.send_message(missing_voter_role_message(allowed_roles) or "你没有本次募选投票资格。", ephemeral=True)
            return
        vote_id = int(fresh.get("vote_id") or 0)
        if await self.repo.has_vote_record(vote_id, int(interaction.user.id)):
            await interaction.response.send_message("你已经投过票，投票后不能更改。", ephemeral=True)
            return
        candidates = await self.repo.list_active_registrations(int(fresh["id"]))
        valid_user_ids = {int(reg.get("user_id") or 0) for reg in candidates}
        selected = [int(uid) for uid in selected_user_ids if int(uid) in valid_user_ids]
        max_selectable = min(int(fresh.get("vote_max_selections") or 1), len(candidates))
        if not selected:
            await interaction.response.send_message("请选择至少一名有效候选人。", ephemeral=True)
            return
        if len(selected) > max_selectable:
            await interaction.response.send_message(f"选择人数超过上限，最多可选 {max_selectable} 人。", ephemeral=True)
            return
        try:
            await self.repo.add_vote_record(vote_id=vote_id, election_id=int(fresh["id"]), voter_id=int(interaction.user.id), selected_user_ids=selected)
        except Exception as exc:
            await interaction.response.send_message(f"投票失败：{exc}", ephemeral=True)
            return
        await interaction.response.edit_message(content="投票成功。你的投票不会公开，且提交后不能更改。", embed=None, view=None)
