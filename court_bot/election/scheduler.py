from __future__ import annotations

import logging

from discord.ext import tasks

from .constants import (
    STATUS_REGISTRATION,
    STATUS_REGISTRATION_ENDED,
    STATUS_SETUP,
    STATUS_VOTING,
)
from .time_utils import parse_iso, utc_now

log = logging.getLogger(__name__)


class ElectionScheduler:
    def __init__(self, cog):
        self.cog = cog

    def start(self) -> None:
        if not self.loop.is_running():
            self.loop.start()

    def cancel(self) -> None:
        self.loop.cancel()

    @tasks.loop(seconds=60)
    async def loop(self) -> None:
        try:
            await self.tick()
        except Exception:
            log.exception("Election scheduler tick failed")

    @loop.before_loop
    async def before_loop(self) -> None:
        await self.cog.bot.wait_until_ready()

    async def tick(self) -> None:
        now = utc_now()
        elections = await self.cog.repo.list_active_elections_all()
        for election in elections:
            try:
                await self.process_election(election, now=now)
            except Exception:
                log.exception("Failed to process election scheduler for %s", election.get("id"))
        try:
            await self.cog.continuous.finalize_due_applications()
        except Exception:
            log.exception("Failed to process continuous application scheduler")

    async def process_election(self, election: dict, *, now) -> None:
        status = str(election.get("status"))
        reg_start = parse_iso(election.get("registration_start_at"))
        reg_end = parse_iso(election.get("registration_end_at"))
        vote_start = parse_iso(election.get("voting_start_at"))
        vote_end = parse_iso(election.get("voting_end_at"))
        if not all((reg_start, reg_end, vote_start, vote_end)):
            return

        if status == STATUS_SETUP and now >= reg_start:
            await self.cog.repo.set_election_status(int(election["id"]), STATUS_REGISTRATION)
            await self.cog.repo.log(int(election["id"]), int(election["guild_id"]), None, "scheduler_registration_started", {})
            election = await self.cog.repo.get_election(int(election["id"])) or election
            await self.cog.refresh_registration_entry(election, reason="registration_started")
            status = str(election.get("status"))

        if status == STATUS_REGISTRATION and now >= reg_end:
            await self.cog.close_registration_phase(election)
            election = await self.cog.repo.get_election(int(election["id"])) or election
            status = str(election.get("status"))

        if status == STATUS_REGISTRATION_ENDED and now >= vote_start:
            await self.cog.open_voting_phase(election)
            election = await self.cog.repo.get_election(int(election["id"])) or election
            status = str(election.get("status"))

        if status == STATUS_VOTING and now >= vote_end:
            await self.cog.complete_election(election)
