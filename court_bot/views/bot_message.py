"""Short-lived, owner-bound bot-message editors."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

import discord

from ..services.bot_message import BotMessageError, build_embed, snapshot, respond, report_error, acknowledge_early
PREPARATION_TIMEOUT = 1.5

def rich_embeds(message):
    return [embed for embed in message.embeds if embed.type == 'rich']

class RetryView(discord.ui.View):
    """Retry from a fresh interaction without retaining an authorization result."""

    def __init__(self, interaction, retry):
        super().__init__(timeout=300)
        self.owner_id = interaction.user.id
        self.guild_id = interaction.guild_id
        self.retry = retry
        button = discord.ui.Button(label='继续打开', style=discord.ButtonStyle.primary)
        button.callback = self.resume
        self.add_item(button)

    async def resume(self, interaction):
        try:
            check_owner(interaction, self.owner_id, self.guild_id)
            await self.retry(interaction)
        except Exception as error:
            await report_error(interaction, error)

@asynccontextmanager
async def preparation_deadline(interaction, retry):
    """Acknowledge slow preparation without exposing data or authorizing an action."""
    state = SimpleNamespace(lock=asyncio.Lock(), sent=False, watchdog_started=False)

    async def watchdog():
        await asyncio.sleep(PREPARATION_TIMEOUT)
        async with state.lock:
            if not interaction.response.is_done():
                state.watchdog_started = True
                waiting = RetryView(interaction, retry)
                waiting.children[0].disabled = True
                waiting.children[0].label = '正在准备…'
                await respond(
                    interaction,
                    '正在读取消息并检查权限，请稍候…',
                    view=waiting,
                )
                state.sent = True

    # Archived threads must be reopened before sending an acknowledgement.
    task = None
    if not getattr(getattr(interaction, 'channel', None), 'archived', False):
        task = asyncio.create_task(watchdog())
    try:
        yield state
    finally:
        if task is not None:
            if not state.watchdog_started:
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logging.getLogger(__name__).exception('Bot-message preparation watchdog failed')


def check_owner(interaction, owner_id, guild_id):
    if interaction.user.id != owner_id or interaction.guild_id != guild_id:
        raise BotMessageError('仅原操作人可在原服务器内使用此面板。')

async def deliver_editor(service, interaction, deadline, message, factory):
    """Keep completed preparation; a fresh click only opens the prepared UI.

    No write is authorized here. Modal submission refetches permissions and the
    message and checks its expected snapshot before any mutation.
    """
    async def show(current, current_message):
        value = factory(current, current_message)
        if isinstance(value, discord.ui.Modal):
            await current.response.send_modal(value)
        else:
            await respond(current, '选择要编辑的部分：', view=value)

    async with deadline.lock:
        if deadline.sent or interaction.response.is_done():
            async def ready(current):
                fresh_message = await service.authorize_prepared_editor(current, message)
                await show(current, fresh_message)
            view = RetryView(interaction, ready)
            view.children[0].label = '打开编辑器'
            await interaction.edit_original_response(
                content='准备完成，请打开编辑器。', view=view
            )
        else:
            await show(interaction, message)

def embed_inputs(embed=None):
    data = embed.to_dict() if embed else {}
    description = data.get('description', '')
    if len(description) > 4000:
        raise BotMessageError('此嵌入描述超过编辑框 4000 字限制，请使用替换命令复制完整嵌入。')
    values = [('title', '标题', data.get('title', ''), 256), ('description', '描述', description, 4000), ('color', '颜色（十六进制，如 #5865F2）', f"#{data['color']:06X}" if 'color' in data else '', 7), ('footer', '页脚文字', data.get('footer', {}).get('text', ''), 2048), ('image', '图片 URL', data.get('image', {}).get('url', ''), 4000)]
    return {key: discord.ui.TextInput(label=label, default=value, required=False, max_length=limit, style=discord.TextStyle.paragraph if key == 'description' else discord.TextStyle.short) for key, label, value, limit in values}

class BoundModal(discord.ui.Modal):

    def __init__(self, service, interaction, *, title):
        super().__init__(title=title, timeout=300)
        self.service = service
        self.owner_id = interaction.user.id
        self.guild_id = interaction.guild_id

    async def on_error(self, interaction, error):
        await report_error(interaction, error)

class ContentModal(BoundModal):

    def __init__(self, service, interaction, reference, message):
        super().__init__(service, interaction, title='编辑机器人消息正文')
        self.reference = reference
        self.expected = snapshot(message)
        if len(message.content) > 2000:
            raise BotMessageError('正文超过编辑框 2000 字限制，请使用替换命令。')
        self.content = discord.ui.TextInput(label='正文（可清空，有嵌入时可留空）', style=discord.TextStyle.paragraph, default=message.content, required=False, max_length=2000)
        self.add_item(self.content)

    async def on_submit(self, interaction):
        try:
            check_owner(interaction, self.owner_id, self.guild_id)
            await self.service.edit_content(interaction, self.reference, self.content.value, expected=self.expected)
        except Exception as error:
            await report_error(interaction, error)

class EmbedModal(BoundModal):

    def __init__(self, service, interaction, reference, message, index=0):
        super().__init__(service, interaction, title=f'编辑嵌入 {index + 1}')
        self.reference = reference
        self.index = index
        self.expected = snapshot(message)
        self.inputs = embed_inputs(rich_embeds(message)[index])
        for item in self.inputs.values():
            self.add_item(item)

    async def on_submit(self, interaction):
        try:
            check_owner(interaction, self.owner_id, self.guild_id)
            await self.service.edit_embed(interaction, self.reference, self.index, {key: item.value for key, item in self.inputs.items()}, expected=self.expected)
        except Exception as error:
            await report_error(interaction, error)

class SendModal(BoundModal):

    def __init__(self, service, interaction, channel_id, mode='text', allow_mentions=False):
        super().__init__(service, interaction, title='发送机器人消息')
        self.channel_id = channel_id
        self.mode = mode
        self.allow_mentions = allow_mentions
        self.inputs = embed_inputs() if mode == 'embed' else {'content': discord.ui.TextInput(label='正文', style=discord.TextStyle.paragraph, required=True, max_length=2000)}
        for item in self.inputs.values():
            self.add_item(item)

    async def on_submit(self, interaction):
        try:
            check_owner(interaction, self.owner_id, self.guild_id)
            fields = {key: item.value for key, item in self.inputs.items()}
            if self.mode == 'embed':
                await self.service.send(interaction, self.channel_id, embeds=[build_embed(fields)], allow_mentions=self.allow_mentions)
            else:
                await self.service.send(interaction, self.channel_id, content=fields['content'], allow_mentions=self.allow_mentions)
        except Exception as error:
            await report_error(interaction, error)

class EditorView(discord.ui.View):

    def __init__(self, service, interaction, reference, message):
        super().__init__(timeout=300)
        self.service = service
        self.reference = reference
        self.owner_id = interaction.user.id
        self.guild_id = interaction.guild_id
        options = [discord.SelectOption(label='编辑正文', value='content')]
        options += [discord.SelectOption(label=f'编辑嵌入 {n + 1}', value=f'embed:{n}') for n in range(len(rich_embeds(message)))]
        self.selector = discord.ui.Select(placeholder='选择要编辑的部分', options=options)
        self.selector.callback = self.selected
        self.add_item(self.selector)
        button = discord.ui.Button(label='取消', style=discord.ButtonStyle.secondary)
        button.callback = self.cancel
        self.add_item(button)

    async def selected(self, interaction):
        await self.choose(interaction, self.selector.values[0])

    async def choose(self, interaction, choice):
        try:
            check_owner(interaction, self.owner_id, self.guild_id)
            retry = lambda next_interaction: self.choose(next_interaction, choice)
            async with preparation_deadline(interaction, retry) as deadline:
                async with self.service.interaction_scope(interaction):
                    message = await self.service.resolve_message(
                        interaction, self.reference, editable=True
                    )
                    def factory(current, current_message):
                        if choice == 'content':
                            return ContentModal(self.service, current, self.reference, current_message)
                        else:
                            index = int(choice.split(':')[1])
                            if index >= len(rich_embeds(current_message)):
                                raise BotMessageError('消息嵌入已变化，请重新打开编辑器。')
                            return EmbedModal(self.service, current, self.reference, current_message, index)
                    await deliver_editor(self.service, interaction, deadline, message, factory)
        except Exception as error:
            await report_error(interaction, error)

    async def cancel(self, interaction):
        try:
            check_owner(interaction, self.owner_id, self.guild_id)
            await acknowledge_early(interaction, thinking=False)
            async with self.service.interaction_scope(interaction):
                if interaction.response.is_done():
                    await interaction.edit_original_response(content='已取消。', view=None)
                else:
                    await interaction.response.edit_message(content='已取消。', view=None)
                self.stop()
        except Exception as error:
            await report_error(interaction, error)

async def open_editor(service, interaction, reference):
    try:
        retry = lambda next_interaction: open_editor(service, next_interaction, reference)
        async with preparation_deadline(interaction, retry) as deadline:
            async with service.interaction_scope(interaction):
                message = await service.resolve_message(interaction, reference, editable=True)
                reference = message.jump_url
                def factory(current, current_message):
                    if not rich_embeds(current_message):
                        return ContentModal(service, current, reference, current_message)
                    elif len(rich_embeds(current_message)) == 1 and not current_message.content:
                        return EmbedModal(service, current, reference, current_message)
                    else:
                        return EditorView(service, current, reference, current_message)
                await deliver_editor(service, interaction, deadline, message, factory)
    except Exception as error:
        await report_error(interaction, error)

