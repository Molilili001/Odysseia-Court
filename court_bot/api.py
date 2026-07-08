from __future__ import annotations

import hmac
import logging
from typing import Any

from aiohttp import web

from .election.continuous_database import ContinuousApplicationRepo


log = logging.getLogger(__name__)


def _json_error(error: str, *, status: int) -> web.Response:
    return web.json_response({"ok": False, "error": error}, status=status)


def _parse_int_param(request: web.Request, name: str, *, required: bool = False) -> int | None:
    raw = request.query.get(name)
    if raw is None or not raw.strip():
        if required:
            raise ValueError(f"{name}_required")
        return None
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name}_invalid") from exc
    if value <= 0:
        raise ValueError(f"{name}_invalid")
    return value


def _approved_application_payload(row: dict[str, Any]) -> dict[str, Any]:
    approved_at = row.get("closed_at") or row.get("updated_at")
    return {
        "application_id": int(row["id"]),
        "config_id": int(row["config_id"]),
        "config_name": str(row.get("config_name") or ""),
        "guild_id": str(int(row["guild_id"])),
        "user_id": str(int(row["user_id"])),
        "display_name": str(row.get("display_name") or ""),
        "username": str(row.get("username") or ""),
        "field_key": str(row.get("field_key") or ""),
        "field_name": str(row.get("field_name") or ""),
        "approved_at": approved_at,
        "submitted_at": row.get("submitted_at"),
    }


class ApprovedListApiServer:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self.repo = ContinuousApplicationRepo(bot.db)

    @property
    def enabled(self) -> bool:
        return bool(self.bot.config.approved_api_enabled)

    def _authorized(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False

        token = header[len(prefix) :].strip()
        if not token:
            return False

        return any(hmac.compare_digest(token, expected) for expected in self.bot.config.approved_api_tokens)

    @web.middleware
    async def auth_middleware(self, request: web.Request, handler):
        if request.path == "/healthz":
            return await handler(request)
        if not self._authorized(request):
            return _json_error("unauthorized", status=401)
        return await handler(request)

    async def healthz(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "approved-api"})

    async def approved(self, request: web.Request) -> web.Response:
        try:
            guild_id = _parse_int_param(request, "guild_id", required=True)
            config_id = _parse_int_param(request, "config_id")
            requested_limit = _parse_int_param(request, "limit")
        except ValueError as exc:
            return _json_error(str(exc), status=400)

        assert guild_id is not None
        max_limit = int(self.bot.config.approved_api_max_limit)
        limit = min(requested_limit or 100, max_limit)
        field_name = (request.query.get("field_name") or "").strip() or None

        try:
            rows = await self.repo.list_approved_applications(
                guild_id=guild_id,
                config_id=config_id,
                field_name=field_name,
                limit=limit,
            )
        except Exception:
            log.exception("Approved API query failed")
            return _json_error("internal_error", status=500)

        items = [_approved_application_payload(dict(row)) for row in rows]
        return web.json_response(
            {
                "ok": True,
                "guild_id": str(guild_id),
                "config_id": config_id,
                "field_name": field_name,
                "limit": limit,
                "count": len(items),
                "items": items,
            }
        )

    def create_app(self) -> web.Application:
        app = web.Application(middlewares=[self.auth_middleware])
        app.add_routes(
            [
                web.get("/healthz", self.healthz),
                web.get("/v1/continuous/approved", self.approved),
            ]
        )
        return app

    async def start(self) -> None:
        if not self.enabled:
            return
        if self.runner is not None:
            return

        app = self.create_app()
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        host = self.bot.config.approved_api_host
        port = int(self.bot.config.approved_api_port)
        self.site = web.TCPSite(self.runner, host=host, port=port)
        await self.site.start()
        log.info("Approved API listening on http://%s:%s", host, port)

    async def close(self) -> None:
        if self.site is not None:
            await self.site.stop()
            self.site = None
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
