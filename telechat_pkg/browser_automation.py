"""
Browser Automation (Feature 10) — Playwright-based web actions for telechat.

Inspired by the Claude Browser Automation Agent and Playwright MCP connector.
Allows users to request web actions like screenshots, form filling, data
extraction, and page navigation via chat commands.

Usage:
    from telechat_pkg.browser_automation import BrowserAgent
    agent = BrowserAgent()
    await agent.start()
    result = await agent.screenshot("https://example.com")
    result = await agent.extract_text("https://example.com", selector="article")
    result = await agent.fill_form("https://example.com/form", {"name": "John", "email": "j@x.com"})
    await agent.stop()
"""
from __future__ import annotations

import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_BLOCKED_HOSTS = {"localhost", "0.0.0.0"}


def _is_blocked_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        if parsed.scheme not in ("http", "https"):
            return True
        if hostname in _BLOCKED_HOSTS:
            return True
        addr = ip_address(hostname)
        return addr.is_private or addr.is_loopback or addr.is_reserved
    except ValueError:
        return False

BROWSER_ENABLED = os.getenv("BROWSER_ENABLED", "false").lower() in ("1", "true", "yes")
BROWSER_HEADLESS = os.getenv("BROWSER_HEADLESS", "true").lower() in ("1", "true", "yes")
BROWSER_TIMEOUT = int(os.getenv("BROWSER_TIMEOUT", "30000"))  # ms
SCREENSHOT_DIR = os.getenv("SCREENSHOT_DIR", str(Path(tempfile.gettempdir()) / "telechat_screenshots"))


@dataclass
class BrowserResult:
    success: bool
    data: Any = None
    screenshot_path: str = ""
    url: str = ""
    title: str = ""
    error: str = ""
    duration: float = 0.0


@dataclass
class PageInfo:
    url: str
    title: str
    text_content: str = ""
    links: list[dict] = field(default_factory=list)


