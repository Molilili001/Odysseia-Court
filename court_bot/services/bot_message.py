from __future__ import annotations

import asyncio
import copy
import logging
import re
from contextlib import AsyncExitStack, asynccontextmanager
from urllib.parse import urlsplit
from weakref import WeakValueDictionary

import discord

from .bot_message_repo import BotMessageRepo

log = logging.getLogger(__name__)
THREAD_TYPES = {discord.ChannelType.public_thread, discord.ChannelType.private_thread,
                discord.ChannelType.news_thread}
TEXT_TYPES = THREAD_TYPES | {discord.ChannelType.text, discord.ChannelType.news}
LINK = re.compile(r'https?://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/channels/([0-9]+)/([0-9]+)(?:/([0-9]+))?/?')


class BotMessageError(Exception):
    """An error safe to display to the operator."""


def _id(value) -> int:
    if not re.fullmatch(r'[0-9]{1,19}', str(value)) or not 0 < int(value) < 2**63:
        raise BotMessageError('❌ 消息或频道 ID 格式错误。')
    return int(value)


def parse_message_reference(text: str, guild_id: int, current_channel_id: int) -> tuple[int, int]:
    text = (text or '').strip()
    match = LINK.fullmatch(text)
    if match and match[3]:
        if _id(match[1]) != guild_id:
            raise BotMessageError('❌ 不允许跨服务器操作消息。')
        return _id(match[2]), _id(match[3])
    pair = re.fullmatch(r'([0-9]+)-([0-9]+)', text)
    if pair:
        return _id(pair[1]), _id(pair[2])
    if text.isascii() and text.isdecimal():
        return _id(current_channel_id), _id(text)
    raise BotMessageError('❌ 消息格式错误，请填写 Discord 消息链接、频道ID-消息ID 或纯消息ID。')


def parse_channel_reference(text: str, guild_id: int) -> int:
    text = (text or '').strip()
    match = LINK.fullmatch(text)
    if match:
        if _id(match[1]) != guild_id:
            raise BotMessageError('❌ 不允许跨服务器发送。')
        return _id(match[2])
    mention = re.fullmatch(r'<#([0-9]+)>', text)
    return _id(mention[1] if mention else text)


def snapshot(message) -> dict:
    return {'content': message.content or '', 'embeds': [copy.deepcopy(e.to_dict())
        for e in message.embeds if e.type in (None, 'rich')]}


def _url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return parsed.scheme in ('https', 'http') and bool(parsed.hostname) and len(value) <= 2048
    except ValueError:
        return False


def validate_payload(content: str, embeds: list[discord.Embed], *, has_attachments=False) -> None:
    if len(content) > 2000:
        raise BotMessageError('❌ 消息正文超过 2000 字上限。')
    if len(embeds) > 10:
        raise BotMessageError('❌ 一条消息最多 10 个 Embed。')
    if not content.strip() and not embeds and not has_attachments:
        raise BotMessageError('❌ 消息不能完全为空；附件和组件不会从来源复制。')
    total = 0
    for embed in embeds:
        data = embed.to_dict()
        if data.get('type', 'rich') != 'rich':
            raise BotMessageError('❌ 只支持 rich Embed，自动链接预览不能编辑。')
        for value, maximum in ((embed.title or '', 256), (embed.description or '', 4096),
                               (embed.footer.text or '', 2048), (embed.author.name or '', 256)):
            if len(value) > maximum:
                raise BotMessageError(f'❌ Embed 字段超过 {maximum} 字上限。')
        if len(embed.fields) > 25 or any(not f.name or not f.value or len(f.name)>256 or len(f.value)>1024 for f in embed.fields):
            raise BotMessageError('❌ Embed 字段数量或长度非法。')
        if embed.colour is not None and not 0 <= embed.colour.value <= 0xFFFFFF:
            raise BotMessageError('❌ Embed 颜色非法。')
        urls = [data.get('url')]
        for name in ('image', 'thumbnail', 'author', 'footer'):
            section = data.get(name) or {}
            urls.extend([section.get('url'), section.get('icon_url')])
        if any(url and not (_url(url) or url.startswith('attachment://')) for url in urls):
            raise BotMessageError('❌ Embed 图片或链接必须是有效 HTTP/HTTPS URL。')
        if not any(data.get(key) for key in ('title','description','fields','image','thumbnail','author','footer')):
            raise BotMessageError('❌ Embed 不能为空，请填写标题、描述或图片。')
        total += len(embed)
    if total > 6000:
        raise BotMessageError('❌ 所有 Embed 的文字总长度不能超过 6000 字。')


