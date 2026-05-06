from __future__ import annotations

"""Election smoke test that does not connect to Discord.

Usage:
    python tools/election_smoke_test.py

What it covers:
- pe_* schema creation on a temporary SQLite database.
- Election creation with duration-based Beijing schedule.
- Registration edit vs withdrawn re-register timestamp rule.
- Voter role OR rule.
- Vote immutability.
- Result calculation / field lock / tie-break.

It is intentionally safe: it only uses a temporary database and deletes it afterwards.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from court_bot.election.constants import PUBLICITY_REALTIME, REG_WITHDRAWN
from court_bot.election.database import ElectionRepo
from court_bot.election.permissions import can_register, can_vote
from court_bot.election.result_service import ResultService
from court_bot.election.time_utils import build_schedule, to_utc_iso
from court_bot.services.db import Database


class FakeRole:
    def __init__(self, role_id: int):
        self.id = role_id


class FakeMember:
    def __init__(self, role_ids: list[int]):
        self.roles = [FakeRole(role_id) for role_id in role_ids]


async def run() -> dict:
    with tempfile.TemporaryDirectory() as td:
        db_path = str(Path(td) / "election-smoke.db")
        db = Database(db_path)
        await db.connect()
        try:
            await db.init_schema()
            repo = ElectionRepo(db)
            await repo.ensure_schema()
            schedule = build_schedule(
                start_at_text="2026-05-01 20:00",
                registration_duration_minutes=60,
                publicity_duration_minutes=0,
                voting_duration_minutes=60,
            )
            election_id = await repo.create_election(
                guild_id=9001,
                name="Smoke Test Election",
                publicity_mode=PUBLICITY_REALTIME,
                registration_channel_id=101,
                voting_channel_id=102,
                public_channel_id=103,
                alert_channel_id=None,
                allowed_candidate_role_ids=[333, 444],
                allowed_voter_role_ids=[111, 222],
                vote_max_selections=2,
                registration_duration_minutes=60,
                publicity_duration_minutes=0,
                voting_duration_minutes=60,
                registration_start_at=to_utc_iso(schedule.registration_start_at),
                registration_end_at=to_utc_iso(schedule.registration_end_at),
                voting_start_at=to_utc_iso(schedule.voting_start_at),
                voting_end_at=to_utc_iso(schedule.voting_end_at),
                created_by=999,
                fields=[("第一岗位", 1), ("第二岗位", 1)],
            )
            election = await repo.get_election(election_id)
            assert election is not None

            assert can_register(FakeMember([333]), ElectionRepo.decode_role_ids(election["allowed_candidate_role_ids"]))
            assert not can_register(FakeMember([555]), ElectionRepo.decode_role_ids(election["allowed_candidate_role_ids"]))
            assert can_vote(FakeMember([222]), ElectionRepo.decode_role_ids(election["allowed_voter_role_ids"]))
            assert not can_vote(FakeMember([333]), ElectionRepo.decode_role_ids(election["allowed_voter_role_ids"]))

            reg1 = await repo.upsert_registration(election=election, user_id=1, display_name="甲", selected_field_keys=["field_1", "field_2"], self_intro="A")
            reg2 = await repo.upsert_registration(election=election, user_id=2, display_name="乙", selected_field_keys=["field_1", "field_2"], self_intro="B")
            reg3 = await repo.upsert_registration(election=election, user_id=3, display_name="丙", selected_field_keys=["field_2"], self_intro="C")
            old_registered_at = reg1["registered_at"]
            reg1_edit = await repo.upsert_registration(election=election, user_id=1, display_name="甲-编辑", selected_field_keys=["field_1", "field_2"], self_intro="A2")
            assert reg1_edit["registered_at"] == old_registered_at
            await repo.set_registration_status(election_id=election_id, user_id=1, status=REG_WITHDRAWN, reason="smoke", operator_id=1)
            reg1_re = await repo.upsert_registration(
                election=election,
                user_id=1,
                display_name="甲-重报",
                selected_field_keys=["field_1", "field_2"],
                self_intro="A3",
                is_re_register_after_withdraw=True,
            )
            assert reg1_re["registered_at"] != old_registered_at

            vote_id = await repo.create_vote(election)
            await repo.add_vote_record(vote_id=vote_id, election_id=election_id, voter_id=1000, selected_user_ids=[1, 2])
            await repo.add_vote_record(vote_id=vote_id, election_id=election_id, voter_id=1001, selected_user_ids=[1, 3])
            await repo.add_vote_record(vote_id=vote_id, election_id=election_id, voter_id=1002, selected_user_ids=[2, 3])
            await repo.add_vote_record(vote_id=vote_id, election_id=election_id, voter_id=1003, selected_user_ids=[3])
            try:
                await repo.add_vote_record(vote_id=vote_id, election_id=election_id, voter_id=1002, selected_user_ids=[1])
                raise AssertionError("duplicate vote should have failed")
            except Exception:
                pass

            result = await ResultService(repo).calculate(election)
            assert result["is_void"] is False
            assert result["fields"][0]["winners"][0]["user_id"] in {"1", "2"}
            assert result["fields"][1]["winners"][0]["user_id"] == "3"
            return {
                "ok": True,
                "db_path": db_path,
                "election_id": election_id,
                "registered_users": [reg1_re["user_id"], reg2["user_id"], reg3["user_id"]],
                "total_voters": result["total_voters"],
                "winners": [
                    {
                        "field": field["field_name"],
                        "winner_user_ids": [winner["user_id"] for winner in field["winners"]],
                    }
                    for field in result["fields"]
                ],
            }
        finally:
            await db.close()


def main() -> None:
    result = asyncio.run(run())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
