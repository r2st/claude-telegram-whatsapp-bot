"""
MarkdownV2 formatter for Telegram.

Converts Claude's markdown output to Telegram MarkdownV2 format,
which supports code blocks with language hints, blockquotes,
spoilers, underline, strikethrough, and proper escaping.
"""
from __future__ import annotations

import re


# Characters that must be escaped in MarkdownV2 outside of code blocks
_ESCAPE_CHARS = r'_*[]()~`>#+=|{}.!-'
_ESCAPE_RE = re.compile(r'([' + re.escape(_ESCAPE_CHARS) + r'])')

# Patterns for code blocks (``` ... ```) and inline code (` ... `)
_CODE_BLOCK_RE = re.compile(r'```(\w*)\n(.*?)```', re.DOTALL)
_INLINE_CODE_RE = re.compile(r'`([^`\n]+)`')

# Pattern for markdown links [text](url)
_LINK_RE = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')

# Bold: **text** or __text__
_BOLD_RE = re.compile(r'\*\*(.+?)\*\*', re.DOTALL)
# Italic: *text* (but not **)
_ITALIC_RE = re.compile(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)')
# Strikethrough: ~~text~~
_STRIKE_RE = re.compile(r'~~(.+?)~~')
# Blockquote lines starting with >
_BLOCKQUOTE_RE = re.compile(r'^> ?(.*)$', re.MULTILINE)
# Heading lines starting with # through ######. Claude writes #### and deeper
# often enough that stopping at three left literal hashes in the output.
_HEADING_RE = re.compile(r'^#{1,6}\s+(.+)$', re.MULTILINE)
# Leading/trailing emphasis inside a heading ("## **Important**"), which would
# otherwise end up nested inside the bold the heading itself becomes.
_HEADING_EMPHASIS_RE = re.compile(r'^[*_]+|[*_]+$')
# Stand-in for the '>' of a blockquote while the rest of the line is escaped.
_QUOTE_MARK = "\x00QUOTE\x00"
# Horizontal rule
_HR_RE = re.compile(r'^-{3,}$', re.MULTILINE)
# Bullet lists
_BULLET_RE = re.compile(r'^(\s*)[-*]\s+', re.MULTILINE)
# Numbered lists
_NUM_LIST_RE = re.compile(r'^(\s*)\d+\.\s+', re.MULTILINE)
# Bare URLs not in markdown link format
_URL_RE = re.compile(r'(?<!\()(https?://[^\s\)\]>]+)')
_MD_LINK_SPANS_RE = re.compile(r'\[([^\]]+)\]\((https?://[^\)]+)\)')


def escape_md2(text: str) -> str:
    """Escape special characters for MarkdownV2."""
    return _ESCAPE_RE.sub(r'\\\1', text)