def build_embed(fields: dict, base=None) -> discord.Embed:
    data = copy.deepcopy(base.to_dict() if isinstance(base, discord.Embed) else base or {})
    for key in ('title', 'description'):
        value = fields.get(key, '')
        if value:
            data[key] = value
        else:
            data.pop(key, None)
    color = fields.get('color', '').strip()
    if color:
        if not re.fullmatch(r'#?[0-9a-fA-F]{6}', color):
            raise BotMessageError('❌ 颜色格式错误，请填写 #5865F2 这样的六位色值。')
        data['color'] = int(color.lstrip('#'), 16)
    else:
        data.pop('color', None)
    footer = fields.get('footer', '')
    if footer:
        data['footer'] = {**data.get('footer', {}), 'text': footer}
    else:
        data.pop('footer', None)
    image = fields.get('image', '').strip()
    if image:
        if not _url(image):
            raise BotMessageError('❌ 图片 URL 必须是有效 HTTP/HTTPS 链接。')
        data['image'] = {'url': image}
    else:
        data.pop('image', None)
    embed = discord.Embed.from_dict(data)
    validate_payload('', [embed])
    return embed


async def respond(interaction, content='', **kwargs):
    kwargs['allowed_mentions'] = discord.AllowedMentions.none()
    if interaction.response.is_done():
        return await interaction.edit_original_response(content=content, **kwargs)
    return await interaction.response.send_message(content, ephemeral=True, **kwargs)


async def acknowledge_early(interaction, *, thinking=True):
    """Neutral ACK before slow work, except when the origin must first be unarchived."""
    channel = getattr(interaction, 'channel', None)
    if not getattr(channel, 'archived', False) and not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=thinking)


def _api_error(error: Exception) -> BotMessageError:
    if isinstance(error, discord.Forbidden):
        return BotMessageError('❌ Discord 拒绝操作：机器人缺少访问、编辑、发送或管理子区权限。')
    if isinstance(error, discord.NotFound):
        return BotMessageError('❌ 找不到消息或频道，可能已删除，或机器人无法访问。')
    return BotMessageError('❌ Discord 请求失败，操作结果可能不确定；请检查目标消息后再试。')


async def report_error(interaction, error):
    if isinstance(error, BotMessageError):
        text = str(error)
    elif isinstance(error, discord.HTTPException):
        text = str(_api_error(error))
        log.warning('Bot message Discord error', exc_info=error)
    else:
        text = '❌ 操作失败，详细信息已记录到控制台，请联系管理检查。'
        log.error('Bot message operation failed', exc_info=error)
    try:
        await respond(interaction, text, view=None)
    except discord.HTTPException:
        log.exception('Could not deliver bot message error response')


