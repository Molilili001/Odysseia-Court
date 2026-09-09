"""Independent bot-message command group and message context menu."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.app_commands import locale_str, Choice
from discord.ext import commands

from ..services.bot_message import BotMessageService, BotMessageError, parse_channel_reference, respond, report_error, acknowledge_early
from ..services.bot_message_repo import BotMessageRepo
from ..views.bot_message import open_editor, SendModal, preparation_deadline

def localized(name, chinese):
    return locale_str(name, zh_CN=chinese, zh_TW=chinese, en_US=chinese, en_GB=chinese)

class BotMessageGroup(app_commands.Group):

    def __init__(self, bot, service):
        super().__init__(name=localized('bot_message', '机器人消息'), description=localized('Manage bot messages', '管理机器人消息'), guild_only=True)
        self.bot = bot
        self.service = service

    @app_commands.command(name=localized('edit', '编辑'), description='编辑机器人消息正文或嵌入')
    @app_commands.rename(reference=localized('reference', '消息'))
    async def edit(self, interaction: discord.Interaction, reference: str):
        await open_editor(self.service, interaction, reference)

    @app_commands.command(name=localized('send', '发送'), description='发送正文、嵌入或复制来源消息')
    @app_commands.rename(channel=localized('channel', '频道'), thread_or_channel=localized('thread_or_channel', '帖子或频道'), mode=localized('mode', '形式'), source=localized('source', '来源消息'), allow_mentions=localized('allow_mentions', '允许提及'))
    @app_commands.choices(mode=[Choice(name='纯文本（弹窗输入）', value='text'), Choice(name='嵌入卡片（弹窗输入）', value='embed'), Choice(name='复制另一条消息的内容', value='copy')])
    async def send(self, interaction: discord.Interaction, channel: discord.TextChannel | None=None, thread_or_channel: str | None=None, mode: str='text', source: str | None=None, allow_mentions: bool=False):
        try:
            if channel is not None and thread_or_channel is not None:
                raise BotMessageError('频道与子区或频道参数不能同时填写。')
            if not interaction.guild:
                raise BotMessageError('请在服务器内使用。')
            channel_id = channel.id if channel else interaction.channel_id
            if thread_or_channel is not None:
                channel_id = parse_channel_reference(thread_or_channel, interaction.guild_id)
            if mode == 'copy':
                if not source:
                    raise BotMessageError('复制模式必须提供来源消息。')
                await self.service.send(interaction, channel_id, source=source, allow_mentions=allow_mentions)
                return
            if mode not in ('text', 'embed'):
                raise BotMessageError('未知的发送模式。')
            if source:
                raise BotMessageError('来源消息仅用于复制模式。')
            if not getattr(getattr(interaction, 'channel', None), 'archived', False):
                # The interaction payload contains the member's current roles.  Use it for
                # this non-mutating first check so a slow Discord REST round trip cannot
                # consume the modal response window. SendModal submission performs the
                # full fresh-member and channel checks again before sending anything.
                await self.service.authorize(interaction, use_interaction_member=True)
                await interaction.response.send_modal(
                    SendModal(self.service, interaction, channel_id, mode, allow_mentions)
                )
                return
            retry = lambda next_interaction: self.get_command('send').callback(
                self, next_interaction, channel=channel, thread_or_channel=thread_or_channel,
                mode=mode, source=source, allow_mentions=allow_mentions,
            )
            async with preparation_deadline(interaction, retry) as deadline:
                async with self.service.interaction_scope(interaction):
                    await self.service.resolve_channel(interaction, channel_id, send=True)
                    async with deadline.lock:
                        if deadline.sent or interaction.response.is_done():
                            return
                        await interaction.response.send_modal(
                            SendModal(self.service, interaction, channel_id, mode, allow_mentions)
                        )
        except Exception as error:
            await report_error(interaction, error)

    @app_commands.command(name=localized('replace', '替换'), description='用来源消息的正文和嵌入替换机器人消息')
    @app_commands.rename(target=localized('target', '目标消息'), source=localized('source', '来源消息'))
    async def replace(self, interaction: discord.Interaction, target: str, source: str):
        try:
            await self.service.replace(interaction, target, source)
        except Exception as error:
            await report_error(interaction, error)

    @app_commands.command(name=localized('undo', '撤销'), description='撤销指定机器人消息最近一次修改')
    @app_commands.rename(reference=localized('reference', '消息'))
    async def undo(self, interaction: discord.Interaction, reference: str):
        try:
            await self.service.undo(interaction, reference)
        except Exception as error:
            await report_error(interaction, error)

    @app_commands.command(name=localized('history', '历史'), description='查看最近十次机器人消息修改记录')
    @app_commands.rename(reference=localized('reference', '消息'))
    async def history(self, interaction: discord.Interaction, reference: str):
        try:
            await acknowledge_early(interaction)
            async with self.service.interaction_scope(interaction):
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True, thinking=True)
                message = await self.service.resolve_message(interaction, reference, editable=True)
                rows = await self.service.repo.history(interaction.guild_id, message.channel.id, message.id)
                lines = []
                for row in rows[:10]:
                    action = {'edit_content': '编辑正文', 'edit_embed': '编辑Embed', 'replace': '替换', 'undo': '撤销', 'send': '发送'}.get(row.get('action'), '修改')
                    lines.append(f"#{row.get('id', '?')} · {action} · <@{row.get('operator_id', '?')}> · {str(row.get('created_at', ''))[:40]}")
                await respond(interaction, '最近 10 条修改记录：\n' + '\n'.join(lines) if lines else '尚无修改记录。')
        except Exception as error:
            await report_error(interaction, error)

    @app_commands.command(name=localized('config', '配置'), description='管理独立的机器人消息操作身份组（议诉管理员）')
    @app_commands.rename(action=localized('action', '操作'), role=localized('role', '身份组'))
    @app_commands.choices(action=[Choice(name='查看当前配置', value='view'), Choice(name='添加可用身份组', value='add_role'), Choice(name='移除可用身份组', value='remove_role')])
    async def config(self, interaction: discord.Interaction, action: str='view', role: discord.Role | None=None):
        try:
            await acknowledge_early(interaction)
            async with self.service.interaction_scope(interaction, config=True):
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True, thinking=True)
                if action not in ('view', 'add_role', 'remove_role'):
                    raise BotMessageError('未知的设置操作。')
                feedback = ''
                if action != 'view':
                    if role is None:
                        raise BotMessageError('请指定身份组。')
                    if role.guild.id != interaction.guild_id:
                        raise BotMessageError('身份组必须属于当前服务器。')
                    method = self.service.repo.add_role if action == 'add_role' else self.service.repo.remove_role
                    await self.service.authorize(interaction, config=True)
                    changed = await method(interaction.guild_id, role.id)
                    if action == 'add_role':
                        feedback = '已添加身份组。' if changed else '该身份组已存在。'
                    else:
                        feedback = '已移除身份组。' if changed else '该身份组尚未添加。'
                roles = await self.service.repo.get_allowed_roles(interaction.guild_id)
                chunks = [feedback + '\n议诉管理沿用原有管理鉴权。\n额外允许的机器人消息操作身份组：\n']
                for role_id in sorted(roles):
                    text = f'<@&{role_id}> (`{role_id}`)\n'
                    if len(chunks[-1]) + len(text) > 1900:
                        chunks.append('')
                    chunks[-1] += text
                if not roles:
                    chunks[0] += '暂无'
                await respond(interaction, chunks[0], allowed_mentions=discord.AllowedMentions.none())
                for chunk in chunks[1:]:
                    await interaction.followup.send(chunk, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except Exception as error:
            await report_error(interaction, error)

class BotMessageCog(commands.Cog):

    def __init__(self, bot, service):
        self.bot = bot
        self.service = service
        self.group = BotMessageGroup(bot, service)
        self.context = app_commands.ContextMenu(name='编辑机器人消息', callback=self.context_edit)
        self.guild_ids = tuple(getattr(getattr(bot, 'config', None), 'command_guild_ids', ()))
        kwargs = {'guilds': [discord.Object(id=gid) for gid in self.guild_ids]} if self.guild_ids else {}
        bot.tree.add_command(self.group, **kwargs)
        bot.tree.add_command(self.context, **kwargs)

    @app_commands.guild_only()
    async def context_edit(self, interaction: discord.Interaction, message: discord.Message):
        await open_editor(self.service, interaction, message.jump_url)

    def cog_unload(self):
        for guild_id in self.guild_ids or (None,):
            kwargs = {'guild': discord.Object(id=guild_id)} if guild_id is not None else {}
            self.bot.tree.remove_command(self.group.name, **kwargs)
            self.bot.tree.remove_command(self.context.name, type=discord.AppCommandType.message, **kwargs)

async def setup(bot):
    # Database helpers commit the shared connection; wait for existing transactions.
    election = bot.get_cog('ElectionCog')
    transaction_locks = (
        (election.repo.lock, election.continuous_repo.lock) if election else ()
    )
    repo = BotMessageRepo(bot.db, transaction_locks=transaction_locks)
    await repo.init_schema()
    await bot.add_cog(BotMessageCog(bot, BotMessageService(bot, repo)))