class BrowserAgent:
    """Playwright-based browser automation agent."""

    def __init__(self):
        self._browser = None
        self._context = None
        self._playwright = None
        self._started = False

    async def start(self):
        """Start the browser instance."""
        if self._started:
            return
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=BROWSER_HEADLESS)
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent="TeleChat/1.6 Browser Agent",
            )
            self._started = True
            Path(SCREENSHOT_DIR).mkdir(parents=True, exist_ok=True)
            log.info("Browser agent started (headless=%s)", BROWSER_HEADLESS)
        except ImportError:
            log.error("playwright not installed. Run: pip install playwright && playwright install chromium")
            raise
        except Exception as e:
            log.error("Failed to start browser: %s", e)
            raise

    async def stop(self):
        """Stop the browser instance."""
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._started = False
        log.info("Browser agent stopped")

    async def _ensure_started(self):
        if not self._started:
            await self.start()

    async def screenshot(self, url: str, *, full_page: bool = False) -> BrowserResult:
        """Navigate to URL and take a screenshot."""
        if _is_blocked_url(url):
            return BrowserResult(success=False, error="Blocked: private/internal address", url=url)
        await self._ensure_started()
        start = time.time()
        try:
            page = await self._context.new_page()
            await page.goto(url, timeout=BROWSER_TIMEOUT)
            await page.wait_for_load_state("networkidle", timeout=BROWSER_TIMEOUT)

            filename = f"screenshot_{int(time.time())}.png"
            filepath = str(Path(SCREENSHOT_DIR) / filename)
            await page.screenshot(path=filepath, full_page=full_page)

            title = await page.title()
            await page.close()

            return BrowserResult(
                success=True,
                screenshot_path=filepath,
                url=url,
                title=title,
                duration=time.time() - start,
            )
        except Exception as e:
            return BrowserResult(success=False, error=str(e), url=url, duration=time.time() - start)

    async def extract_text(self, url: str, *, selector: str = "body") -> BrowserResult:
        """Navigate to URL and extract text content."""
        if _is_blocked_url(url):
            return BrowserResult(success=False, error="Blocked: private/internal address", url=url)
        await self._ensure_started()
        start = time.time()
        try:
            page = await self._context.new_page()
            await page.goto(url, timeout=BROWSER_TIMEOUT)
            await page.wait_for_load_state("networkidle", timeout=BROWSER_TIMEOUT)

            content = await page.text_content(selector) or ""
            title = await page.title()
            current_url = page.url

            # Extract links
            links = await page.eval_on_selector_all(
                "a[href]",
                """elements => elements.slice(0, 20).map(e => ({
                    text: e.textContent.trim().slice(0, 100),
                    href: e.href
                }))"""
            )

            await page.close()

            return BrowserResult(
                success=True,
                data=PageInfo(url=current_url, title=title, text_content=content[:5000], links=links),
                url=current_url,
                title=title,
                duration=time.time() - start,
            )
        except Exception as e:
            return BrowserResult(success=False, error=str(e), url=url, duration=time.time() - start)

    async def fill_form(self, url: str, fields: dict[str, str], *, submit: bool = False) -> BrowserResult:
        """Navigate to URL and fill form fields."""
        if _is_blocked_url(url):
            return BrowserResult(success=False, error="Blocked: private/internal address", url=url)
        await self._ensure_started()
        start = time.time()
        try:
            page = await self._context.new_page()
            await page.goto(url, timeout=BROWSER_TIMEOUT)
            await page.wait_for_load_state("networkidle", timeout=BROWSER_TIMEOUT)

            filled = []
            for selector, value in fields.items():
                try:
                    await page.fill(selector, value, timeout=5000)
                    filled.append(selector)
                except Exception as e:
                    log.warning("Failed to fill %s: %s", selector, e)

            if submit:
                try:
                    await page.click('button[type="submit"], input[type="submit"]', timeout=5000)
                    await page.wait_for_load_state("networkidle", timeout=BROWSER_TIMEOUT)
                except Exception:
                    # No submit button, or the navigation never settled. The
                    # fields were still filled, which is what the caller asked
                    # for — but "why didn't it submit?" needs an answer.
                    log.warning("form submit did not complete", exc_info=True)

            # Screenshot the result
            filename = f"form_{int(time.time())}.png"
            filepath = str(Path(SCREENSHOT_DIR) / filename)
            await page.screenshot(path=filepath)

            title = await page.title()
            await page.close()

            return BrowserResult(
                success=True,
                data={"filled": filled, "total_fields": len(fields)},
                screenshot_path=filepath,
                url=url,
                title=title,
                duration=time.time() - start,
            )
        except Exception as e:
            return BrowserResult(success=False, error=str(e), url=url, duration=time.time() - start)

    async def run_script(self, url: str, script: str) -> BrowserResult:
        """Navigate to URL and run JavaScript."""
        await self._ensure_started()
        start = time.time()
        try:
            page = await self._context.new_page()
            await page.goto(url, timeout=BROWSER_TIMEOUT)
            await page.wait_for_load_state("networkidle", timeout=BROWSER_TIMEOUT)

            result = await page.evaluate(script)
            title = await page.title()
            await page.close()

            return BrowserResult(
                success=True,
                data=result,
                url=url,
                title=title,
                duration=time.time() - start,
            )
        except Exception as e:
            return BrowserResult(success=False, error=str(e), url=url, duration=time.time() - start)

    async def get_page_info(self, url: str) -> BrowserResult:
        """Get comprehensive page information."""
        await self._ensure_started()
        start = time.time()
        try:
            page = await self._context.new_page()
            await page.goto(url, timeout=BROWSER_TIMEOUT)
            await page.wait_for_load_state("networkidle", timeout=BROWSER_TIMEOUT)

            title = await page.title()
            content = await page.text_content("body") or ""

            # Get meta info
            meta = await page.evaluate("""() => {
                const metas = {};
                document.querySelectorAll('meta[name], meta[property]').forEach(m => {
                    const key = m.getAttribute('name') || m.getAttribute('property');
                    metas[key] = m.getAttribute('content');
                });
                return metas;
            }""")

            await page.close()

            return BrowserResult(
                success=True,
                data={
                    "title": title,
                    "text_preview": content[:2000],
                    "meta": meta,
                    "url": url,
                },
                url=url,
                title=title,
                duration=time.time() - start,
            )
        except Exception as e:
            return BrowserResult(success=False, error=str(e), url=url, duration=time.time() - start)


# Singleton
_browser_agent: BrowserAgent | None = None


def get_browser_agent() -> BrowserAgent:
    global _browser_agent
    if _browser_agent is None:
        _browser_agent = BrowserAgent()
    return _browser_agent
