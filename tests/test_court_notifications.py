from __future__ import annotations

import unittest

from court_bot.embeds import build_court_started_dm_content


class CourtNotificationTests(unittest.TestCase):
    def test_court_started_dm_content_points_parties_to_channel(self) -> None:
        content = build_court_started_dm_content(
            {
                "id": 42,
                "complainant_id": 1001,
                "defendant_id": 1002,
                "requested_visibility": "private",
                "rule_text": "Rule 3：禁止人身攻击",
            },
            court_mention="<#9001>",
            court_url="https://discord.com/channels/1/9001",
            approved_visibility="public",
        )

        self.assertIn("议诉 #42 已开始（公开）", content)
        self.assertIn("议诉频道：<#9001>", content)
        self.assertIn("直达链接：https://discord.com/channels/1/9001", content)
        self.assertIn("投诉人：<@1001>", content)
        self.assertIn("被投诉人：<@1002>", content)
        self.assertIn("Rule 3：禁止人身攻击", content)
        self.assertIn("获取本轮发言权", content)


if __name__ == "__main__":
    unittest.main()