class BotMessageService:
    def __init__(self, bot, repo: BotMessageRepo):
        self.bot, self.repo = bot, repo
        self._locks = WeakValueDictionary()

    async def authorize(self, interaction, config=False, *, use_interaction_member=False):
        guild = interaction.guild
        if guild is None:
            raise BotMessageError('❌ 请在服务器内使用机器人消息。')
        member = interaction.user if use_interaction_member and hasattr(interaction.user, 'roles') else None
        if member is None:
            try:
                member = await guild.fetch_member(interaction.user.id)
            except discord.HTTPException as error:
                raise BotMessageError('❌ 无法确认你当前的服务器成员权限。') from error
        if await self.bot.is_admin(member, guild):
            return member
        if not config:
            allowed = await self.repo.get_allowed_roles(guild.id)
            if any(role.id in allowed for role in member.roles):
                return member
        raise BotMessageError('❌ 配置仅限 Court 管理员。' if config else '❌ 你没有机器人消息操作权限。')

    async def authorize_prepared_editor(self, interaction, message):
        """Recheck access before exposing content captured by slow preparation."""
        guild = interaction.guild
        if guild is None or message.guild.id != guild.id:
            raise BotMessageError('❌ 不允许跨服务器操作消息。')
        member = await self.authorize(interaction, use_interaction_member=True)
        channel = await self._channel(guild, member, message.channel.id, history=True)
        return await self._message(channel, message.id, editable=True)

    async def _channel(self, guild, member, channel_id, *, send=False, history=False):
        try:
            channel = await guild.fetch_channel(_id(channel_id))
            if channel.guild.id != guild.id:
                raise BotMessageError('❌ 不允许跨服务器操作。')
            if channel.type not in TEXT_TYPES:
                raise BotMessageError('❌ 目标必须是文字频道、公告频道或已有帖子/Thread；不支持创建论坛帖。')
            user_perms = channel.permissions_for(member)
            bot_member = guild.me or await guild.fetch_member(self.bot.user.id)
            bot_perms = channel.permissions_for(bot_member)
            if not user_perms.view_channel or (history and not user_perms.read_message_history):
                raise BotMessageError('❌ 你不能访问目标频道或读取其消息历史。')
            if not bot_perms.view_channel or (history and not bot_perms.read_message_history):
                raise BotMessageError('❌ 机器人缺少查看频道或读取消息历史权限。')
            if channel.type == discord.ChannelType.private_thread:
                for candidate, perms in ((member,user_perms), (bot_member,bot_perms)):
                    if not perms.manage_threads:
                        try:
                            await channel.fetch_member(candidate.id)
                        except discord.NotFound as error:
                            raise BotMessageError('❌ 操作者或机器人不是该私有 Thread 的成员。') from error
            if send:
                can_send = bot_perms.send_messages_in_threads if channel.type in THREAD_TYPES else bot_perms.send_messages
                if not can_send:
                    raise BotMessageError('❌ 机器人没有在目标频道/子区发送消息的权限。')
            return channel
        except discord.HTTPException as error:
            raise _api_error(error) from error

    async def resolve_channel(self, interaction, channel_id, send=False):
        member = await self.authorize(interaction)
        return await self._channel(interaction.guild, member, channel_id, send=send)

    async def _message(self, channel, message_id, *, editable=False):
        try:
            message = await channel.fetch_message(message_id)
        except discord.HTTPException as error:
            raise _api_error(error) from error
        if message.guild.id != channel.guild.id:
            raise BotMessageError('❌ 不允许跨服务器操作消息。')
        if editable:
            if message.author.id != self.bot.user.id or message.webhook_id:
                raise BotMessageError('❌ 这条消息不是当前 Court Bot 自己发送的，无法编辑。')
            if message.type not in (discord.MessageType.default, discord.MessageType.reply):
                raise BotMessageError('❌ 该系统消息不支持编辑。')
            if getattr(message.flags, 'is_components_v2', False):
                raise BotMessageError('❌ 该消息使用 Components V2，不能通过正文/Embed 编辑器修改。')
            self._archive_check(channel, restore=True)
        return message

    async def resolve_message(self, interaction, reference, editable=False):
        member = await self.authorize(interaction)
        cid, mid = parse_message_reference(reference, interaction.guild.id, interaction.channel_id)
        channel = await self._channel(interaction.guild, member, cid, history=True)
        return await self._message(channel, mid, editable=editable)

    def _archive_check(self, channel, *, restore):
        if channel.type not in THREAD_TYPES:
            return
        perms = channel.permissions_for(channel.guild.me)
        if channel.locked and not perms.manage_threads:
            raise BotMessageError('❌ Thread 已锁定，机器人需要管理子区权限，未进行修改。')
        if channel.archived:
            if not (perms.manage_threads or perms.send_messages_in_threads):
                raise BotMessageError('❌ Thread 已归档，机器人无法解除归档。')
            if restore and not (perms.manage_threads or channel.owner_id == self.bot.user.id):
                raise BotMessageError('❌ 机器人无法保证恢复归档，需要管理子区权限。')

    @asynccontextmanager
    async def _scope(self, interaction, target_id=None, *, sending=False, config=False):
        if interaction.guild is None:
            raise BotMessageError('❌ 请在服务器内使用机器人消息。')
        ids = sorted({interaction.channel_id, target_id or interaction.channel_id})
        # Keep strong references during lock acquisition; WeakValueDictionary prevents leaks.
        locks = []
        for cid in ids:
            key = (interaction.guild.id, cid)
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            locks.append(lock)
        async with AsyncExitStack() as stack:
            for lock in locks:
                await stack.enter_async_context(lock)
            member = await self.authorize(interaction, config=config)
            channels = {cid: await self._channel(interaction.guild, member, cid,
                        send=sending and cid == target_id) for cid in ids}
            for cid, channel in channels.items():
                self._archive_check(channel, restore=not (sending and cid == target_id))
            restored = []
            keep_active = set()
            try:
                for cid, channel in channels.items():
                    if channel.type in THREAD_TYPES and channel.archived:
                        try:
                            active = await channel.edit(archived=False, reason='机器人消息：临时解除归档')
                        except discord.HTTPException as error:
                            raise _api_error(error) from error
                        channels[cid] = active
                        restored.append(active)
                yield channels, keep_active
            finally:
                failures = []
                for channel in reversed(restored):
                    if channel.id in keep_active:
                        continue
                    try:
                        await channel.edit(archived=True, reason='机器人消息：恢复原归档状态')
                    except Exception:
                        log.exception('Failed to restore archive for thread %s', channel.id)
                        failures.append(channel.id)
                if failures:
                    raise BotMessageError('⚠️ 操作可能已执行，但恢复 Thread 归档失败，请管理检查：' + ', '.join(map(str, failures)))

    @asynccontextmanager
    async def interaction_scope(self, interaction, config=False):
        async with self._scope(interaction, config=config):
            yield

    async def _ack(self, interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)

    async def _early_ack(self, interaction):
        await acknowledge_early(interaction)

    async def _reconcile(self, message):
        """Resolve interrupted API/DB boundary against the remote state; fail closed if ambiguous."""
        rows = await self.repo.pending(message.guild.id, message.channel.id, message.id)
        for row in rows:
            current = snapshot(message)
            after = {'content':row['after_content'], 'embeds':row['after_embeds']}
            before = {'content':row['before_content'], 'embeds':row['before_embeds']}
            if current == after:
                await self.repo.finish(row['id'])
            elif current == before:
                await self.repo.discard(row['id'])
            else:
                raise BotMessageError('❌ 上次操作结果未确认，当前消息与保存快照不符；请管理检查历史后再操作。')

    async def _apply(self, interaction, message, after, action, before):
        member = await self.authorize(interaction)
        channel = await self._channel(interaction.guild, member, message.channel.id, history=True)
        message = await self._message(channel, message.id, editable=True)
        if snapshot(message) != before:
            raise BotMessageError('❌ 目标消息在操作期间发生变化，请重新执行。')
        embeds = [discord.Embed.from_dict(e) for e in after['embeds']]
        validate_payload(after['content'], embeds, has_attachments=bool(message.attachments))
        if embeds and not message.channel.permissions_for(message.guild.me).embed_links:
            raise BotMessageError('❌ 机器人缺少嵌入链接权限。')
        await self.authorize(interaction)
        history_id = await self.repo.begin(message.guild.id, message.channel.id, message.id,
            interaction.user.id, action, before, after)
        payload = {'allowed_mentions':discord.AllowedMentions.none()}
        if action != 'edit_embed':
            payload['content'] = after['content']
        if action != 'edit_content':
            payload['embeds'] = embeds
        try:
            edited = await message.edit(**payload)
        except (discord.Forbidden, discord.NotFound) as error:
            await self.repo.discard(history_id)
            raise _api_error(error) from error
        except discord.HTTPException as error:
            # A definite 4xx rejection cannot have edited the message; uncertain 5xx keeps recovery data.
            if 400 <= error.status < 500:
                await self.repo.discard(history_id)
            raise _api_error(error) from error
        try:
            await self.repo.finish(history_id)
        except Exception as error:
            raise BotMessageError('⚠️ 消息已修改，但历史确认失败；修改前快照已保存，请勿盲目重试。') from error
        note = '\n该消息包含交互组件，仅修改文字/Embed；对应系统后续刷新面板时可能覆盖修改。' if message.components else ''
        await respond(interaction, f'✅ 操作成功。[查看消息]({edited.jump_url})' + note)
        return edited

    async def _mutate(self, interaction, reference, action, *, content=None, index=None, fields=None, source=None, expected=None):
        await self._early_ack(interaction)
        if interaction.guild is None:
            raise BotMessageError('❌ 请在服务器内使用机器人消息。')
        cid, mid = parse_message_reference(reference, interaction.guild.id, interaction.channel_id)
        async with self._scope(interaction, cid) as (channels, _):
            await self._ack(interaction)
            member = await self.authorize(interaction)
            channel = await self._channel(interaction.guild, member, cid, history=True)
            message = await self._message(channel, mid, editable=True)
            await self._reconcile(message)
            before = snapshot(message)
            if expected is not None and before != expected:
                raise BotMessageError('❌ 消息已被修改，请重新打开编辑窗口，避免覆盖其他人的改动。')
            after = copy.deepcopy(before)
            if action == 'edit_content':
                after['content'] = content
            elif action == 'edit_embed':
                if index is None or not 0 <= index < len(after['embeds']):
                    raise BotMessageError('❌ 对应 Embed 已不存在，请重新打开编辑窗口。')
                after['embeds'][index] = build_embed(fields, after['embeds'][index]).to_dict()
            elif action == 'replace':
                draft = await self.resolve_message(interaction, source)
                if draft.id == message.id:
                    raise BotMessageError('❌ 来源与目标不能是同一条消息。')
                after = snapshot(draft)
                validate_payload(after['content'], [discord.Embed.from_dict(e) for e in after['embeds']])
            elif action == 'undo':
                history = await self.repo.history(interaction.guild.id, cid, mid)
                if not history or history[0]['action'] == 'send':
                    raise BotMessageError('❌ 该消息没有可以撤销的修改记录。')
                row = history[0]
                after = {'content':row['before_content'], 'embeds':row['before_embeds']}
            return await self._apply(interaction, message, after, action, before)

    async def edit_content(self, interaction, reference, content, expected=None):
        return await self._mutate(interaction, reference, 'edit_content', content=content, expected=expected)

    async def edit_embed(self, interaction, reference, index, fields, expected=None):
        return await self._mutate(interaction, reference, 'edit_embed', index=index, fields=fields, expected=expected)

    async def replace(self, interaction, target, source):
        return await self._mutate(interaction, target, 'replace', source=source)

    async def undo(self, interaction, reference):
        return await self._mutate(interaction, reference, 'undo')

    async def send(self, interaction, channel_id, content='', embeds=None, source=None, allow_mentions=False):
        await self._early_ack(interaction)
        async with self._scope(interaction, channel_id, sending=True) as (channels, keep_active):
            await self._ack(interaction)
            if source:
                draft = snapshot(await self.resolve_message(interaction, source))
                content, embeds = draft['content'], [discord.Embed.from_dict(e) for e in draft['embeds']]
            embeds = embeds or []
            validate_payload(content, embeds)
            member = await self.authorize(interaction)
            channel = await self._channel(interaction.guild, member, channel_id, send=True)
            if embeds and not channel.permissions_for(interaction.guild.me).embed_links:
                raise BotMessageError('❌ 机器人缺少嵌入链接权限。')
            try:
                message = await channel.send(content=content, embeds=embeds,
                    allowed_mentions=discord.AllowedMentions.all() if allow_mentions else discord.AllowedMentions.none())
            except discord.HTTPException as error:
                raise _api_error(error) from error
            keep_active.add(channel_id)
            try:
                hid = await self.repo.begin(interaction.guild.id, channel_id, message.id, interaction.user.id,
                    'send', {'content':'','embeds':[]}, snapshot(message))
                await self.repo.finish(hid)
            except Exception as error:
                raise BotMessageError(f'⚠️ 消息已发送，但历史保存失败，请勿重复发送。[查看消息]({message.jump_url})') from error
            await respond(interaction, f'✅ 已发送。[查看消息]({message.jump_url})\n仅复制正文与 rich Embed，不复制附件或组件。')
            return message

