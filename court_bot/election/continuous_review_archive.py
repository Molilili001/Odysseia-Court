"""Self contained HTML audit export; attachments remain at their Discord URLs."""
from __future__ import annotations

import html
import json


def build_review_html(app: dict, review: dict, votes: list[dict], events: list[dict], messages: list) -> bytes:
    esc = lambda value: html.escape(str(value if value is not None else ""), quote=True)
    lines = ["<!doctype html><html lang='zh'><meta charset='utf-8'><title>常态审核归档</title>",
             "<style>body{font:16px sans-serif;max-width:960px;margin:2rem auto;line-height:1.6}pre{white-space:pre-wrap;background:#eee;padding:1rem}article{border-top:1px solid #ddd;padding:.8rem}img{max-width:500px}</style>",
             f"<h1>常态审核 #{app['id']}</h1>",
             f"<p>申请人：{esc(app['display_name'])}（{app['user_id']}）｜岗位：{esc(app['field_name'])}</p>",
             f"<p>原始申请：{esc(app['self_intro'])}</p>",
             f"<p>最终状态：{esc(app['status'])}｜审核结果：{esc(review.get('outcome'))}</p>",
             f"<p>开始：{esc(review.get('review_started_at'))}｜截止：{esc(review.get('review_deadline_at'))}｜完成：{esc(review.get('completed_at'))}</p>",
             f"<p>DM：{esc(review.get('dm_status'))}｜错误：{esc(review.get('dm_error'))}</p>",
             "<h2>配置快照</h2><pre>" + esc(json.dumps(review["snapshot"], ensure_ascii=False, indent=2)) + "</pre>",
             "<h2>最终有效票</h2>"]
    for vote in votes:
        lines.append(f"<article>{esc(vote['reviewer_name'])}（{vote['reviewer_id']}）：{esc(vote['choice'])}｜理由：{esc(vote['reason'])}</article>")
    lines.append("<h2>审计事件与状态时间线</h2>")
    for event in events:
        lines.append(f"<article>{esc(event['created_at'])}｜{esc(event.get('actor_id'))}｜{esc(event['event_type'])}｜{esc(event.get('old_choice'))} → {esc(event.get('new_choice'))}｜{esc(event.get('reason'))}｜{esc(event.get('detail_json'))}</article>")
    lines.append("<h2>Thread 中保留的消息</h2>")
    for message in messages:
        lines.append(f"<article><b>{esc(message.author)}（{message.author.id}）</b>｜{esc(message.created_at)}<br>{esc(message.content)}")
        for attachment in message.attachments:
            url = esc(attachment.url)
            lines.append(f"<p>附件：<a href='{url}'>{esc(attachment.filename)}</a></p>")
            if attachment.content_type and attachment.content_type.startswith("image/"):
                lines.append(f"<img src='{url}' alt='{esc(attachment.filename)}'>")
        lines.append("</article>")
    lines.append("</html>")
    return "\n".join(lines).encode("utf-8")
