from __future__ import annotations

from collections import Counter
from typing import Any

from .constants import VOTE_MODE_UNIFIED
from .database import ElectionRepo
from .time_utils import utc_now_iso


class ResultService:
    def __init__(self, repo: ElectionRepo):
        self.repo = repo

    async def calculate(self, election: dict[str, Any], *, void_reason: str | None = None) -> dict[str, Any]:
        fields = await self.repo.list_fields(int(election["id"]))
        registrations = await self.repo.list_active_registrations(int(election["id"]))
        records = await self.repo.list_vote_records(int(election["id"]))

        if void_reason:
            return {
                "vote_mode": VOTE_MODE_UNIFIED,
                "publicity_mode": election.get("publicity_mode"),
                "is_void": True,
                "void_reason": void_reason,
                "total_voters": len(records),
                "total_votes": 0,
                "fields": [],
                "calculated_at": utc_now_iso(),
            }

        if not registrations:
            return {
                "vote_mode": VOTE_MODE_UNIFIED,
                "publicity_mode": election.get("publicity_mode"),
                "is_void": True,
                "void_reason": "无人有效报名，本次募选作废",
                "total_voters": 0,
                "total_votes": 0,
                "fields": [],
                "calculated_at": utc_now_iso(),
            }

        if not records:
            return {
                "vote_mode": VOTE_MODE_UNIFIED,
                "publicity_mode": election.get("publicity_mode"),
                "is_void": True,
                "void_reason": "无人投票，本次募选作废",
                "total_voters": 0,
                "total_votes": 0,
                "fields": [],
                "calculated_at": utc_now_iso(),
            }

        vote_counts: Counter[str] = Counter()
        total_votes = 0
        for record in records:
            user_ids = self.repo.decode_field_keys(record.get("selected_user_ids"))
            for uid in user_ids:
                vote_counts[str(uid)] += 1
                total_votes += 1

        selected_user_ids: set[str] = set()
        winner_field_by_user_id: dict[str, dict[str, str]] = {}
        result_fields: list[dict[str, Any]] = []

        def sort_key(item: dict[str, Any]) -> tuple[int, str, str]:
            return (-int(item.get("votes") or 0), str(item.get("registered_at") or ""), str(item.get("user_id") or ""))

        for field in fields:
            field_key = str(field["field_key"])
            field_name = str(field.get("name") or field_key)
            all_candidates: list[dict[str, Any]] = []
            eligible_pool: list[dict[str, Any]] = []
            for reg in registrations:
                uid = str(int(reg["user_id"]))
                selected_keys = self.repo.decode_field_keys(reg.get("selected_field_keys"))
                if field_key not in selected_keys:
                    continue
                item = {
                    "user_id": uid,
                    "display_name": reg.get("display_name"),
                    "votes": int(vote_counts.get(uid, 0)),
                    "registered_at": reg.get("registered_at"),
                    "registration_id": int(reg.get("id")),
                    "field_key": field_key,
                    "field_name": field_name,
                    "selected_field_keys": selected_keys,
                }
                all_candidates.append(item)
                if uid not in selected_user_ids:
                    eligible_pool.append(item)

            all_candidates.sort(key=sort_key)
            eligible_pool.sort(key=sort_key)
            for rank, candidate in enumerate(all_candidates, start=1):
                candidate["rank"] = rank

            winner_count = int(field.get("winner_count") or 0)
            winners = eligible_pool[:winner_count]
            for winner in winners:
                uid = str(winner["user_id"])
                selected_user_ids.add(uid)
                winner["won_field_key"] = field_key
                winner["won_field_name"] = field_name
                winner_field_by_user_id[uid] = {"field_key": field_key, "field_name": field_name}
            result_fields.append(
                {
                    "field_key": field_key,
                    "field_name": field_name,
                    "winner_count": winner_count,
                    "vacancies": max(0, winner_count - len(winners)),
                    "winners": winners,
                    "candidates": all_candidates,
                }
            )

        # Mark each candidate row with the final confirmed winning field, even when
        # the candidate is displayed under another selected field. This keeps the
        # result embed able to show “确定当选岗位”.
        for field_result in result_fields:
            for candidate in field_result.get("candidates", []):
                won = winner_field_by_user_id.get(str(candidate.get("user_id")))
                if won:
                    candidate["won_field_key"] = won["field_key"]
                    candidate["won_field_name"] = won["field_name"]

        return {
            "vote_mode": VOTE_MODE_UNIFIED,
            "publicity_mode": election.get("publicity_mode"),
            "is_void": False,
            "total_voters": len(records),
            "total_votes": total_votes,
            "fields": result_fields,
            "calculated_at": utc_now_iso(),
        }
