from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from court_bot.election.cog import parse_fields_config, parse_role_ids_from_text
from court_bot.election.continuous_constants import (
    CONT_APP_APPROVED,
    CONT_APP_REJECTED,
    CONT_APP_VOTING,
    CONT_APP_WITHDRAWN,
    CONT_MODE_SUPPORT,
    CONT_VOTE_NO,
    CONT_VOTE_SUPPORT,
    CONT_VOTE_YES,
)
from court_bot.election.continuous_database import ContinuousApplicationRepo
from court_bot.election.continuous_embeds import build_continuous_application_embed, build_continuous_my_status_embed, build_continuous_public_event_embed
from court_bot.election.continuous_logic import calculate_application_result, calculate_support_collection_result, parse_continuous_fields_config
from court_bot.election.continuous_service import ContinuousApplicationService
from court_bot.election.constants import (
    PUBLIC_PENDING,
    PUBLICITY_REALTIME,
    REG_ACTIVE,
    REG_COUNT_DISPLAY_DETAIL,
    REG_COUNT_DISPLAY_TOTAL,
    REG_REJECTED,
    REG_WITHDRAWN,
)
from court_bot.election.embeds import (
    build_candidate_public_embed,
    build_my_vote_status_embed,
    build_registration_count_text,
    build_vote_candidate_list_embeds,
    format_candidate_vote_line,
    format_role_mentions,
)
from court_bot.election.permissions import can_register, can_vote, missing_candidate_role_message, missing_voter_role_message
from court_bot.election.result_service import ResultService
from court_bot.election.text_utils import contains_forbidden_mention, sanitize_public_text
from court_bot.election.time_utils import build_schedule, parse_duration_minutes, to_utc_iso, utc_now_iso
from court_bot.election.views import FieldSelectView, resolve_registration_selected_field_keys
from court_bot.services.db import Database
from court_bot.election.database import ElectionRepo


class FakeRole:
    def __init__(self, role_id: int):
        self.id = role_id


class FakeMember:
    def __init__(self, role_ids: list[int]):
        self.roles = [FakeRole(role_id) for role_id in role_ids]


class FakeMessage:
    def __init__(self) -> None:
        self.embed = None
        self.view = object()

    async def edit(self, *, embed=None, view=None, allowed_mentions=None) -> None:
        self.embed = embed
        self.view = view


class FakeChannel:
    def __init__(self, message: FakeMessage) -> None:
        self.message = message

    async def fetch_message(self, message_id: int) -> FakeMessage:
        return self.message


