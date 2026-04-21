from __future__ import annotations

import base64
import html as html_escape
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

import discord


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _is_image_filename(name: str) -> bool:
    name = (name or "").lower()
    return any(name.endswith(ext) for ext in IMAGE_EXTS)


def is_image_attachment(att: discord.Attachment) -> bool:
    ct = (att.content_type or "").lower()
    if ct.startswith("image/"):
        return True
    return _is_image_filename(att.filename)


def sanitize_filename(name: str) -> str:
    name = (name or "file").strip()
    name = re.sub(r"[^a-zA-Z0-9._-]+", "_", name)
    name = name.strip("._")
    return name or "file"


def fmt_dt(dt: datetime | None) -> str:
    if not dt:
        return ""
    # 参照截图风格：2026/03/22 13:06（Windows 环境不支持 %-m）
    return dt.strftime("%Y/%m/%d %H:%M")


_URL_RE = re.compile(r'(https?://[^\s<>"]+)')


def render_discord_markdown(text: str) -> str:
    """轻量渲染：更像 DiscordChatExporter 的导出效果。

    重点：
    - 先 HTML escape
    - 支持 ``` 代码块、`inline code`
    - 支持粗体/斜体/删除线/下划线（简单正则，非完整 markdown 解析）
    - 自动链接
    - 换行 -> <br>

    注：这是“足够好”的渲染，不追求 100% 兼容 Discord markdown。
    """

    if not text:
        return ""

    # 先处理 code block
    raw = text
    chunks: list[str] = []
    parts = raw.split("```")
    for i, part in enumerate(parts):
        if i % 2 == 1:
            # code block
            code_html = html_escape.escape(part)
            chunks.append(f"<pre class='codeblock'><code>{code_html}</code></pre>")
        else:
            s = html_escape.escape(part)

            # inline code
            s = re.sub(r"`([^`]+)`", lambda m: f"<code class='inline'>{m.group(1)}</code>", s)
            # bold / underline / italic / strike (very naive)
            s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
            s = re.sub(r"__([^_]+)__", r"<u>\1</u>", s)
            s = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", s)
            s = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", s)

            # quote lines
            lines = s.split("\n")
            q_lines: list[str] = []
            for ln in lines:
                if ln.startswith("&gt; "):
                    q_lines.append(f"<blockquote>{ln[5:]}</blockquote>")
                else:
                    q_lines.append(ln)
            s = "\n".join(q_lines)

            # auto-link
            s = _URL_RE.sub(r"<a href='\1' target='_blank' rel='noreferrer'>\1</a>", s)

            s = s.replace("\n", "<br>")
            chunks.append(s)

    return "".join(chunks)


@dataclass
class ArchiveBuildResult:
    mode: str  # 'html' | 'zip'
    filename: str
    data: bytes
    warnings: list[str]


def _build_html(
    *,
    header_html: str,
    message_blocks: Iterable[str],
) -> str:
    css = """
    :root { --bg: #313338; --panel: #2b2d31; --text: #dbdee1; --muted: #949ba4; --link: #00a8fc; }
    body { margin:0; background: var(--bg); color: var(--text); font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; }
    a { color: var(--link); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .wrap { max-width: 1100px; margin: 0 auto; padding: 24px; }
    .header { background: var(--panel); border-radius: 10px; padding: 18px 20px; margin-bottom: 18px; }
    .header h1 { margin: 0 0 10px 0; font-size: 18px; }
    .header .meta { color: var(--muted); font-size: 13px; line-height: 1.5; white-space: pre-wrap; }

    .msg { display: flex; gap: 14px; padding: 10px 8px; border-radius: 8px; }
    .msg:hover { background: rgba(255,255,255,0.03); }
    .avatar { width: 40px; height: 40px; border-radius: 50%; flex: 0 0 auto; background: #1f2328; object-fit: cover; }
    .content { flex: 1 1 auto; min-width: 0; }
    .line1 { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
    .author { color: #f2f3f5; font-weight: 700; }
    .ts { color: var(--muted); font-size: 12px; }
    .text { margin-top: 3px; font-size: 14px; line-height: 1.35; word-wrap: break-word; }

    .attachments { margin-top: 6px; display: flex; flex-direction: column; gap: 8px; }
    .img { max-width: 600px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.08); }
    .file { color: var(--muted); font-size: 13px; }

    .embeds { margin-top: 8px; display: flex; flex-direction: column; gap: 8px; }
    .embed { max-width: 520px; background: rgba(0,0,0,0.22); border-left: 4px solid rgba(255,255,255,0.12); border-radius: 6px; padding: 10px 12px; }
    .embed-title { font-weight: 700; margin-bottom: 6px; }
    .embed-desc { font-size: 13px; line-height: 1.35; }
    .embed-fields { margin-top: 8px; display: flex; flex-direction: column; gap: 6px; }
    .embed-field .name { color: var(--muted); font-size: 12px; font-weight: 700; }
    .embed-field .value { font-size: 13px; line-height: 1.35; }
    .embed-image { margin-top: 8px; }
    .embed-image img { max-width: 100%; border-radius: 6px; border: 1px solid rgba(255,255,255,0.08); }
    .embed-footer { margin-top: 8px; color: var(--muted); font-size: 12px; }

    code.inline { background: rgba(0,0,0,0.35); padding: 2px 4px; border-radius: 4px; }
    pre.codeblock { background: rgba(0,0,0,0.35); padding: 10px; border-radius: 8px; overflow-x: auto; }
    blockquote { margin: 8px 0; padding: 8px 10px; border-left: 4px solid rgba(255,255,255,0.15); background: rgba(0,0,0,0.20); border-radius: 6px; }
    """

    body = "\n".join(message_blocks)
    return f"""<!doctype html>
<html lang='zh-CN'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>案件归档</title>
  <style>{css}</style>
</head>
<body>
  <div class='wrap'>
    <div class='header'>
      {header_html}
    </div>
    <div class='messages'>
      {body}
    </div>
  </div>
</body>
</html>"""


