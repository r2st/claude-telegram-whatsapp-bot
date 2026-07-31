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

# Every span parked while the surrounding text is escaped uses this one shape,
# so a single pass can put them all back. Restoring them used to be six loops
# doing `result.replace(placeholder, value)` once per span, and each of those
# rescans the entire message — O(spans x length). A reply is exactly the input
# that makes both factors grow together, so the cost climbed quadratically:
# 3.5 KB took 0.4 ms, 59 KB took 54 ms. One `sub` over the whole string is
# linear, and the nesting the loop order used to encode is handled by expanding
# each replacement before it is spliced in.
_PLACEHOLDER_RE = re.compile(r'\x00(BOLD|STRIKE|ITALIC|LINK|INLINE|CODEBLOCK)(\d+)\x00')


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

    # Restore every parked span in one pass. A span's own text can hold another
    # placeholder — a heading containing a link becomes bold wrapped around a
    # LINK marker — so each replacement is expanded before it is spliced in
    # rather than relying on a later loop to catch it. Code content is spliced
    # in raw and deliberately not expanded: it is the user's text, not ours.
    def _expand(fragment: str) -> str:
        return _PLACEHOLDER_RE.sub(_restore, fragment)

    def _restore(m: re.Match) -> str:
        kind = m.group(1)
        idx = int(m.group(2))
        table = {
            "BOLD": bolds, "STRIKE": strikes, "ITALIC": italics,
            "LINK": links, "INLINE": inline_codes, "CODEBLOCK": code_blocks,
        }[kind]
        # A literal NUL-delimited marker in the source text would otherwise
        # index off the end and drop the whole message to plain text.
        if idx >= len(table):
            return m.group(0)

        if kind == "BOLD":
            return f"*{_expand(escape_md2(bolds[idx]))}*"
        if kind == "STRIKE":
            return f"~{_expand(escape_md2(strikes[idx]))}~"
        if kind == "ITALIC":
            return f"_{_expand(escape_md2(italics[idx]))}_"
        if kind == "LINK":
            link_text, url = links[idx]
            # URLs in links: only escape ) and \
            escaped_url = url.replace("\\", "\\\\").replace(")", "\\)")
            return f"[{_expand(escape_md2(link_text))}]({escaped_url})"
        if kind == "INLINE":
            return f"`{inline_codes[idx]}`"
        lang, code = code_blocks[idx]
        return f"```{lang}\n{code}```"

    return _expand(result)


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