def to_markdown_v2(text: str) -> str:
    """Convert standard markdown (from Claude) to Telegram MarkdownV2.

    Handles code blocks, inline code, bold, italic, strikethrough,
    blockquotes, links, lists, and headings while properly escaping
    all other special characters.
    """
    if not text:
        return text

    # Extract code blocks first to protect them from escaping
    code_blocks: list[tuple[str, str]] = []

    def _save_code_block(m: re.Match) -> str:
        lang = m.group(1) or ""
        code = m.group(2)
        placeholder = f"\x00CODEBLOCK{len(code_blocks)}\x00"
        code_blocks.append((lang, code))
        return placeholder

    result = _CODE_BLOCK_RE.sub(_save_code_block, text)

    # Extract inline code
    inline_codes: list[str] = []

    def _save_inline_code(m: re.Match) -> str:
        placeholder = f"\x00INLINE{len(inline_codes)}\x00"
        inline_codes.append(m.group(1))
        return placeholder

    result = _INLINE_CODE_RE.sub(_save_inline_code, result)

    # Extract links
    links: list[tuple[str, str]] = []

    def _save_link(m: re.Match) -> str:
        placeholder = f"\x00LINK{len(links)}\x00"
        links.append((m.group(1), m.group(2)))
        return placeholder

    result = _LINK_RE.sub(_save_link, result)

    # Bold, declared before headings because a heading *becomes* bold text.
    bolds: list[str] = []

    def _save_bold_text(raw: str) -> str:
        placeholder = f"\x00BOLD{len(bolds)}\x00"
        bolds.append(raw)
        return placeholder

    def _save_bold(m: re.Match) -> str:
        return _save_bold_text(m.group(1))

    # Convert headings to bold — through the same placeholder as **bold**.
    # Emitting a bare *text* here instead left it to be matched by the italic
    # rule further down, so every heading Claude wrote came out italic.
    result = _HEADING_RE.sub(
        lambda m: _save_bold_text(_HEADING_EMPHASIS_RE.sub("", m.group(1).strip())),
        result,
    )

    # Convert horizontal rules
    result = _HR_RE.sub("—" * 20, result)

    # Convert bold **text** → *text*
    result = _BOLD_RE.sub(_save_bold, result)

    # Convert strikethrough ~~text~~ → ~text~
    strikes: list[str] = []

    def _save_strike(m: re.Match) -> str:
        placeholder = f"\x00STRIKE{len(strikes)}\x00"
        strikes.append(m.group(1))
        return placeholder

    result = _STRIKE_RE.sub(_save_strike, result)

    # Convert italic *text* → _text_
    italics: list[str] = []

    def _save_italic(m: re.Match) -> str:
        placeholder = f"\x00ITALIC{len(italics)}\x00"
        italics.append(m.group(1))
        return placeholder

    result = _ITALIC_RE.sub(_save_italic, result)

    # Convert blockquotes. The '>' goes behind a marker: escape_md2 turns a
    # bare one into '\>', which MarkdownV2 renders as a literal greater-than
    # sign, so quoted text never actually became a blockquote.
    result = _BLOCKQUOTE_RE.sub(lambda m: f"{_QUOTE_MARK}{m.group(1)}", result)

    # Convert bullet points
    result = _BULLET_RE.sub(r'\1• ', result)

    # Now escape all remaining special chars
    result = escape_md2(result)

    # ...and put the blockquote markers back, now that escaping is done.
    result = result.replace(_QUOTE_MARK, ">")

    # Restore bold
    for i, bold_text in enumerate(bolds):
        escaped_bold = escape_md2(bold_text)
        result = result.replace(f"\x00BOLD{i}\x00", f"*{escaped_bold}*")

    # Restore strikethrough
    for i, strike_text in enumerate(strikes):
        escaped_strike = escape_md2(strike_text)
        result = result.replace(f"\x00STRIKE{i}\x00", f"~{escaped_strike}~")

    # Restore italic
    for i, italic_text in enumerate(italics):
        escaped_italic = escape_md2(italic_text)
        result = result.replace(f"\x00ITALIC{i}\x00", f"_{escaped_italic}_")

    # Restore links
    for i, (link_text, url) in enumerate(links):
        escaped_text = escape_md2(link_text)
        # URLs in links: only escape ) and \
        escaped_url = url.replace("\\", "\\\\").replace(")", "\\)")
        result = result.replace(f"\x00LINK{i}\x00", f"[{escaped_text}]({escaped_url})")

    # Restore inline code (content not escaped in MarkdownV2)
    for i, code_text in enumerate(inline_codes):
        result = result.replace(f"\x00INLINE{i}\x00", f"`{code_text}`")

    # Restore code blocks (content not escaped in MarkdownV2)
    for i, (lang, code) in enumerate(code_blocks):
        result = result.replace(f"\x00CODEBLOCK{i}\x00", f"```{lang}\n{code}```")

    return result


def try_markdownv2(text: str) -> tuple[str, str]:
    """Try to convert text to MarkdownV2, return (formatted_text, parse_mode).

    Falls back to plain text if conversion fails.
    Returns (text, parse_mode) tuple.
    """
    try:
        converted = to_markdown_v2(text)
        return converted, "MarkdownV2"
    except Exception:
        return text, ""


def protect_urls(text: str) -> str:
    """Wrap bare URLs for legacy Markdown mode (backward compat)."""
    existing_link_spans = set()
    for m in _MD_LINK_SPANS_RE.finditer(text):
        existing_link_spans.add((m.start(), m.end()))

    def _replace_bare_url(match):
        url = match.group(1)
        start = match.start(1)
        for ls, le in existing_link_spans:
            if ls <= start < le:
                return match.group(0)
        trailing = ""
        while url and url[-1] in ("*", "_", "`", "~"):
            trailing = url[-1] + trailing
            url = url[:-1]
        return f"[{url}]({url}){trailing}"

    return _URL_RE.sub(_replace_bare_url, text)