async def build_archive(
    *,
    channel: discord.TextChannel,
    header_lines: list[str],
    guild_filesize_limit: int,
) -> ArchiveBuildResult:
    """导出频道为 DCE 风格 HTML（优先单文件，超限则 ZIP）。

    仅保证：
    - Discord 图片附件离线可看（在 size 允许时）
    - 外链仅记录 URL
    - 非图片附件仅记录 URL
    """

    warnings: list[str] = []

    messages: list[discord.Message] = []
    async for m in channel.history(limit=None, oldest_first=True):
        messages.append(m)

    # 下载图片附件（Discord attachment），用于离线保存
    images: list[tuple[str, str, bytes]] = []  # (key, mime, data)

    def guess_mime(filename: str, content_type: str | None) -> str:
        if content_type and content_type.startswith("image/"):
            return content_type
        fn = filename.lower()
        if fn.endswith(".png"):
            return "image/png"
        if fn.endswith(".jpg") or fn.endswith(".jpeg"):
            return "image/jpeg"
        if fn.endswith(".gif"):
            return "image/gif"
        if fn.endswith(".webp"):
            return "image/webp"
        return "application/octet-stream"

    for m in messages:
        for att in m.attachments:
            if not is_image_attachment(att):
                continue
            try:
                data = await att.read()
            except Exception:
                warnings.append(f"图片下载失败：{att.url}")
                continue
            key = f"{m.id}_{sanitize_filename(att.filename)}"
            images.append((key, guess_mime(att.filename, att.content_type), data))

    # -------- 构造消息块（两种模式共用：inline / assets） --------

    def render_embed_html(e: discord.Embed) -> str:
        color_value: int | None = None
        try:
            if e.color:
                color_value = int(e.color.value)
        except Exception:
            color_value = None

        border = f"#{color_value:06x}" if isinstance(color_value, int) else "rgba(255,255,255,0.12)"

        title = (getattr(e, "title", None) or "").strip()
        url = (getattr(e, "url", None) or "").strip()
        title_html = html_escape.escape(title)
        if url and title_html:
            safe_url = html_escape.escape(url, quote=True)
            title_html = f"<a href='{safe_url}' target='_blank' rel='noreferrer'>{title_html}</a>"

        desc = (getattr(e, "description", None) or "").strip()
        desc_html = render_discord_markdown(desc)

        field_blocks: list[str] = []
        for f in getattr(e, "fields", []) or []:
            n = html_escape.escape(str(getattr(f, "name", "")))
            v = render_discord_markdown(str(getattr(f, "value", "")))
            if not n and not v:
                continue
            field_blocks.append(
                f"<div class='embed-field'><div class='name'>{n}</div><div class='value'>{v}</div></div>"
            )
        fields_html = "" if not field_blocks else f"<div class='embed-fields'>{''.join(field_blocks)}</div>"

        image_url = ""
        thumb_url = ""
        try:
            image_url = (e.image.url or "") if getattr(e, "image", None) else ""
        except Exception:
            image_url = ""
        try:
            thumb_url = (e.thumbnail.url or "") if getattr(e, "thumbnail", None) else ""
        except Exception:
            thumb_url = ""

        media_url = image_url or thumb_url
        image_html = ""
        if media_url:
            safe_media_url = html_escape.escape(str(media_url), quote=True)
            image_html = f"<div class='embed-image'><img src='{safe_media_url}' /></div>"

        footer_parts: list[str] = []
        try:
            author_name = (e.author.name or "") if getattr(e, "author", None) else ""
            if author_name:
                footer_parts.append(str(author_name))
        except Exception:
            pass
        try:
            footer_text = (e.footer.text or "") if getattr(e, "footer", None) else ""
            if footer_text:
                footer_parts.append(str(footer_text))
        except Exception:
            pass
        try:
            if getattr(e, "timestamp", None):
                footer_parts.append(fmt_dt(e.timestamp))
        except Exception:
            pass

        footer_html = ""
        if footer_parts:
            footer_html = f"<div class='embed-footer'>{html_escape.escape('｜'.join(footer_parts))}</div>"

        parts: list[str] = []
        if title_html:
            parts.append(f"<div class='embed-title'>{title_html}</div>")
        if desc_html:
            parts.append(f"<div class='embed-desc'>{desc_html}</div>")
        if fields_html:
            parts.append(fields_html)
        if image_html:
            parts.append(image_html)
        if footer_html:
            parts.append(footer_html)

        inner = "".join(parts) or "<div class='embed-desc'>(空 Embed)</div>"
        return f"<div class='embed' style='border-left-color: {border};'>{inner}</div>"

    def build_message_blocks(*, image_src: dict[str, str]) -> list[str]:
        blocks: list[str] = []
        for m in messages:
            author = m.author
            name = html_escape.escape(getattr(author, "display_name", str(author)))
            avatar = getattr(getattr(author, "display_avatar", None), "url", "")
            avatar_html = f"<img class='avatar' src='{avatar}' />" if avatar else "<div class='avatar'></div>"

            ts = m.created_at.strftime("%Y/%m/%d %H:%M")
            text = m.content or m.system_content or ""
            content_html = render_discord_markdown(text)

            embed_lines: list[str] = []
            for e in m.embeds:
                try:
                    embed_lines.append(render_embed_html(e))
                except Exception:
                    continue
            embeds_html = "" if not embed_lines else "<div class='embeds'>" + "".join(embed_lines) + "</div>"

            attach_lines: list[str] = []
            # 图片附件
            for att in m.attachments:
                if is_image_attachment(att):
                    key = f"{m.id}_{sanitize_filename(att.filename)}"
                    src = image_src.get(key) or html_escape.escape(att.url)
                    attach_lines.append(f"<div class='att'><img class='img' src='{src}' /></div>")
                else:
                    # 非图片仅记录 URL
                    url = html_escape.escape(att.url)
                    fn = html_escape.escape(att.filename)
                    attach_lines.append(f"<div class='file'>📎 <a href='{url}' target='_blank' rel='noreferrer'>{fn}</a></div>")

            attachments_html = "" if not attach_lines else "<div class='attachments'>" + "".join(attach_lines) + "</div>"

            blocks.append(
                """
<div class='msg'>
  {avatar_html}
  <div class='content'>
    <div class='line1'><span class='author'>{name}</span><span class='ts'>{ts}</span></div>
    <div class='text'>{content_html}</div>
    {embeds_html}
    {attachments_html}
  </div>
</div>
""".format(
                    avatar_html=avatar_html,
                    name=name,
                    ts=ts,
                    content_html=content_html,
                    embeds_html=embeds_html,
                    attachments_html=attachments_html,
                )
            )
        return blocks

    header_html = "<h1>📌 案件归档</h1>" + "<div class='meta'>" + html_escape.escape("\n".join(header_lines)) + "</div>"

    # 优先：单文件 HTML（base64 内嵌图片）
    image_src_inline: dict[str, str] = {}
    for key, mime, data in images:
        b64 = base64.b64encode(data).decode("ascii")
        image_src_inline[key] = f"data:{mime};base64,{b64}"

    html_inline = _build_html(header_html=header_html, message_blocks=build_message_blocks(image_src=image_src_inline))
    html_inline_bytes = html_inline.encode("utf-8")

    limit = int(guild_filesize_limit * 0.95)
    if len(html_inline_bytes) <= limit:
        return ArchiveBuildResult(mode="html", filename=f"archive-{channel.id}.html", data=html_inline_bytes, warnings=warnings)

    # 超限：ZIP（index.html + assets）
    image_src_assets: dict[str, str] = {key: f"assets/{key}" for key, _, _ in images}
    html_assets = _build_html(header_html=header_html, message_blocks=build_message_blocks(image_src=image_src_assets))

    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.html", html_assets.encode("utf-8"))
        for key, _, data in images:
            z.writestr(f"assets/{key}", data)

    zip_bytes = mem.getvalue()
    if len(zip_bytes) <= limit:
        return ArchiveBuildResult(mode="zip", filename=f"archive-{channel.id}.zip", data=zip_bytes, warnings=warnings)

    # 仍超限：降级（不内嵌、不打包图片，仅保留 URL）
    warnings.append("归档文件过大：已降级为仅记录图片 URL（未离线保存）。")
    html_urls = _build_html(header_html=header_html, message_blocks=build_message_blocks(image_src={}))
    return ArchiveBuildResult(mode="html", filename=f"archive-{channel.id}-urls.html", data=html_urls.encode("utf-8"), warnings=warnings)