def make_fields(count: int) -> list[dict]:
    return [
        {
            "field_key": f"field_{idx}",
            "name": f"岗位{idx}",
            "winner_count": 1,
        }
        for idx in range(1, count + 1)
    ]


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

    def test_parse_continuous_fields_config(self) -> None:
        self.assertEqual(parse_continuous_fields_config("管理组, 技术组\n创作者"), ["管理组", "技术组", "创作者"])
        self.assertEqual(parse_continuous_fields_config("管理组:1,技术组：1"), ["管理组", "技术组"])
        with self.assertRaises(ValueError):
            parse_continuous_fields_config("管理组,管理组")

    def test_continuous_application_result_thresholds(self) -> None:
        passed = calculate_application_result(yes_votes=6, no_votes=4, min_total_votes=10, approval_threshold_percent=60)
        self.assertTrue(passed["passed"])
        self.assertEqual(passed["total_votes"], 10)
        self.assertEqual(passed["approval_ratio_percent"], 60)
        low_total = calculate_application_result(yes_votes=5, no_votes=0, min_total_votes=10, approval_threshold_percent=60)
        self.assertFalse(low_total["passed"])
        low_ratio = calculate_application_result(yes_votes=5, no_votes=5, min_total_votes=10, approval_threshold_percent=60)
        self.assertFalse(low_ratio["passed"])
        support = calculate_support_collection_result(support_votes=3, support_target_votes=3)
        self.assertTrue(support["passed"])
        self.assertEqual(support["support_votes"], 3)
        self.assertFalse(calculate_support_collection_result(support_votes=2, support_target_votes=3)["passed"])

    def test_continuous_application_embed_does_not_expand_user_identity(self) -> None:
        embed = build_continuous_application_embed(
            {"id": 1, "name": "常态申请", "mode": CONT_MODE_SUPPORT},
            {
                "id": 9,
                "user_id": 123456789,
                "display_name": "申请人昵称",
                "username": "unique_user",
                "field_name": "管理组",
                "self_intro": "报名宣言",
                "status": CONT_APP_VOTING,
                "voting_end_at": None,
            },
        )
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(fields["申请人"], "<@123456789>")
        all_text = "\n".join(str(value) for value in fields.values())
        self.assertNotIn("申请人昵称", all_text)
        self.assertNotIn("unique_user", all_text)
        self.assertNotIn("用户 ID", all_text)

    def test_continuous_support_failure_display_keeps_vote_count_private(self) -> None:
        async def run() -> None:
            message = FakeMessage()
            service = ContinuousApplicationService(bot=None, repo=object())

            async def fake_get_text_channel(channel_id: int):
                return FakeChannel(message)

            service._get_text_channel = fake_get_text_channel
            config = {"id": 1, "name": "支持收集", "mode": CONT_MODE_SUPPORT, "voting_channel_id": 11}
            application = {
                "id": 9,
                "user_id": 123456789,
                "field_name": "管理组",
                "self_intro": "报名宣言",
                "status": CONT_APP_REJECTED,
                "status_reason": "未达到通过条件",
                "voting_end_at": None,
                "vote_channel_id": 11,
                "vote_message_id": 99,
            }
            result = {
                "mode": CONT_MODE_SUPPORT,
                "passed": False,
                "support_votes": 1,
                "total_votes": 1,
                "support_target_votes": 3,
            }

            self.assertTrue(await service._edit_vote_message(config, application, result=result))
            fields = {field.name: field.value for field in message.embed.fields}
            self.assertEqual(fields["当前状态"], "未通过")
            self.assertEqual(fields["最终结果"], "未通过")
            self.assertNotIn("支持票", fields)
            self.assertNotIn("目标票数", fields)
            self.assertIsNone(message.view)

        asyncio.run(run())

    def test_continuous_support_failure_is_published_without_vote_count(self) -> None:
        async def run() -> None:
            calls = []
            service = ContinuousApplicationService(bot=None, repo=object())

            async def fake_publish_event(config, application, event, *, result=None):
                calls.append((config, application, event, result))
                return True

            service._publish_event = fake_publish_event
            published = await service._publish_result_event(
                {"id": 1, "mode": CONT_MODE_SUPPORT},
                {"id": 9, "user_id": 123456789, "field_name": "管理组"},
                "未通过：<@123456789> 申请「管理组」。",
                result={"mode": CONT_MODE_SUPPORT, "passed": False, "support_votes": 1, "total_votes": 1},
            )

            self.assertTrue(published)
            self.assertEqual(len(calls), 1)
            self.assertIn("未通过", calls[0][2])

            embed = build_continuous_public_event_embed(
                {"id": 1, "name": "支持收集", "mode": CONT_MODE_SUPPORT},
                {
                    "id": 9,
                    "user_id": 123456789,
                    "field_name": "管理组",
                    "status": CONT_APP_REJECTED,
                    "cooldown_until": "2099-05-02T13:00:00+00:00",
                },
                event="未通过：<@123456789> 申请「管理组」。",
                result={"mode": CONT_MODE_SUPPORT, "passed": False, "support_votes": 1, "total_votes": 1, "support_target_votes": 3},
            )
            fields = {field.name: field.value for field in embed.fields}
            self.assertEqual(fields["状态"], "未通过")
            self.assertNotIn("结果统计", fields)
            self.assertIn("冷却结束", fields)

        asyncio.run(run())

    def test_continuous_support_failure_is_private_from_applicant_status(self) -> None:
        embed = build_continuous_my_status_embed(
            {"id": 1, "name": "支持收集", "mode": CONT_MODE_SUPPORT},
            {
                "id": 9,
                "field_name": "管理组",
                "status": CONT_APP_REJECTED,
                "submitted_at": None,
                "voting_end_at": None,
                "result_json": '{"mode":"support_collection","passed":false,"support_votes":1,"total_votes":1,"support_target_votes":3}',
            },
            cooldown_until="2099-05-02T13:00:00+00:00",
        )
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(embed.description, "最近申请状态：未通过")
        self.assertNotIn("结果统计", fields)
        self.assertIn("冷却结束", fields)

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

    def test_role_restriction_text_uses_suppressed_discord_role_mentions(self) -> None:
        self.assertEqual(
            format_role_mentions([1134611078203052122, 1383835973384802396], action="报名"),
            "拥有以下任意一个身份组即可报名：<@&1134611078203052122>、<@&1383835973384802396>",
        )
        self.assertEqual(
            missing_candidate_role_message([1134611078203052122]),
            "你没有本次募选报名资格；需要拥有以下任意一个身份组：<@&1134611078203052122>。",
        )
        self.assertIn("<@&1383835973384802396>", missing_voter_role_message([1383835973384802396]))

    def test_public_candidate_embed_uses_emoji_identity_and_user_mention(self) -> None:
        embed = build_candidate_public_embed(
            {"id": 1, "name": "测试募选"},
            {
                "id": 9,
                "user_id": 123456789,
                "display_name": "候选人A",
                "selected_field_keys": '["field_1"]',
                "self_intro": "我的宣言",
                "status": REG_ACTIVE,
                "username": "unique_user",
                "registered_at": None,
                "last_modified_at": None,
            },
            {"field_1": "岗位一"},
        )
        fields = {field.name: field.value for field in embed.fields}
        self.assertIn("【候选人公示】｜测试募选", embed.title or "")
        self.assertEqual(fields["👤 候选人"], "候选人A")
        self.assertEqual(fields["🏷️ 用户名"], "`unique_user`")
        self.assertEqual(fields["🔗 提及"], "<@123456789>")
        self.assertNotIn("🆔 用户 ID", fields)
        self.assertEqual(fields["📌 当前状态"], "✅ 有效报名")
        self.assertEqual(fields["🗳️ 参选岗位"], "岗位一")

    def test_public_vote_list_adds_icons_and_mention_without_changing_private_line(self) -> None:
        candidate = {"display_name": "候选人A", "username": "unique_user", "user_id": 123456789, "field_names": ["岗位一", "岗位二"]}
        self.assertEqual(format_candidate_vote_line(candidate, prefix="1. "), "1. 候选人A（用户ID：123456789）｜参选：岗位一、岗位二")
        embed = build_vote_candidate_list_embeds({"id": 1, "name": "测试募选"}, [candidate])[0]
        self.assertIn("1. 👤 候选人A（🏷️ `unique_user`｜🔗 <@123456789>）｜🗳️ 参选：岗位一、岗位二", embed.description or "")
        self.assertNotIn("🆔", embed.description or "")

    def test_can_register_or_rule(self) -> None:
        self.assertTrue(can_register(FakeMember([]), []))
        self.assertTrue(can_register(FakeMember([10, 20]), [20, 30]))
        self.assertFalse(can_register(FakeMember([10, 11]), [20, 30]))

    def test_my_vote_status_embed_shows_private_selected_candidates(self) -> None:
        embed = build_my_vote_status_embed(
            {"id": 1, "name": "测试募选", "status": "voting", "voting_end_at": None},
            {"created_at": None},
            [
                {
                    "user_id": 123456789,
                    "display_name": "候选人A",
                    "field_names": ["岗位一", "岗位二"],
                    "status": REG_ACTIVE,
                }
            ],
        )
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(fields["投票状态"], "已提交")
        self.assertIn("候选人A（用户ID：123456789）", fields["已选择候选人"])
        self.assertIn("参选：岗位一、岗位二", fields["已选择候选人"])
        self.assertNotIn("<@123456789>", fields["已选择候选人"])

    def test_my_vote_status_embed_respects_discord_field_limit(self) -> None:
        candidates = [
            {
                "user_id": 100000 + idx,
                "display_name": f"候选人{idx:03d}" + "很长的名字" * 6,
                "field_names": ["岗位一", "岗位二", "岗位三", "岗位四", "岗位五"],
                "status": REG_ACTIVE,
            }
            for idx in range(300)
        ]
        embed = build_my_vote_status_embed(
            {"id": 1, "name": "测试募选", "status": "voting", "voting_end_at": None},
            {"created_at": None},
            candidates,
            is_eligible=False,
            eligibility_note="你当前没有本次募选投票资格。",
        )
        self.assertLessEqual(len(embed.fields), 25)
        self.assertTrue(all(len(str(field.value)) <= 1024 for field in embed.fields))
        self.assertIn("…后续内容已省略。", embed.fields[-1].value)

    def test_my_vote_status_embed_truncates_overlong_candidate_line(self) -> None:
        embed = build_my_vote_status_embed(
            {"id": 1, "name": "测试募选", "status": "voting", "voting_end_at": None},
            {"created_at": None},
            [
                {
                    "user_id": 123456789,
                    "display_name": "候选人A" + "超长昵称" * 20,
                    "field_names": [f"岗位{idx}" + "很长的岗位名" * 8 for idx in range(25)],
                    "status": REG_ACTIVE,
                }
            ],
        )
        fields = {field.name: field.value for field in embed.fields}
        self.assertLessEqual(len(fields["已选择候选人"]), 1024)
        self.assertIn("…", fields["已选择候选人"])

    def test_registration_select_all_resolves_all_fields(self) -> None:
        fields = make_fields(25)
        self.assertEqual(
            resolve_registration_selected_field_keys(fields, ["__all_registration_fields__"]),
            [f"field_{idx}" for idx in range(1, 26)],
        )
        self.assertEqual(
            resolve_registration_selected_field_keys(fields, ["field_1", "field_3", "field_1", "unknown"]),
            ["field_1", "field_3"],
        )

    def test_registration_field_select_keeps_all_25_fields(self) -> None:
        async def build_view() -> FieldSelectView:
            return FieldSelectView(cog=object(), election={"id": 1}, fields=make_fields(25))

        view = asyncio.run(build_view())
        self.assertEqual(len(view.children), 2)
        shortcut_select = view.children[0]
        field_select = view.children[1]
        self.assertEqual([option.label for option in shortcut_select.options], ["全选"])
        self.assertEqual(len(field_select.options), 25)
        self.assertEqual(field_select.options[-1].value, "field_25")

    def test_registration_count_text_total_and_detail(self) -> None:
        fields = make_fields(2)
        registrations = [
            {"selected_field_keys": '["field_1"]'},
            {"selected_field_keys": '["field_2"]'},
            {"selected_field_keys": '["field_1","field_2"]'},
        ]
        self.assertEqual(build_registration_count_text(fields, registrations, mode=REG_COUNT_DISPLAY_TOTAL), "总人数：3 人")
        detail = build_registration_count_text(fields, registrations, mode=REG_COUNT_DISPLAY_DETAIL)
        self.assertEqual(
            detail,
            "总人数：3 人\n"
            "岗位1：1 人\n"
            "岗位2：1 人\n"
            "全选（全部岗位）：1 人",
        )


class ElectionRepoAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "election-test.db")
        self.db = Database(self.db_path)
        await self.db.connect()
        await self.db.init_schema()
        self.repo = ElectionRepo(self.db)
        await self.repo.ensure_schema()
        self.cont_repo = ContinuousApplicationRepo(self.db)
        await self.cont_repo.ensure_schema()

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
            registration_count_display=REG_COUNT_DISPLAY_DETAIL,
        )
        election = await self.repo.get_election(election_id)
        assert election is not None
        return election

    async def test_schema_create_registration_reregister_and_vote_immutability(self) -> None:
        election = await self._create_election()
        self.assertEqual(election.get("registration_count_display"), REG_COUNT_DISPLAY_DETAIL)
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
        vote_record = await self.repo.get_vote_record_for_voter(int(election["id"]), 500)
        self.assertIsNotNone(vote_record)
        assert vote_record is not None
        self.assertEqual(ElectionRepo.decode_field_keys(vote_record["selected_user_ids"]), ["1"])
        with self.assertRaises(Exception):
            await self.repo.add_vote_record(vote_id=vote_id, election_id=int(election["id"]), voter_id=500, selected_user_ids=[1])
        await self.repo.invalidate_vote(election_id=int(election["id"]), voter_id=500, operator_id=999, reason="异常票")
        invalidation = await self.repo.get_vote_invalidation(int(election["id"]), 500)
        self.assertEqual(invalidation.get("reason") if invalidation else None, "异常票")

    async def test_election_admin_role_settings_roundtrip(self) -> None:
        self.assertEqual(await self.repo.get_admin_role_ids(100), [])
        await self.repo.set_admin_role_ids(100, [333, 444, 333])
        self.assertEqual(await self.repo.get_admin_role_ids(100), [333, 444])
        await self.repo.set_admin_role_ids(100, [])
        self.assertEqual(await self.repo.get_admin_role_ids(100), [])

    async def test_rejected_registration_can_resubmit_intro_with_same_fields(self) -> None:
        election = await self._create_election()
        reg = await self.repo.upsert_registration(
            election=election,
            user_id=10,
            display_name="候选人10",
            selected_field_keys=["field_1"],
            self_intro="原宣言",
        )
        await self.repo.update_registration_public_message(int(reg["id"]), channel_id=12, message_id=12345, status=PUBLIC_PENDING)
        await self.repo.set_registration_status(election_id=int(election["id"]), user_id=10, status=REG_REJECTED, reason="请修改宣言", operator_id=999)
        updated = await self.repo.upsert_registration(election=election, user_id=10, display_name="候选人10", selected_field_keys=["field_1"], self_intro="新宣言", public_status_override=PUBLIC_PENDING)
        self.assertEqual(updated["status"], REG_ACTIVE)
        self.assertEqual(updated["self_intro"], "新宣言")
        self.assertEqual(ElectionRepo.decode_field_keys(updated["selected_field_keys"]), ["field_1"])
        self.assertEqual(updated["public_sync_status"], PUBLIC_PENDING)

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

    async def test_continuous_repo_config_application_vote_and_cooldown(self) -> None:
        config_id = await self.cont_repo.create_config(
            guild_id=100,
            name="常态申请",
            entry_channel_id=10,
            voting_channel_id=11,
            public_channel_id=12,
            allowed_application_role_ids=[333],
            allowed_voter_role_ids=[111, 222],
            min_total_votes=3,
            approval_threshold_percent=66.7,
            voting_duration_minutes=60,
            cooldown_minutes=1440,
            created_by=999,
            fields=["管理组", "技术组"],
        )
        config = await self.cont_repo.get_config(config_id)
        assert config is not None
        self.assertEqual(ContinuousApplicationRepo.decode_role_ids(config["allowed_application_role_ids"]), [333])
        fields = await self.cont_repo.list_fields(config_id)
        self.assertEqual([field["name"] for field in fields], ["管理组", "技术组"])

        app_id = await self.cont_repo.create_application(
            config=config,
            user_id=1,
            display_name="申请人",
            username="applicant",
            field_key=str(fields[0]["field_key"]),
            field_name=str(fields[0]["name"]),
            self_intro="宣言",
            voting_end_at="2099-05-01T13:00:00+00:00",
        )
        app = await self.cont_repo.get_application(app_id)
        assert app is not None
        self.assertEqual(app["status"], CONT_APP_VOTING)
        self.assertIsNotNone(await self.cont_repo.get_active_application(config_id, 1))
        with self.assertRaises(ValueError):
            await self.cont_repo.create_application(
                config=config,
                user_id=1,
                display_name="申请人",
                username="applicant",
                field_key=str(fields[1]["field_key"]),
                field_name=str(fields[1]["name"]),
                self_intro="重复申请",
                voting_end_at="2099-05-01T13:00:00+00:00",
            )

        changed = await self.cont_repo.set_application_status(app_id, CONT_APP_WITHDRAWN, reason="wrong state", expected_status=CONT_APP_APPROVED)
        self.assertFalse(changed)
        app = await self.cont_repo.get_application(app_id)
        assert app is not None
        self.assertEqual(app["status"], CONT_APP_VOTING)

        await self.cont_repo.upsert_vote_record(application=app, voter_id=500, choice=CONT_VOTE_YES)
        await self.cont_repo.upsert_vote_record(application=app, voter_id=501, choice=CONT_VOTE_NO)
        await self.cont_repo.upsert_vote_record(application=app, voter_id=500, choice=CONT_VOTE_NO)
        counts = await self.cont_repo.count_votes(app_id)
        self.assertEqual(counts[CONT_VOTE_YES], 0)
        self.assertEqual(counts[CONT_VOTE_NO], 2)
        self.assertEqual(counts["total"], 2)

        cooldown_until = "2099-05-02T13:00:00+00:00"
        await self.cont_repo.set_application_status(app_id, CONT_APP_WITHDRAWN, reason="用户退出", cooldown_until=cooldown_until, result={"event": "withdrawn"})
        self.assertEqual(await self.cont_repo.get_active_cooldown(config_id, 1, utc_now_iso()), cooldown_until)
        with self.assertRaises(ValueError):
            await self.cont_repo.upsert_vote_record(application=app, voter_id=502, choice=CONT_VOTE_YES)

    async def test_continuous_repo_rejects_expired_votes_and_finalizes_atomically(self) -> None:
        config_id = await self.cont_repo.create_config(
            guild_id=100,
            name="常态申请",
            entry_channel_id=10,
            voting_channel_id=11,
            public_channel_id=12,
            allowed_application_role_ids=[],
            allowed_voter_role_ids=[],
            min_total_votes=2,
            approval_threshold_percent=50,
            voting_duration_minutes=60,
            cooldown_minutes=1440,
            created_by=999,
            fields=["管理组"],
        )
        config = await self.cont_repo.get_config(config_id)
        assert config is not None
        fields = await self.cont_repo.list_fields(config_id)

        expired_id = await self.cont_repo.create_application(
            config=config,
            user_id=2,
            display_name="申请人2",
            username="applicant2",
            field_key=str(fields[0]["field_key"]),
            field_name=str(fields[0]["name"]),
            self_intro="宣言",
            voting_end_at="2000-05-01T13:00:00+00:00",
        )
        expired = await self.cont_repo.get_application(expired_id)
        assert expired is not None
        changed = await self.cont_repo.set_application_status(
            expired_id,
            CONT_APP_WITHDRAWN,
            reason="late withdraw",
            expected_status=CONT_APP_VOTING,
            require_not_expired=True,
        )
        self.assertFalse(changed)
        expired = await self.cont_repo.get_application(expired_id)
        assert expired is not None
        self.assertEqual(expired["status"], CONT_APP_VOTING)
        with self.assertRaises(ValueError):
            await self.cont_repo.upsert_vote_record(application=expired, voter_id=500, choice=CONT_VOTE_YES)

        app_id = await self.cont_repo.create_application(
            config=config,
            user_id=3,
            display_name="申请人3",
            username="applicant3",
            field_key=str(fields[0]["field_key"]),
            field_name=str(fields[0]["name"]),
            self_intro="宣言",
            voting_end_at="2099-05-01T13:00:00+00:00",
        )
        app = await self.cont_repo.get_application(app_id)
        assert app is not None
        await self.cont_repo.upsert_vote_record(application=app, voter_id=500, choice=CONT_VOTE_YES)
        await self.cont_repo.upsert_vote_record(application=app, voter_id=501, choice=CONT_VOTE_NO)

        finalized = await self.cont_repo.finalize_voting_application(
            app_id,
            min_total_votes=2,
            approval_threshold_percent=50,
            cooldown_until_if_rejected="2099-05-02T13:00:00+00:00",
        )
        assert finalized is not None
        updated, result = finalized
        self.assertTrue(result["passed"])
        self.assertEqual(updated["status"], CONT_APP_APPROVED)
        self.assertIsNone(await self.cont_repo.finalize_voting_application(
            app_id,
            min_total_votes=2,
            approval_threshold_percent=50,
            cooldown_until_if_rejected="2099-05-02T13:00:00+00:00",
        ))
        with self.assertRaises(ValueError):
            await self.cont_repo.upsert_vote_record(application=app, voter_id=502, choice=CONT_VOTE_YES)

    async def test_continuous_support_collection_support_withdraw_and_finalize(self) -> None:
        with self.assertRaises(ValueError):
            await self.cont_repo.create_config(
                guild_id=100,
                name="无效支持收集",
                entry_channel_id=10,
                voting_channel_id=11,
                public_channel_id=12,
                allowed_application_role_ids=[],
                allowed_voter_role_ids=[],
                min_total_votes=1,
                approval_threshold_percent=51,
                voting_duration_minutes=60,
                cooldown_minutes=1440,
                created_by=999,
                fields=["管理组"],
                mode=CONT_MODE_SUPPORT,
                support_target_votes=0,
            )
        config_id = await self.cont_repo.create_config(
            guild_id=100,
            name="支持收集",
            entry_channel_id=10,
            voting_channel_id=11,
            public_channel_id=12,
            allowed_application_role_ids=[],
            allowed_voter_role_ids=[],
            min_total_votes=1,
            approval_threshold_percent=51,
            voting_duration_minutes=60,
            cooldown_minutes=1440,
            created_by=999,
            fields=["管理组"],
            mode=CONT_MODE_SUPPORT,
            support_target_votes=2,
        )
        config = await self.cont_repo.get_config(config_id)
        assert config is not None
        self.assertEqual(config["mode"], CONT_MODE_SUPPORT)
        self.assertEqual(config["support_target_votes"], 2)
        fields = await self.cont_repo.list_fields(config_id)
        app_id = await self.cont_repo.create_application(
            config=config,
            user_id=20,
            display_name="申请人20",
            username="applicant20",
            field_key=str(fields[0]["field_key"]),
            field_name=str(fields[0]["name"]),
            self_intro="宣言",
            voting_end_at="2099-05-01T13:00:00+00:00",
        )
        app = await self.cont_repo.get_application(app_id)
        assert app is not None

        await self.cont_repo.upsert_vote_record(application=app, voter_id=500, choice=CONT_VOTE_SUPPORT)
        counts = await self.cont_repo.count_votes(app_id)
        self.assertEqual(counts[CONT_VOTE_SUPPORT], 1)
        self.assertEqual(counts["total"], 1)
        self.assertIsNone(await self.cont_repo.finalize_voting_application(
            app_id,
            min_total_votes=1,
            approval_threshold_percent=51,
            cooldown_until_if_rejected="2099-05-02T13:00:00+00:00",
            mode=CONT_MODE_SUPPORT,
            support_target_votes=2,
            reject_when_unmet=False,
        ))

        self.assertTrue(await self.cont_repo.delete_vote_record(application=app, voter_id=500, choice=CONT_VOTE_SUPPORT))
        counts = await self.cont_repo.count_votes(app_id)
        self.assertEqual(counts[CONT_VOTE_SUPPORT], 0)
        self.assertFalse(await self.cont_repo.delete_vote_record(application=app, voter_id=500, choice=CONT_VOTE_SUPPORT))

        await self.cont_repo.upsert_vote_record(application=app, voter_id=500, choice=CONT_VOTE_SUPPORT)
        await self.cont_repo.upsert_vote_record(application=app, voter_id=501, choice=CONT_VOTE_SUPPORT)
        finalized = await self.cont_repo.finalize_voting_application(
            app_id,
            min_total_votes=1,
            approval_threshold_percent=51,
            cooldown_until_if_rejected="2099-05-02T13:00:00+00:00",
            mode=CONT_MODE_SUPPORT,
            support_target_votes=2,
            reject_when_unmet=False,
        )
        assert finalized is not None
        updated, result = finalized
        self.assertEqual(updated["status"], CONT_APP_APPROVED)
        self.assertTrue(result["passed"])
        self.assertEqual(result["support_votes"], 2)
        supporters = await self.cont_repo.list_vote_records(app_id, choice=CONT_VOTE_SUPPORT)
        self.assertEqual([row["voter_id"] for row in supporters], [500, 501])

    async def test_continuous_support_collection_due_rejects_without_publishing_supporters(self) -> None:
        config_id = await self.cont_repo.create_config(
            guild_id=100,
            name="支持收集",
            entry_channel_id=10,
            voting_channel_id=11,
            public_channel_id=12,
            allowed_application_role_ids=[],
            allowed_voter_role_ids=[],
            min_total_votes=1,
            approval_threshold_percent=51,
            voting_duration_minutes=60,
            cooldown_minutes=1440,
            created_by=999,
            fields=["管理组"],
            mode=CONT_MODE_SUPPORT,
            support_target_votes=3,
        )
        config = await self.cont_repo.get_config(config_id)
        assert config is not None
        fields = await self.cont_repo.list_fields(config_id)
        app_id = await self.cont_repo.create_application(
            config=config,
            user_id=21,
            display_name="申请人21",
            username="applicant21",
            field_key=str(fields[0]["field_key"]),
            field_name=str(fields[0]["name"]),
            self_intro="宣言",
            voting_end_at="2099-05-01T13:00:00+00:00",
        )
        app = await self.cont_repo.get_application(app_id)
        assert app is not None
        await self.cont_repo.upsert_vote_record(application=app, voter_id=500, choice=CONT_VOTE_SUPPORT)

        finalized = await self.cont_repo.finalize_voting_application(
            app_id,
            min_total_votes=1,
            approval_threshold_percent=51,
            cooldown_until_if_rejected="2099-05-02T13:00:00+00:00",
            mode=CONT_MODE_SUPPORT,
            support_target_votes=3,
        )
        assert finalized is not None
        updated, result = finalized
        self.assertEqual(updated["status"], CONT_APP_REJECTED)
        self.assertFalse(result["passed"])
        self.assertEqual(result["support_votes"], 1)
        self.assertEqual(result["support_target_votes"], 3)
        self.assertEqual(updated["cooldown_until"], "2099-05-02T13:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
