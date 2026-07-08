from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from court_bot.api import ApprovedListApiServer
from court_bot.election.continuous_constants import CONT_APP_APPROVED
from court_bot.election.continuous_database import ContinuousApplicationRepo
from court_bot.services.db import Database


class ApprovedListApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "api.sqlite")
        self.db = Database(self.db_path)
        await self.db.connect()
        await self.db.init_schema()
        self.repo = ContinuousApplicationRepo(self.db)
        await self.repo.ensure_schema()

        self.config_id = await self.repo.create_config(
            guild_id=100,
            name="常态申请",
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
            fields=["管理组", "技术组"],
        )
        config = await self.repo.get_config(self.config_id)
        assert config is not None
        fields = await self.repo.list_fields(self.config_id)

        first_id = await self.repo.create_application(
            config=config,
            user_id=123456789012345678,
            display_name="申请人",
            username="applicant",
            field_key=str(fields[0]["field_key"]),
            field_name=str(fields[0]["name"]),
            self_intro="宣言",
            voting_end_at="2099-05-01T13:00:00+00:00",
        )
        await self.repo.set_application_status(first_id, CONT_APP_APPROVED, reason="通过")

        second_id = await self.repo.create_application(
            config=config,
            user_id=223456789012345678,
            display_name="技术申请人",
            username="tech_applicant",
            field_key=str(fields[1]["field_key"]),
            field_name=str(fields[1]["name"]),
            self_intro="宣言",
            voting_end_at="2099-05-01T13:00:00+00:00",
        )
        await self.repo.set_application_status(second_id, CONT_APP_APPROVED, reason="通过")

        bot = SimpleNamespace(
            db=self.db,
            config=SimpleNamespace(
                approved_api_enabled=True,
                approved_api_host="127.0.0.1",
                approved_api_port=0,
                approved_api_tokens=("secret-token",),
                approved_api_max_limit=100,
            ),
        )
        self.api = ApprovedListApiServer(bot)
        self.client = TestClient(TestServer(self.api.create_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.db.close()
        self.tmp.cleanup()

    async def test_healthz_does_not_require_auth(self) -> None:
        resp = await self.client.get("/healthz")
        self.assertEqual(resp.status, 200)
        payload = await resp.json()
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["service"], "approved-api")

    async def test_disabled_api_does_not_start_runner(self) -> None:
        bot = SimpleNamespace(
            db=self.db,
            config=SimpleNamespace(
                approved_api_enabled=False,
                approved_api_host="127.0.0.1",
                approved_api_port=0,
                approved_api_tokens=("secret-token",),
                approved_api_max_limit=100,
            ),
        )
        api = ApprovedListApiServer(bot)
        await api.start()
        self.assertIsNone(api.runner)

    async def test_approved_requires_bearer_token(self) -> None:
        resp = await self.client.get("/v1/continuous/approved?guild_id=100")
        self.assertEqual(resp.status, 401)
        payload = await resp.json()
        self.assertEqual(payload["error"], "unauthorized")

    async def test_approved_requires_guild_id(self) -> None:
        resp = await self.client.get(
            "/v1/continuous/approved",
            headers={"Authorization": "Bearer secret-token"},
        )
        self.assertEqual(resp.status, 400)
        payload = await resp.json()
        self.assertEqual(payload["error"], "guild_id_required")

    async def test_approved_returns_filtered_json(self) -> None:
        resp = await self.client.get(
            f"/v1/continuous/approved?guild_id=100&config_id={self.config_id}&field_name=管理组",
            headers={"Authorization": "Bearer secret-token"},
        )
        self.assertEqual(resp.status, 200)
        payload = await resp.json()
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["guild_id"], "100")
        self.assertEqual(payload["config_id"], self.config_id)
        self.assertEqual(payload["field_name"], "管理组")
        self.assertEqual(payload["count"], 1)
        item = payload["items"][0]
        self.assertEqual(item["user_id"], "123456789012345678")
        self.assertEqual(item["field_name"], "管理组")
        self.assertEqual(item["config_name"], "常态申请")
