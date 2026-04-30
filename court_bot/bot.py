from __future__ import annotations

import asyncio
import gc
import io
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from .config import Config
from .i18n import StaticExtrasTranslator
from .constants import (
    SIDE_COMPLAINANT,
    SIDE_DEFENDANT,
    STATUS_AWAITING_CONTINUE,
    STATUS_AWAITING_JUDGEMENT,
    STATUS_CLOSED,
    STATUS_IN_SESSION,
    STATUS_NEEDS_MORE_EVIDENCE,
    STATUS_UNDER_REVIEW,
    STATUS_WITHDRAWN,
    TURN_MESSAGE_LIMIT,
    TURN_SPEAK_MINUTES,
    VIS_PRIVATE,
    VIS_PUBLIC,
    round_label,
)
from .embeds import build_case_review_embed, build_court_panel_embed, build_opening_post_embed
from .services.audit import send_audit_log
from .services.archive_export import build_archive
from .services.db import CaseRepo, Database, GuildSettingsRepo


log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class CourtBot(commands.Bot):
    def __init__(self, *, config: Config):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True  # 用于 role 判断/处罚更稳（需要在开发者后台开启）
        # 归档导出（HTML）与回合发言监管需要读取消息内容（需要在开发者后台开启 Message Content Intent）
        intents.message_content = True

        # 小型 VPS 优化：降低 Discord.py 的消息/成员缓存占用。
        # 本项目主要依赖交互对象与按需 fetch_member，不需要完整成员缓存。
        super().__init__(
            command_prefix="!",
            intents=intents,
            max_messages=config.max_message_cache,
            member_cache_flags=discord.MemberCacheFlags.none(),
            chunk_guilds_at_startup=False,
        )
        self.config = config

        self.db = Database(config.db_path)
        self.repo = CaseRepo(self.db)
        self.settings_repo = GuildSettingsRepo(self.db)
        self._settings_cache: dict[int, dict] = {}

        self._turn_timeout_task: asyncio.Task | None = None
        self._case_locks: dict[int, asyncio.Lock] = {}
        self._archive_semaphore = asyncio.Semaphore(config.archive_concurrency)

    # -------------------- 权限/工具方法 --------------------

    async def get_settings(self, guild_id: int, *, refresh: bool = False) -> Optional[dict]:
        """读取服务器设置（带简单缓存）。"""

        if not refresh and guild_id in self._settings_cache:
            return self._settings_cache[guild_id]

        settings = await self.settings_repo.get_settings(guild_id)
        if settings is not None:
            self._settings_cache[guild_id] = settings
        return settings

    async def is_admin(self, user: discord.abc.User, guild: discord.Guild | None = None) -> bool:
        """判断是否管理。

        规则：
        - 具备 Discord 原生 `管理员/管理服务器` 权限：直接视为管理（用于初始化与兜底）。
        - 否则检查服务器设置中的 `admin_role_ids`。
        """

        member: Optional[discord.Member]
        if isinstance(user, discord.Member):
            member = user
            guild = member.guild
        else:
            if guild is None:
                return False
            member = guild.get_member(user.id)
            if member is None:
                try:
                    member = await guild.fetch_member(user.id)
                except Exception:
                    return False

        settings = await self.get_settings(guild.id)
        if settings and (settings.get("admin_role_ids") or set()):
            # 已配置管理身份组：以身份组为准，但仍允许服务器管理员兜底
            if member.guild_permissions.administrator:
                return True
            admin_role_ids: set[int] = settings.get("admin_role_ids") or set()
            return any(r.id in admin_role_ids for r in member.roles)

        # 未配置时：允许具备高权限的成员进行初始化/兜底操作
        return bool(member.guild_permissions.administrator or member.guild_permissions.manage_guild)

    async def admin_mention(self, guild_id: int) -> str:
        """用于议诉频道里 @ 管理（只取第一个管理身份组避免 ping 太多）。"""

        settings = await self.get_settings(guild_id)
        if settings:
            admin_role_ids: set[int] = settings.get("admin_role_ids") or set()
            rid = next(iter(admin_role_ids), None)
            if rid:
                return f"<@&{rid}>"
        return "@管理"

    async def get_channel_or_thread(self, channel_id: int) -> Optional[discord.abc.Messageable]:
        ch = self.get_channel(channel_id)
        if ch is not None:
            return ch
        try:
            fetched = await self.fetch_channel(channel_id)
            return fetched
        except Exception:
            return None

    def _case_lock(self, case_id: int) -> asyncio.Lock:
        lock = self._case_locks.get(case_id)
        if lock is None:
            lock = asyncio.Lock()
            self._case_locks[case_id] = lock
        return lock

    def forget_case_runtime_state(self, case_id: int) -> None:
        """释放已结束议诉的进程内临时状态，避免长期运行时小对象累积。"""

        lock = self._case_locks.get(int(case_id))
        if lock is not None and not lock.locked():
            self._case_locks.pop(int(case_id), None)


    async def get_case_space(self, case: dict) -> Optional[discord.abc.Messageable]:
        # 私密：court_channel_id；公开：court_thread_id
        cid = case.get("court_channel_id")
        tid = case.get("court_thread_id")
        if cid:
            return await self.get_channel_or_thread(int(cid))
        if tid:
            return await self.get_channel_or_thread(int(tid))
        return None


    # -------------------- 标题生成 --------------------

    @staticmethod
    def _clean_title_part(text: str, max_len: int) -> str:
        text = (text or "").strip().replace("\n", " ").replace("\r", " ")
        text = re.sub(r"\s+", "", text)
        # 避免出现类似 <@123>、<#123> 的奇怪展示
        text = text.replace("<", "").replace(">", "")
        return text[:max_len]

    async def _get_member_display_name(self, guild: discord.Guild, user_id: int) -> str:
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except Exception:
                member = None
        if member is None:
            return str(user_id)
        return member.display_name

    async def build_court_title(self, case: dict, guild: discord.Guild) -> str:
        """生成开始议诉空间标题。

        格式（用户要求）：
        时间｜投诉人名投诉被投诉人名｜违反：xxxx
        """

        # 使用 UTC+8（香港/大陆时间），用于标题展示
        tz = None
        try:
            tz = ZoneInfo("Asia/Hong_Kong")
        except Exception:
            tz = None

        now = datetime.now(tz) if tz else datetime.now()
        # 可读格式：YYYY-MM-DD HH:MM
        time_str = now.strftime("%Y-%m-%d %H:%M")

        complainant_name = self._clean_title_part(
            await self._get_member_display_name(guild, int(case["complainant_id"])),
            12,
        )
        defendant_name = self._clean_title_part(
            await self._get_member_display_name(guild, int(case["defendant_id"])),
            12,
        )

        rule_text = self._clean_title_part(str(case.get("rule_text", "")), 20)
        if not rule_text:
            rule_text = "未知规则"

        # 去掉前缀，使用更明显的分隔符
        title = f"{time_str}｜{complainant_name}投诉{defendant_name}｜违反：{rule_text}"
        return title[:100]

    # -------------------- 生命周期 --------------------

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.db.init_schema()

        # 加载 Cog
        await self.load_extension("court_bot.cogs.court")

        # 启用指令本地化（让 locale_str 生效）
        # 没有 translator 的情况下，Discord 只会看到内部英文名，例如 /court show_settings
        await self.tree.set_translator(StaticExtrasTranslator())

        # 恢复 persistent views
        await self.restore_persistent_views()

        # 启动“发言回合超时”扫描任务（10 分钟窗口）
        if self._turn_timeout_task is None:
            self._turn_timeout_task = asyncio.create_task(self._turn_timeout_loop())

        # 同步指令
        #
        # Discord 的应用指令分为两类：
        # - Global Commands：全局生效，但更新/刷新可能有延迟
        # - Guild Commands：仅某个服务器生效，但更新几乎即时
        #
        # 为避免“服务器里出现两组指令（旧的 global + 新的 guild 或反过来）”，本项目采用：
        # - 若设置了 COMMAND_GUILD_ID：只同步该 Guild 的命令，并把全局命令同步为空（清理旧的全局指令）
        # - 否则：同步全局命令
        if self.config.command_guild_id:
            guild = discord.Object(id=self.config.command_guild_id)
            try:
                await self.tree.sync(guild=guild)
                log.info("Synced commands to guild %s", self.config.command_guild_id)
            except Exception:
                log.exception("Failed to sync commands to guild %s", self.config.command_guild_id)

            # 清理旧的全局命令（当前模式不使用全局命令）
            try:
                await self.tree.sync()
                log.info("Synced commands globally (purge old global commands)")
            except Exception:
                log.exception("Failed to purge global commands")
        else:
            try:
                await self.tree.sync()
                log.info("Synced commands globally")
            except Exception:
                log.exception("Failed to sync commands globally")

    async def close(self) -> None:
        if self._turn_timeout_task is not None:
            self._turn_timeout_task.cancel()
            try:
                await self._turn_timeout_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        await super().close()
        await self.db.close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")

    async def on_message(self, message: discord.Message) -> None:
        """议诉频道纪律兜底。

        - 非当前发言者（且非管理）发言将被删除
        - 当前发言者发言计数，达到上限自动结束本轮
        """

        try:
            if message.author.bot:
                return
            if message.guild is None:
                return

            # 仅处理“议诉频道”
            case = await self.repo.find_case_by_space_id(message.guild.id, message.channel.id)
            if not case:
                return

            # 非议诉中：一律不允许当事人/观众发言（防止权限误配）
            if case.get("status") != STATUS_IN_SESSION:
                if not await self.is_admin(message.author, message.guild):
                    try:
                        await message.delete()
                    except Exception:
                        pass
                return

            st = await self.repo.get_turn_state(int(case["id"]))
            if not st:
                # 无发言权窗口：仅管理可发言，其余删除
                if not await self.is_admin(message.author, message.guild):
                    try:
                        await message.delete()
                    except Exception:
                        pass
                return

            speaker_id = int(st.get("speaker_id") or 0)
            if message.author.id == speaker_id:
                # 当前发言者：计数（即便是管理也计数，避免“测试时不生效”）
                new_count = await self.repo.increment_turn_msg_count(int(case["id"]), delta=1)
                msg_limit = int(st.get("msg_limit") or TURN_MESSAGE_LIMIT)
                if new_count is not None and new_count >= msg_limit:
                    # 达到条数上限：自动结束本轮
                    try:
                        await self.end_speaking_turn(
                            case_id=int(case["id"]),
                            operator=None,
                            reason="达到条数上限自动结束",
                        )
                    except Exception:
                        log.exception("Failed to auto-end turn (case %s)", case.get("id"))
                return

            # 非当前发言者：管理可插话，其余删除
            if await self.is_admin(message.author, message.guild):
                return

            try:
                await message.delete()
            except Exception:
                pass
            return
        finally:
            # 仍然交给 commands 扩展处理（若未来新增前缀命令）
            try:
                await self.process_commands(message)
            except Exception:
                pass


    async def _turn_timeout_loop(self) -> None:
        """后台扫描 turn_state 超时，自动结束本轮发言。

        说明：
        - 该任务在 setup_hook 启动
        - 依赖 turn_state.expires_at（UTC ISO）
        """

        await self.wait_until_ready()

        while not self.is_closed():
            try:
                expired = await self.repo.list_expired_turn_states()
                for st in expired:
                    case_id = int(st.get("case_id") or 0)
                    if not case_id:
                        continue
                    try:
                        await self.end_speaking_turn(case_id=case_id, operator=None, reason="超时自动结束")
                    except Exception:
                        log.exception("Failed to end expired turn (case %s)", case_id)
            except Exception:
                log.exception("Turn timeout loop error")

            # 频率不需要太高，避免 API/DB 压力
            await asyncio.sleep(15)

    # -------------------- persistent views 恢复 --------------------

    async def restore_persistent_views(self) -> None:
        from .views.review import ReviewView
        from .views.court import CourtView
        from .views.continue_panel import ContinueView
        from .views.judgement import JudgementView
        from .views.archive import ArchiveView
        from .views.entry import EntryView

        self.add_view(EntryView(bot=self))
        cases = await self.repo.list_cases_for_restore()
        for c in cases:
            cid = int(c["id"])
            status = c.get("status")

            if status in (STATUS_UNDER_REVIEW, STATUS_NEEDS_MORE_EVIDENCE):
                view = ReviewView(bot=self, case_id=cid)
                if c.get("review_message_id"):
                    self.add_view(view, message_id=int(c["review_message_id"]))
                else:
                    self.add_view(view)

            if status == STATUS_IN_SESSION:
                # 全局注册（不绑定 message_id）：使同一议诉频道内的任意“系统消息按钮”也能在重启后继续工作
                view = CourtView(bot=self, case_id=cid)
                self.add_view(view)

            if status in (STATUS_CLOSED, STATUS_WITHDRAWN):
                # 结案/撤诉后提供“归档并删除”按钮
                view = ArchiveView(bot=self, case_id=cid)
                self.add_view(view)

            if status == STATUS_AWAITING_CONTINUE:
                view = ContinueView(bot=self, case_id=cid)
                try:
                    st = await self.repo.get_continue_state(cid)
                except Exception:
                    st = None
                msg_id = int(st.get("panel_message_id") or 0) if st else 0
                if msg_id:
                    self.add_view(view, message_id=msg_id)
                else:
                    self.add_view(view)

            if status == STATUS_AWAITING_JUDGEMENT and c.get("judge_panel_message_id"):
                view = JudgementView(bot=self, case_id=cid)
                self.add_view(view, message_id=int(c["judge_panel_message_id"]))

        log.info("Restored persistent views: %s cases", len(cases))

    # -------------------- 开始议诉：创建议诉频道 + 控制面板 --------------------

    async def create_court_space(self, *, case_id: int, approved_visibility: str) -> Optional[discord.abc.Messageable]:
        case = await self.repo.get_case(case_id)
        if not case:
            return None

        # Ticket 风格：公开/私密都创建“每案一个频道”。
        space = await self._create_case_channel(case, approved_visibility=approved_visibility)
        return space

    async def _create_case_channel(self, case: dict, *, approved_visibility: str) -> Optional[discord.TextChannel]:
        guild = self.get_guild(int(case["guild_id"]))
        if guild is None:
            guild = await self.fetch_guild(int(case["guild_id"]))

        settings = await self.get_settings(guild.id)
        if not settings:
            raise RuntimeError("本服务器尚未配置议诉系统。请先运行：/议诉 设置")

        court_category_id = settings.get("court_category_id")
        if not court_category_id:
            raise RuntimeError("未配置‘议诉分类’，请先运行：/议诉 设置")

        admin_role_ids: set[int] = settings.get("admin_role_ids") or set()
        if not admin_role_ids:
            raise RuntimeError("未配置‘管理身份组’，请先运行：/议诉 设置")

        if approved_visibility == VIS_PUBLIC and not settings.get("audience_role_id"):
            raise RuntimeError("未配置‘观众身份组’，无法创建公开议诉。请先运行：/议诉 设置")

        category = guild.get_channel(int(court_category_id))
        if not isinstance(category, discord.CategoryChannel):
            category = await guild.fetch_channel(int(court_category_id))
        if not isinstance(category, discord.CategoryChannel):
            raise RuntimeError("COURT_CATEGORY_ID 不是有效分类")

        # 权限覆写
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
        everyone = guild.default_role
        overwrites[everyone] = discord.PermissionOverwrite(view_channel=False)

        # 确保 Bot 自身可见可操作（避免被 @everyone 的 view_channel=False 误伤）
        bot_member = guild.me
        if bot_member is None and self.user is not None:
            try:
                bot_member = await guild.fetch_member(self.user.id)
            except Exception:
                bot_member = None

        if bot_member is not None:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                attach_files=True,
                read_message_history=True,
                manage_messages=True,
                use_application_commands=True,
            )

        for rid in admin_role_ids:
            role = guild.get_role(rid)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    attach_files=True,
                    read_message_history=True,
                    use_application_commands=True,
                )

        # 公开议诉：观众只读可见
        audience_role_id = settings.get("audience_role_id")
        if approved_visibility == VIS_PUBLIC and audience_role_id:
            audience_role = guild.get_role(int(audience_role_id))
            if audience_role:
                overwrites[audience_role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=False,
                    attach_files=False,
                    read_message_history=True,
                    use_application_commands=True,
                )

        complainant = guild.get_member(int(case["complainant_id"]))
        if complainant is None:
            try:
                complainant = await guild.fetch_member(int(case["complainant_id"]))
            except Exception:
                complainant = None

        defendant = guild.get_member(int(case["defendant_id"]))
        if defendant is None:
            try:
                defendant = await guild.fetch_member(int(case["defendant_id"]))
            except Exception:
                defendant = None

        if complainant:
            overwrites[complainant] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
                attach_files=False,
                read_message_history=True,
                use_application_commands=True,
            )
        if defendant:
            overwrites[defendant] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
                attach_files=False,
                read_message_history=True,
                use_application_commands=True,
            )

        name = f"case-{int(case['id']):04d}"
        court_title = await self.build_court_title(case, guild)
        vis_label = "公开" if approved_visibility == VIS_PUBLIC else "私密"
        channel = await guild.create_text_channel(
            name=name,
            category=category,
            overwrites=overwrites,
            topic=f"{court_title}（{vis_label}）",
        )

        await self.repo.set_court_space(int(case["id"]), court_channel_id=channel.id, court_thread_id=None)

        # 初始公告 + 控制面板
        admin_ping = await self.admin_mention(guild.id)
        evidences = await self.repo.list_evidence(int(case["id"]))
        opening_embed = build_opening_post_embed(case, evidences)
        await channel.send(
            content=(
                f"{admin_ping} 已开始议诉（{vis_label}）。\n"
                f"投诉人：<@{case['complainant_id']}>\n"
                f"被投诉人：<@{case['defendant_id']}>"
            ),
            embed=opening_embed,
        )

        from .views.court import CourtView

        view = CourtView(bot=self, case_id=int(case["id"]))
        self.add_view(view)
        panel_msg = await channel.send(embed=build_court_panel_embed(await self.repo.get_case(int(case["id"]))), view=view)
        await self.repo.set_court_panel_message(int(case["id"]), panel_msg.id)

        await send_audit_log(
            bot=self,
            audit_channel_id=settings.get("audit_log_channel_id"),
            title="创建议诉频道",
            description=f"议诉 #{case['id']} 已创建频道（{vis_label}）：{channel.mention}",
            case_id=int(case["id"]),
        )

        return channel


    # -------------------- 面板刷新 / 进入裁决 --------------------

    async def refresh_court_panel(self, case: dict) -> None:
        panel_msg_id = case.get("court_panel_message_id")
        if not panel_msg_id:
            return

        space = await self.get_case_space(case)
        if space is None:
            return

        try:
            msg = await space.fetch_message(int(panel_msg_id))
        except Exception:
            return

        view = None
        if case.get("status") == STATUS_IN_SESSION:
            from .views.court import CourtView

            view = CourtView(bot=self, case_id=int(case["id"]))
            self.add_view(view)

        if case.get("status") in (STATUS_CLOSED, STATUS_WITHDRAWN):
            from .views.archive import ArchiveView

            view = ArchiveView(bot=self, case_id=int(case["id"]))
            self.add_view(view)

        try:
            # 当议诉不在进行中（待裁决/已结案/撤诉等）时移除按钮，避免误操作
            await msg.edit(embed=build_court_panel_embed(case), view=view)
        except Exception:
            pass



    async def refresh_review_message(self, case: dict, *, keep_review_actions: bool = False) -> bool:
        """刷新管理审核频道的议诉卡片。

        用途：
        - 裁决/结案后，原审核面板应同步显示“已结案”等状态
        - 归档并删除后，去除议诉频道（频道已不存在）
        """

        ch_id = case.get("review_channel_id")
        msg_id = case.get("review_message_id")
        if not ch_id or not msg_id:
            return False

        ch = await self.get_channel_or_thread(int(ch_id))
        if ch is None or not isinstance(ch, discord.TextChannel):
            return False

        try:
            msg = await ch.fetch_message(int(msg_id))
        except Exception:
            return False

        evidences = await self.repo.list_evidence(int(case["id"]))
        try:
            view = None
            if keep_review_actions and case.get("status") in (STATUS_UNDER_REVIEW, STATUS_NEEDS_MORE_EVIDENCE):
                from .views.review import ReviewView

                view = ReviewView(bot=self, case_id=int(case["id"]))
                self.add_view(view)

            await msg.edit(embed=build_case_review_embed(case, evidences), view=view)
            return True
        except Exception:
            log.exception("Failed to refresh review message for case %s", case.get("id"))
            raise


    # -------------------- 自主发言回合：发言权授予/撤回 --------------------

    async def begin_speaking_turn(self, *, case_id: int, speaker: discord.Member) -> dict:
        async with self._case_lock(case_id):
            return await self._begin_speaking_turn_impl(case_id=case_id, speaker=speaker)

    async def _begin_speaking_turn_impl(self, *, case_id: int, speaker: discord.Member) -> dict:
        """授予当前应发言方本轮发言权（10 分钟/10 条）。

        - 写入 turn_state
        - 临时开启该成员的 send_messages/attach_files
        - 在频道内发出系统提示
        """

        case = await self.repo.get_case(case_id)
        if not case:
            raise RuntimeError("未找到该议诉")

        if case.get("status") != STATUS_IN_SESSION:
            raise RuntimeError("当前议诉不在进行中")

        current_side = case.get("current_side") or SIDE_COMPLAINANT
        expected_id = int(case["complainant_id"]) if current_side == SIDE_COMPLAINANT else int(case["defendant_id"])
        if speaker.id != expected_id:
            raise RuntimeError("当前不是你发言的回合")

        st = await self.repo.get_turn_state(case_id)
        if st:
            raise RuntimeError("当前已有进行中的发言回合")

        space = await self.get_case_space(case)
        if not isinstance(space, discord.TextChannel):
            raise RuntimeError("无法找到议诉频道")

        expires_dt = datetime.now(timezone.utc) + timedelta(minutes=TURN_SPEAK_MINUTES)
        expires_at = expires_dt.isoformat()

        await self.repo.upsert_turn_state(
            case_id=case_id,
            channel_id=space.id,
            speaker_id=speaker.id,
            expires_at=expires_at,
            msg_count=0,
            msg_limit=TURN_MESSAGE_LIMIT,
        )

        # 打开当事人发言权限（覆盖其默认禁言设置）
        try:
            await space.set_permissions(
                speaker,
                overwrite=discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    attach_files=True,
                    read_message_history=True,
                    use_application_commands=True,
                ),
                reason=f"议诉 #{case_id} 本轮发言权授予",
            )
        except Exception as e:
            # 回滚 turn_state
            await self.repo.clear_turn_state(case_id)
            raise RuntimeError(f"设置发言权限失败：{e}")

        ts = int(expires_dt.timestamp())
        who = "投诉人" if current_side == SIDE_COMPLAINANT else "被投诉人"
        r = int(case.get("current_round") or 1)
        await space.send(
            embed=discord.Embed(
                title=f"【系统】第 {r} 轮（{round_label(r)}）{who}发言开始",
                description=(
                    f"发言者：{speaker.mention}\n"
                    f"截止：<t:{ts}:R>\n"
                    f"条数上限：{TURN_MESSAGE_LIMIT} 条\n\n"
                    "请直接在本频道发送文字/图片/文件；发完点击面板『结束本轮发言』。"
                ),
                color=0x5865F2,
            )
        )

        await self.repo.log(
            case_id,
            "turn_started",
            speaker.id,
            {"round": r, "side": current_side, "expires_at": expires_at, "msg_limit": TURN_MESSAGE_LIMIT},
        )

        return await self.repo.get_turn_state(case_id) or {}

    async def end_speaking_turn(self, *, case_id: int, operator: discord.abc.User | None, reason: str) -> dict:
        async with self._case_lock(case_id):
            return await self._end_speaking_turn_impl(case_id=case_id, operator=operator, reason=reason)

    async def _end_speaking_turn_impl(self, *, case_id: int, operator: discord.abc.User | None, reason: str) -> dict:
        """结束当前发言回合，撤回权限并推进到下一回合。

        可用于：
        - 发言者点击结束
        - 达到条数上限自动结束
        - 超时自动结束
        - 管理强制结束
        """

        case = await self.repo.get_case(case_id)
        if not case:
            raise RuntimeError("未找到该议诉")

        if case.get("status") != STATUS_IN_SESSION:
            # 若议诉已不在进行中，理论上不应存在 turn_state；这里做防御性清理。
            await self.repo.clear_turn_state(case_id)
            return case

        st = await self.repo.get_turn_state(case_id)
        if not st:
            # 无进行中的回合：返回当前 case
            return case

        space = await self.get_case_space(case)
        if not isinstance(space, discord.TextChannel):
            await self.repo.clear_turn_state(case_id)
            raise RuntimeError("无法找到议诉频道")

        speaker_id = int(st.get("speaker_id") or 0)
        guild = space.guild
        member = guild.get_member(speaker_id)
        if member is None:
            try:
                member = await guild.fetch_member(speaker_id)
            except Exception:
                member = None

        # 撤回发言权限：恢复为默认禁言（保留可见/可用指令）
        if member is not None:
            try:
                await space.set_permissions(
                    member,
                    overwrite=discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=False,
                        attach_files=False,
                        read_message_history=True,
                        use_application_commands=True,
                    ),
                    reason=f"议诉 #{case_id} 结束本轮发言",
                )
            except Exception:
                pass

        await self.repo.clear_turn_state(case_id)

        updated_case = await self.repo.advance_turn(case_id)
        await self.refresh_court_panel(updated_case)

        # 系统提示：下一步
        next_side = updated_case.get("current_side") or SIDE_COMPLAINANT
        next_expected_id = (
            int(updated_case["complainant_id"]) if next_side == SIDE_COMPLAINANT else int(updated_case["defendant_id"])
        )

        await self.repo.log(
            case_id,
            "turn_ended",
            operator.id if operator else None,
            {
                "reason": reason,
                "speaker_id": speaker_id,
                "next_round": int(updated_case.get("current_round") or 1),
                "next_side": next_side,
            },
        )

        if updated_case.get("status") == STATUS_AWAITING_CONTINUE:
            await space.send("【系统】已完成本轮发言，进入‘是否继续议诉’投票阶段。")
            await self.enter_continue_panel(updated_case)
            return updated_case

        if updated_case.get("status") == STATUS_AWAITING_JUDGEMENT:
            # 兜底：若未来状态机直接进入裁决
            await space.send("【系统】议诉已结束，进入裁决。")
            await self.enter_judgement(updated_case)
            return updated_case

        from .views.court import CourtView

        quick_view = CourtView(bot=self, case_id=case_id)
        await space.send(
            content=f"【系统】已结束本轮发言（{reason}）。下一位发言者：<@{next_expected_id}>。",
            view=quick_view,
        )
        return updated_case

    async def enter_continue_panel(self, case: dict) -> None:
        """三辩结束后，发送“是否继续议诉”面板到议诉频道。"""

        if case.get("status") != STATUS_AWAITING_CONTINUE:
            return

        space = await self.get_case_space(case)
        if space is None:
            return

        from .views.continue_panel import ContinueView
        from .embeds import build_continue_panel_embed

        view = ContinueView(bot=self, case_id=int(case["id"]))
        self.add_view(view)

        # 初始状态：双方未选择
        embed = build_continue_panel_embed(case, None)

        msg = await space.send(embed=embed, view=view)
        await self.repo.upsert_continue_state(case_id=int(case["id"]), panel_message_id=msg.id)
        await self.repo.log(int(case["id"]), "enter_continue_panel", None, {"message_id": msg.id})

        settings = await self.get_settings(int(case["guild_id"]))
        await send_audit_log(
            bot=self,
            audit_channel_id=settings.get("audit_log_channel_id") if settings else None,
            title="进入继续/结束议诉投票",
            description=f"议诉 #{case['id']} 已进入‘是否继续议诉’阶段。",
            case_id=int(case["id"]),
        )

    async def enter_judgement(self, case: dict) -> None:
        """议诉结束后，向管理裁决频道发送单击式裁决面板。"""

        # 只在 awaiting_judgement 状态触发
        if case.get("status") != STATUS_AWAITING_JUDGEMENT:
            return

        settings = await self.get_settings(int(case["guild_id"]))
        if not settings or not settings.get("judge_panel_channel_id"):
            # 没配置裁决频道时，不阻断结案，但需要管理手动处理
            space = await self.get_case_space(case)
            if space is not None:
                await space.send("【系统】本服务器未配置‘裁决面板频道’，请管理先运行：/议诉 设置")
            return

        judge_channel = await self.get_channel_or_thread(int(settings["judge_panel_channel_id"]))
        if judge_channel is None or not isinstance(judge_channel, discord.TextChannel):
            return

        from .views.judgement import JudgementView

        view = JudgementView(bot=self, case_id=int(case["id"]))
        self.add_view(view)

        msg = await judge_channel.send(
            content=f"议诉 #{case['id']} 已结束，等待裁决。",
            embed=discord.Embed(
                title=f"议诉 #{case['id']}｜裁决面板",
                description=(
                    f"投诉人：<@{case['complainant_id']}>\n"
                    f"被投诉人：<@{case['defendant_id']}>\n\n"
                    "请点击下方按钮并填写说明后发布最终裁决。"
                ),
                color=0x2B2D31,
            ),
            view=view,
        )

        await self.repo.set_judge_panel_message(int(case["id"]), judge_channel.id, msg.id)
        await self.repo.log(int(case["id"]), "enter_judgement", None, {"judge_panel_message_id": msg.id})

        await send_audit_log(
            bot=self,
            audit_channel_id=settings.get("audit_log_channel_id") if settings else None,
            title="进入裁决",
            description=f"议诉 #{case['id']} 已进入裁决阶段。",
            case_id=int(case["id"]),
        )

    # -------------------- 归档并删除（DCE 风格 HTML/ZIP） --------------------

    async def archive_and_delete_case(self, *, case_id: int, operator: discord.abc.User | None) -> None:
        case = await self.repo.get_case(case_id)
        if not case:
            raise RuntimeError("未找到该议诉")

        if case.get("status") not in (STATUS_CLOSED, STATUS_WITHDRAWN):
            raise RuntimeError("仅支持已结案/已撤诉议诉归档")

        settings = await self.get_settings(int(case["guild_id"]))
        if not settings or not settings.get("archive_channel_id"):
            raise RuntimeError("未配置‘归档频道’，请先运行：/议诉 设置")

        archive_channel = await self.get_channel_or_thread(int(settings["archive_channel_id"]))
        if archive_channel is None or not isinstance(archive_channel, discord.TextChannel):
            raise RuntimeError("归档频道无效")

        space = await self.get_case_space(case)
        if space is None or not isinstance(space, discord.TextChannel):
            raise RuntimeError("无法找到议诉频道")

        # 裁决信息（含理由）/ 撤诉信息
        judgement = await self.repo.get_latest_judgement(case_id)
        reason_log = await self.repo.get_latest_log_by_action(case_id, "judgement_reason")
        reason_text = None
        if reason_log and reason_log.get("meta"):
            reason_text = (reason_log["meta"] or {}).get("reason")

        decision = judgement.get("decision") if judgement else None
        penalty = judgement.get("penalty") if judgement else None

        if case.get("status") == STATUS_WITHDRAWN:
            decision = "撤诉"
            penalty = "无"
            reason_text = reason_text or case.get("status_reason") or "投诉人撤诉"

        # 时间信息
        created_at = None
        try:
            created_at = datetime.fromisoformat(str(case.get("created_at")))
        except Exception:
            created_at = None

        closed_at = None
        if judgement and judgement.get("created_at"):
            try:
                closed_at = datetime.fromisoformat(str(judgement.get("created_at")))
            except Exception:
                closed_at = None

        header_lines = [
            f"议诉编号：#{case_id}",
            f"频道：{space.name}（ID：{space.id}）",
            f"开始议诉时间：{created_at.isoformat(sep=' ', timespec='minutes') if created_at else '未知'}",
            f"结案时间：{closed_at.isoformat(sep=' ', timespec='minutes') if closed_at else '未知'}",
            f"投诉人：{case.get('complainant_id')}",
            f"被投诉人：{case.get('defendant_id')}",
            f"违反规则：{case.get('rule_text')}",
            f"申请说明：{case.get('description')}",
            f"裁决：{decision or '未知'}",
            f"处罚/处置：{penalty or '无'}",
            f"裁决说明：{reason_text or '（无）'}",
        ]

        if operator:
            header_lines.append(f"归档操作人：{operator.id}")

        result = None
        async with self._archive_semaphore:
            try:
                result = await build_archive(
                    channel=space,
                    header_lines=header_lines,
                    guild_filesize_limit=int(space.guild.filesize_limit),
                    media_budget_bytes=(
                        int(self.config.archive_media_budget_mb) * 1024 * 1024 if self.config.archive_media_budget_mb > 0 else 0
                    ),
                    single_image_max_bytes=(
                        int(self.config.archive_single_image_max_mb) * 1024 * 1024 if self.config.archive_single_image_max_mb > 0 else 0
                    ),
                )

                # 发送到归档频道（仅管理可见）
                summary = discord.Embed(
                    title=f"议诉 #{case_id}｜归档",
                    description=f"已从 {space.mention} 导出为 {result.mode.upper()}。",
                    color=0x2B2D31,
                )
                summary.add_field(name="投诉人", value=f"<@{case['complainant_id']}> (`{case['complainant_id']}`)", inline=True)
                summary.add_field(name="被投诉人", value=f"<@{case['defendant_id']}> (`{case['defendant_id']}`)", inline=True)
                summary.add_field(name="裁决", value=f"{decision or '未知'}｜{penalty or '无'}", inline=False)
                if reason_text:
                    summary.add_field(name="说明", value=str(reason_text)[:1024], inline=False)
                if result.warnings:
                    summary.add_field(name="注意", value="\n".join(result.warnings)[:1024], inline=False)
                if operator:
                    summary.set_footer(text=f"归档人：{operator.id}")

                ext = "zip" if result.mode == "zip" else "html"
                archive_filename = f"case-{case_id:04d}-archive.{ext}"
                file = discord.File(fp=io.BytesIO(result.data), filename=archive_filename)
                msg = await archive_channel.send(
                    embed=summary,
                    file=file,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

                await self.repo.log(
                    case_id,
                    "case_archived",
                    operator.id if operator else None,
                    {
                        "mode": result.mode,
                        "filename": archive_filename,
                        "archive_channel_id": int(archive_channel.id),
                        "archive_message_id": int(msg.id),
                        "warnings": result.warnings,
                    },
                )

                # 清理议诉频道引用，避免后续 restore 仍尝试挂载 view
                await self.repo.clear_court_space(case_id)
                self.forget_case_runtime_state(case_id)

                try:
                    updated_case = await self.repo.get_case(case_id)
                    if updated_case:
                        await self.refresh_review_message(updated_case)
                except Exception:
                    pass

                # 删除议诉频道
                try:
                    await space.delete(reason=f"议诉 #{case_id} 已归档并删除")
                except Exception as e:
                    raise RuntimeError(f"归档成功，但删除频道失败：{e}")
            finally:
                # 归档可能产生较大的 bytes/base64/zip 对象；归档结束后主动触发一次回收，降低小内存 VPS 峰值残留。
                result = None
                gc.collect()

