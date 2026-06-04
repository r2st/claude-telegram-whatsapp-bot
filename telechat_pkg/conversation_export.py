"""
Conversation export — export chat history in multiple formats.

Supports plain text, Markdown, HTML (self-contained with chat bubbles),
and JSON export formats.

"""
from __future__ import annotations

import html
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class ExportResult:
    content: str
    format: str
    message_count: int
    filename: str


def _ts_to_str(ts: float) -> str:
    """Convert Unix timestamp to readable string."""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, OverflowError):
        # OverflowError: a timestamp too large for the platform C time_t
        # (e.g. 10**30) raises this on macOS rather than ValueError.
        return "unknown"


def export_text(
    messages: list[dict],
    *,
    title: str = "Conversation",
    include_timestamps: bool = True,
) -> ExportResult:
    """Export conversation as plain text."""
    lines = [f"=== {title} ===", ""]
    for m in messages:
        role = m.get("role", "user").capitalize()
        content = m.get("content", "")
        ts = m.get("timestamp", 0)
        if include_timestamps and ts:
            lines.append(f"[{_ts_to_str(ts)}] {role}:")
        else:
            lines.append(f"{role}:")
        lines.append(content)
        lines.append("")

    lines.append(f"--- Exported {len(messages)} messages ---")
    text = "\n".join(lines)
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    return ExportResult(
        content=text,
        format="text",
        message_count=len(messages),
        filename=f"chat_{ts_str}.txt",
    )


def export_markdown(
    messages: list[dict],
    *,
    title: str = "Conversation",
    include_timestamps: bool = True,
) -> ExportResult:
    """Export conversation as Markdown."""
    lines = [f"# {title}", ""]
    for m in messages:
        role = m.get("role", "user").capitalize()
        content = m.get("content", "")
        ts = m.get("timestamp", 0)
        if include_timestamps and ts:
            lines.append(f"### {role} ({_ts_to_str(ts)})")
        else:
            lines.append(f"### {role}")
        lines.append("")
        lines.append(content)
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.append(f"*Exported {len(messages)} messages on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
    text = "\n".join(lines)
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    return ExportResult(
        content=text,
        format="markdown",
        message_count=len(messages),
        filename=f"chat_{ts_str}.md",
    )


_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    max-width: 800px;
    margin: 0 auto;
    padding: 20px;
    background: #f5f5f5;
}}
h1 {{ text-align: center; color: #333; }}
.message {{
    margin: 12px 0;
    padding: 12px 16px;
    border-radius: 12px;
    max-width: 80%;
    line-height: 1.5;
    word-wrap: break-word;
}}
.user {{
    background: #007AFF;
    color: white;
    margin-left: auto;
    border-bottom-right-radius: 4px;
}}
.assistant {{
    background: white;
    color: #333;
    border: 1px solid #e0e0e0;
    border-bottom-left-radius: 4px;
}}
.system {{
    background: #fff3cd;
    color: #856404;
    margin: 0 auto;
    text-align: center;
    font-size: 0.9em;
    max-width: 60%;
}}
.timestamp {{
    font-size: 0.75em;
    opacity: 0.7;
    margin-top: 4px;
}}
.container {{
    display: flex;
    flex-direction: column;
}}
.meta {{
    text-align: center;
    color: #888;
    font-size: 0.85em;
    margin-top: 20px;
    padding-top: 10px;
    border-top: 1px solid #ddd;
}}
pre {{
    background: rgba(0,0,0,0.1);
    padding: 8px;
    border-radius: 6px;
    overflow-x: auto;
    font-size: 0.9em;
}}
</style>
</head>
<body>
<h1>{title}</h1>
<div class="container">
{messages_html}
</div>
<div class="meta">Exported {count} messages on {export_time}</div>
</body>
</html>
"""


def export_html(
    messages: list[dict],
    *,
    title: str = "Conversation",
    include_timestamps: bool = True,
) -> ExportResult:
    """Export conversation as self-contained HTML with chat bubbles."""
    msg_parts = []
    for m in messages:
        role = m.get("role", "user")
        content = html.escape(m.get("content", ""))
        # Preserve newlines and basic formatting
        content = content.replace("\n", "<br>")
        ts = m.get("timestamp", 0)
        ts_html = ""
        if include_timestamps and ts:
            ts_html = f'<div class="timestamp">{html.escape(_ts_to_str(ts))}</div>'
        msg_parts.append(
            f'<div class="message {html.escape(role)}">{content}{ts_html}</div>'
        )

    messages_html = "\n".join(msg_parts)
    page = _HTML_TEMPLATE.format(
        title=html.escape(title),
        messages_html=messages_html,
        count=len(messages),
        export_time=html.escape(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    return ExportResult(
        content=page,
        format="html",
        message_count=len(messages),
        filename=f"chat_{ts_str}.html",
    )


def export_json(
    messages: list[dict],
    *,
    title: str = "Conversation",
) -> ExportResult:
    """Export conversation as JSON."""
    data = {
        "title": title,
        "exported_at": datetime.now().isoformat(),
        "message_count": len(messages),
        "messages": [
            {
                "role": m.get("role", "user"),
                "content": m.get("content", ""),
                "timestamp": m.get("timestamp"),
            }
            for m in messages
        ],
    }
    text = json.dumps(data, indent=2, ensure_ascii=False)
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    return ExportResult(
        content=text,
        format="json",
        message_count=len(messages),
        filename=f"chat_{ts_str}.json",
    )


EXPORTERS = {
    "text": export_text,
    "txt": export_text,
    "markdown": export_markdown,
    "md": export_markdown,
    "html": export_html,
    "json": export_json,
}


def export_conversation(
    messages: list[dict],
    fmt: str = "text",
    **kwargs,
) -> ExportResult:
    """Export conversation in the specified format.

    Args:
        messages: List of {role, content, timestamp?} dicts.
        fmt: Export format — text, markdown, html, or json.
        **kwargs: Passed to the format-specific exporter.

    Returns:
        ExportResult with content, format, and suggested filename.
    """
    exporter = EXPORTERS.get(fmt.lower())
    if not exporter:
        supported = ", ".join(sorted(set(EXPORTERS.keys())))
        raise ValueError(f"Unknown format {fmt!r}. Supported: {supported}")
    return exporter(messages, **kwargs)
