"""
Web fetch — extract readable content from URLs using Jina Reader or raw HTML stripping.

Provides cleaner content extraction than the basic link_understanding module,
using Jina Reader API for best results or falling back to HTML stripping.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass

import aiohttp

from . import net_guard

log = logging.getLogger(__name__)

_BLOCKED_HOSTS = net_guard.BLOCKED_HOSTS


def _is_blocked_url(url: str) -> bool:
    """Synchronous reject on scheme, known names and IP literals.

    See `net_guard`: this cannot see through a hostname, so `fetch_readable`
    follows it with the resolving check before making a request.
    """
    return net_guard.is_blocked_url(url)

JINA_API_KEY = os.getenv("JINA_API_KEY", "")
WEB_FETCH_ENABLED = os.getenv("WEB_FETCH_ENABLED", "false").lower() in ("1", "true", "yes")
FETCH_TIMEOUT = int(os.getenv("WEB_FETCH_TIMEOUT", "15"))
MAX_CONTENT_LENGTH = int(os.getenv("WEB_FETCH_MAX_KB", "100")) * 1024

_session: aiohttp.ClientSession | None = None


def _get_session() -> "aiohttp.ClientSession":
    global _session
    if _session is None or _session.closed:
        import aiohttp
        _session = aiohttp.ClientSession()
    return _session


@dataclass
class FetchResult:
    url: str
    title: str
    content: str
    word_count: int
    error: str | None = None


def is_available() -> bool:
    return WEB_FETCH_ENABLED


async def fetch_readable(url: str) -> FetchResult:
    """Fetch a URL and extract readable content.

    Uses Jina Reader API if JINA_API_KEY is set, otherwise falls back
    to raw HTML fetch + tag stripping.
    """
    # Resolves the hostname too: blocking only IP literals let any name
    # pointing at 127.0.0.1 or a metadata endpoint straight through.
    blocked = await net_guard.check_url_allowed(url)
    if blocked:
        return FetchResult(url=url, title="", content="", word_count=0, error=blocked)
    if JINA_API_KEY:
        return await _fetch_jina(url)
    return await _fetch_raw(url)


async def _fetch_jina(url: str) -> FetchResult:
    """Use Jina Reader API (r.jina.ai) for high-quality content extraction."""
    reader_url = f"https://r.jina.ai/{url}"
    headers = {
        "Accept": "text/plain",
        "X-Return-Format": "text",
    }
    if JINA_API_KEY:
        headers["Authorization"] = f"Bearer {JINA_API_KEY}"

    try:
        session = _get_session()
        async with session.get(reader_url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return FetchResult(url=url, title="", content="", word_count=0,
                                       error=f"Jina Reader error {resp.status}: {body[:200]}")
                text = await resp.text()
                if len(text) > MAX_CONTENT_LENGTH:
                    text = text[:MAX_CONTENT_LENGTH] + "\n…[truncated]"
                # Jina often puts the title on the first line
                lines = text.strip().split("\n", 1)
                title = lines[0].strip() if lines else ""
                content = lines[1].strip() if len(lines) > 1 else text.strip()
                return FetchResult(
                    url=url, title=title, content=content,
                    word_count=len(content.split()),
                )
    except asyncio.TimeoutError:
        return FetchResult(url=url, title="", content="", word_count=0, error="Fetch timed out")
    except Exception as e:
        return FetchResult(url=url, title="", content="", word_count=0, error=str(e)[:200])


async def _follow_redirects(session, url: str, headers: dict) -> str:
    """Walk redirects by hand, checking every hop, and return the final URL.

    Raises `PermissionError` if a hop lands somewhere the bot must not fetch —
    caught by the caller's `except Exception` and reported as the fetch error.
    """
    from urllib.parse import urljoin

    current = url
    for _hop in range(net_guard.MAX_REDIRECTS):
        blocked = await net_guard.check_url_allowed(current)
        if blocked:
            raise PermissionError(blocked)
        async with session.get(
            current, headers=headers,
            timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT),
            allow_redirects=False,
        ) as resp:
            if resp.status not in (301, 302, 303, 307, 308):
                return current
            location = resp.headers.get("Location")
            if not location:
                return current
            current = urljoin(current, location)

    raise PermissionError("Too many redirects")


async def _fetch_raw(url: str) -> FetchResult:
    """Basic HTML fetch with tag stripping."""
    headers = {
        "User-Agent": "TeleChatBot/1.0 (web-fetch)",
        "Accept": "text/html,application/xhtml+xml,text/plain",
    }

    try:
        session = _get_session()
        # Redirects are followed by hand so each hop is re-checked; see
        # net_guard. Following them automatically vetted only the first URL.
        url = await _follow_redirects(session, url, headers)
        async with session.get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT),
                               allow_redirects=False) as resp:
                if resp.status != 200:
                    return FetchResult(url=url, title="", content="", word_count=0,
                                       error=f"HTTP {resp.status}")
                raw = await resp.text()

        # Extract title
        title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.IGNORECASE | re.DOTALL)
        title = title_match.group(1).strip() if title_match else ""

        # Strip scripts, styles, and tags
        text = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<nav[^>]*>.*?</nav>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<footer[^>]*>.*?</footer>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<header[^>]*>.*?</header>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"&lt;", "<", text)
        text = re.sub(r"&gt;", ">", text)
        text = re.sub(r"\s+", " ", text).strip()

        if len(text) > MAX_CONTENT_LENGTH:
            text = text[:MAX_CONTENT_LENGTH] + " …[truncated]"

        return FetchResult(
            url=url, title=title, content=text,
            word_count=len(text.split()),
        )
    except asyncio.TimeoutError:
        return FetchResult(url=url, title="", content="", word_count=0, error="Fetch timed out")
    except Exception as e:
        return FetchResult(url=url, title="", content="", word_count=0, error=str(e)[:200])
