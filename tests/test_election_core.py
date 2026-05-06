from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from court_bot.election.cog import parse_fields_config, parse_role_ids_from_text
from court_bot.election.constants import PUBLICITY_REALTIME, REG_WITHDRAWN
from court_bot.election.permissions import can_register, can_vote
from court_bot.election.result_service import ResultService
from court_bot.election.text_utils import contains_forbidden_mention, sanitize_public_text
from court_bot.election.time_utils import build_schedule, parse_duration_minutes, to_utc_iso
from court_bot.services.db import Database
from court_bot.election.database import ElectionRepo


class FakeRole:
    def __init__(self, role_id: int):
        self.id = role_id


class FakeMember:
    def __init__(self, role_ids: list[int]):
        self.roles = [FakeRole(role_id) for role_id in role_ids]


class ElectionPureFunctionTests(unittest.TestCase):
    def test_parse_duration_minutes(self) -> None:
        self.assertEqual(parse_duration_minutes("3天"), 4320)
        self.assertEqual(parse_duration_minutes("2天6小时30分钟"), 3270)
        self.assertEqual(parse_duration_minutes("12h"), 720)
        self.assertEqual(parse_duration_minutes("0小时", allow_zero=True), 0)
        with self.assertRaises(ValueError):
            parse_duration_minutes("0小时")
        with self.assertRaises(ValueError):
            parse_duration_minutes("1.5天")

    def test_build_schedule_beijing_to_utc(self) -> None:
        schedule = build_schedule(
            start_at_text="2026-05-01 20:00",
            registration_duration_minutes=60,
            publicity_duration_minutes=0,
            voting_duration_minutes=120,
        )
        self.assertEqual(to_utc_iso(schedule.registration_start_at), "2026-05-01T12:00:00+00:00")
        self.assertEqual(to_utc_iso(schedule.registration_end_at), "2026-05-01T13:00:00+00:00")
        self.assertEqual(to_utc_iso(schedule.voting_start_at), "2026-05-01T13:00:00+00:00")
        self.assertEqual(to_utc_iso(schedule.voting_end_at), "2026-05-01T15:00:00+00:00")

    def test_parse_fields_config(self) -> None:
        self.assertEqual(parse_fields_config("大当家:1,二当家:3\n执行成员：9"), [("大当家", 1), ("二当家", 3), ("执行成员", 9)])
        with self.assertRaises(ValueError):
            parse_fields_config("大当家:1,大当家:2")
        with self.assertRaises(ValueError):
            parse_fields_config("大当家:0")

    def test_parse_role_ids(self) -> None:
        self.assertEqual(parse_role_ids_from_text("<@&123>, 456 123"), [123, 456])
        self.assertEqual(parse_role_ids_from_text(None), [])
        with self.assertRaises(ValueError):
            parse_role_ids_from_text("abc")

    def test_mentions(self) -> None:
        self.assertTrue(contains_forbidden_mention("hello <@123>"))
        self.assertTrue(contains_forbidden_mention("hello <@&456>"))
        self.assertTrue(contains_forbidden_mention("@everyone"))
        self.assertFalse(contains_forbidden_mention("普通宣言"))
        self.assertNotIn("@everyone", sanitize_public_text("hi @everyone"))
        self.assertIn("@\u200beveryone", sanitize_public_text("hi @everyone"))

    def test_can_vote_or_rule(self) -> None:
        self.assertTrue(can_vote(FakeMember([]), []))
        self.assertTrue(can_vote(FakeMember([1, 9]), [2, 9]))
        self.assertFalse(can_vote(FakeMember([1, 3]), [2, 9]))

    def test_can_register_or_rule(self) -> None:
        self.assertTrue(can_register(FakeMember([]), []))
        self.assertTrue(can_register(FakeMember([10, 20]), [20, 30]))
        self.assertFalse(can_register(FakeMember([10, 11]), [20, 30]))


class ElectionRepoAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "election-test.db")
        self.db = Database(self.db_path)
        await self.db.connect()
        await self.db.init_schema()
        self.repo = ElectionRepo(self.db)
        await self.repo.ensure_schema()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.temp_dir.cleanup()

    async def _create_election(self) -> dict:
        schedule = build_schedule(
            start_at_text="2026-05-01 20:00",
            registration_duration_minutes=60,
            publicity_duration_minutes=0,
            voting_duration_minutes=60,
        )
        election_id = await self.repo.create_election(
            guild_id=100,
            name="自动化测试募选",
            publicity_mode=PUBLICITY_REALTIME,
            registration_channel_id=10,
            voting_channel_id=11,
            public_channel_id=12,
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
        election = await self.repo.get_election(election_id)
        assert election is not None
        return election

    async def test_schema_create_registration_reregister_and_vote_immutability(self) -> None:
        election = await self._create_election()
        self.assertEqual(ElectionRepo.decode_role_ids(election["allowed_candidate_role_ids"]), [333, 444])
        self.assertEqual(ElectionRepo.decode_role_ids(election["allowed_voter_role_ids"]), [111, 222])
        await self.repo.set_allowed_candidate_role_ids(int(election["id"]), [555, 666])
        updated = await self.repo.get_election(int(election["id"]))
        assert updated is not None
        self.assertEqual(ElectionRepo.decode_role_ids(updated["allowed_candidate_role_ids"]), [555, 666])
        reg = await self.repo.upsert_registration(
            election=election,
            user_id=1,
            display_name="候选人1",
            selected_field_keys=["field_1"],
            self_intro="宣言1",
        )
        first_registered_at = reg["registered_at"]
        reg = await self.repo.upsert_registration(
            election=election,
            user_id=1,
            display_name="候选人1编辑",
            selected_field_keys=["field_1"],
            self_intro="宣言2",
        )
        self.assertEqual(reg["registered_at"], first_registered_at)
        await self.repo.set_registration_status(election_id=int(election["id"]), user_id=1, status=REG_WITHDRAWN, reason="test", operator_id=1)
        reg = await self.repo.upsert_registration(
            election=election,
            user_id=1,
            display_name="候选人1重报",
            selected_field_keys=["field_1"],
            self_intro="宣言3",
            is_re_register_after_withdraw=True,
        )
        self.assertNotEqual(reg["registered_at"], first_registered_at)

        vote_id = await self.repo.create_vote(election)
        await self.repo.add_vote_record(vote_id=vote_id, election_id=int(election["id"]), voter_id=500, selected_user_ids=[1])
        with self.assertRaises(Exception):
            await self.repo.add_vote_record(vote_id=vote_id, election_id=int(election["id"]), voter_id=500, selected_user_ids=[1])

    async def test_result_order_field_lock_tiebreak_no_fill(self) -> None:
        election = await self._create_election()
        await self.repo.upsert_registration(election=election, user_id=1, display_name="甲", selected_field_keys=["field_1", "field_2"], self_intro="A")
        await self.repo.upsert_registration(election=election, user_id=2, display_name="乙", selected_field_keys=["field_1", "field_2"], self_intro="B")
        await self.repo.upsert_registration(election=election, user_id=3, display_name="丙", selected_field_keys=["field_2"], self_intro="C")
        vote_id = await self.repo.create_vote(election)
        await self.repo.add_vote_record(vote_id=vote_id, election_id=int(election["id"]), voter_id=1000, selected_user_ids=[1, 2])
        await self.repo.add_vote_record(vote_id=vote_id, election_id=int(election["id"]), voter_id=1001, selected_user_ids=[1, 3])
        await self.repo.add_vote_record(vote_id=vote_id, election_id=int(election["id"]), voter_id=1002, selected_user_ids=[2, 3])
        await self.repo.add_vote_record(vote_id=vote_id, election_id=int(election["id"]), voter_id=1003, selected_user_ids=[3])
        result = await ResultService(self.repo).calculate(election)
        self.assertFalse(result["is_void"])
        first_winner = result["fields"][0]["winners"][0]
        second_winner = result["fields"][1]["winners"][0]
        # field_1: user 1 and user 2 both have 2 votes; earlier registration wins.
        self.assertEqual(first_winner["user_id"], "1")
        # user 1 is locked by field_1, so field_2 winner is user 3 with 2 votes.
        self.assertEqual(second_winner["user_id"], "3")

    async def test_void_without_candidates_or_votes(self) -> None:
        election = await self._create_election()
        result = await ResultService(self.repo).calculate(election)
        self.assertTrue(result["is_void"])
        self.assertEqual(result["void_reason"], "无人有效报名，本次募选作废")
        await self.repo.upsert_registration(election=election, user_id=1, display_name="甲", selected_field_keys=["field_1"], self_intro="A")
        result = await ResultService(self.repo).calculate(election)
        self.assertTrue(result["is_void"])
        self.assertEqual(result["void_reason"], "无人投票，本次募选作废")


if __name__ == "__main__":
    unittest.main()
