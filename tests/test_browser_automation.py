"""Behavior tests for browser_automation.BrowserAgent (ticket 0016).

Playwright is an OPTIONAL dependency and we never launch a real browser here.
The whole Playwright surface (async_playwright, chromium, browser, context,
page) is replaced with async fakes injected via a fake ``playwright.async_api``
module in ``sys.modules``. This exercises the agent's lifecycle, the
screenshot / text-extraction / form-fill / script / page-info flows, the
URL-blocking guard, and every error path — without any real network or browser.
"""

import sys
import types

import pytest

from telechat_pkg import browser_automation as ba
from telechat_pkg.browser_automation import (
    BrowserAgent,
    BrowserResult,
    PageInfo,
    _is_blocked_url,
    get_browser_agent,
)


# ─── Fake Playwright stack ────────────────────────────────────────────────────


class FakePage:
    def __init__(self, *, fail_on=None, title="Fake Title", text="page text",
                 links=None, eval_result=None):
        self._fail_on = fail_on or set()
        self._title = title
        self._text = text
        self._links = links if links is not None else [{"text": "L", "href": "http://x/"}]
        self._eval_result = eval_result
        self.url = "http://example.com/final"
        self.closed = False
        self.filled = []
        self.clicked = []

    async def _maybe_fail(self, name):
        if name in self._fail_on:
            raise RuntimeError(f"fail:{name}")

    async def goto(self, url, timeout=None):
        await self._maybe_fail("goto")
        self.url = url + "/final"

    async def wait_for_load_state(self, state, timeout=None):
        await self._maybe_fail("wait")

    async def screenshot(self, path=None, full_page=False):
        await self._maybe_fail("screenshot")
        self.last_screenshot = (path, full_page)

    async def title(self):
        return self._title

    async def text_content(self, selector):
        await self._maybe_fail("text_content")
        return self._text

    async def eval_on_selector_all(self, selector, script):
        return self._links

    async def fill(self, selector, value, timeout=None):
        if selector in self._fail_on:
            raise RuntimeError(f"cant fill {selector}")
        self.filled.append((selector, value))

    async def click(self, selector, timeout=None):
        await self._maybe_fail("click")
        self.clicked.append(selector)

    async def evaluate(self, script):
        await self._maybe_fail("evaluate")
        return self._eval_result

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, page):
        self._page = page
        self.closed = False

    async def new_page(self):
        return self._page

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, context):
        self._context = context
        self.closed = False

    async def new_context(self, **kwargs):
        self.context_kwargs = kwargs
        return self._context

    async def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser):
        self._browser = browser
        self.launch_kwargs = None

    async def launch(self, headless=True):
        self.launch_kwargs = {"headless": headless}
        return self._browser


class FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium
        self.stopped = False

    async def stop(self):
        self.stopped = True


class FakeAsyncPlaywrightCM:
    """Mimics async_playwright() whose .start() returns a playwright object."""

    def __init__(self, playwright):
        self._pw = playwright

    async def start(self):
        return self._pw


def _install_fake_playwright(monkeypatch, page=None, raise_import=False):
    """Inject a fake ``playwright.async_api`` module. Returns the FakePage."""
    page = page or FakePage()
    context = FakeContext(page)
    browser = FakeBrowser(context)
    chromium = FakeChromium(browser)
    pw = FakePlaywright(chromium)

    fake_async_api = types.ModuleType("playwright.async_api")

    if raise_import:
        # Simulate playwright not installed: importing async_playwright fails.
        def _boom():
            raise ImportError("No module named 'playwright'")
        fake_async_api.async_playwright = _boom
    else:
        fake_async_api.async_playwright = lambda: FakeAsyncPlaywrightCM(pw)

    fake_pkg = types.ModuleType("playwright")
    fake_pkg.async_api = fake_async_api
    monkeypatch.setitem(sys.modules, "playwright", fake_pkg)
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_async_api)
    return page


@pytest.fixture
def screenshot_dir(tmp_path, monkeypatch):
    d = str(tmp_path / "shots")
    monkeypatch.setattr(ba, "SCREENSHOT_DIR", d)
    return d


# ══════════════════════════════════════════════════════════════════════════════
# 1. URL blocking guard
# ══════════════════════════════════════════════════════════════════════════════


class TestIsBlockedUrl:
    def test_localhost_blocked(self):
        assert _is_blocked_url("http://localhost/") is True

    def test_zero_host_blocked(self):
        assert _is_blocked_url("http://0.0.0.0/") is True

    def test_non_http_scheme_blocked(self):
        assert _is_blocked_url("ftp://example.com/") is True
        assert _is_blocked_url("file:///etc/passwd") is True

    def test_private_ip_blocked(self):
        assert _is_blocked_url("http://192.168.1.1/") is True
        assert _is_blocked_url("http://10.0.0.5/") is True

    def test_loopback_ip_blocked(self):
        assert _is_blocked_url("http://127.0.0.1/") is True

    def test_public_hostname_allowed(self):
        # Hostname (not an IP) raises ValueError in ip_address -> returns False.
        assert _is_blocked_url("https://example.com/") is False

    def test_public_ip_allowed(self):
        assert _is_blocked_url("http://93.184.216.34/") is False


# ══════════════════════════════════════════════════════════════════════════════
# 2. Lifecycle: start / stop / ensure
# ══════════════════════════════════════════════════════════════════════════════


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_initializes(self, monkeypatch, screenshot_dir):
        _install_fake_playwright(monkeypatch)
        agent = BrowserAgent()
        await agent.start()
        assert agent._started is True
        assert agent._browser is not None
        assert agent._context is not None

    @pytest.mark.asyncio
    async def test_start_idempotent(self, monkeypatch, screenshot_dir):
        _install_fake_playwright(monkeypatch)
        agent = BrowserAgent()
        await agent.start()
        first_browser = agent._browser
        await agent.start()  # second call returns early
        assert agent._browser is first_browser

    @pytest.mark.asyncio
    async def test_start_import_error_propagates(self, monkeypatch, screenshot_dir):
        _install_fake_playwright(monkeypatch, raise_import=True)
        agent = BrowserAgent()
        with pytest.raises(ImportError):
            await agent.start()
        assert agent._started is False

    @pytest.mark.asyncio
    async def test_start_generic_error_propagates(self, monkeypatch, screenshot_dir):
        # async_playwright().start() raises a non-Import error.
        fake_async_api = types.ModuleType("playwright.async_api")

        class BoomCM:
            async def start(self):
                raise RuntimeError("launch failed")

        fake_async_api.async_playwright = lambda: BoomCM()
        fake_pkg = types.ModuleType("playwright")
        fake_pkg.async_api = fake_async_api
        monkeypatch.setitem(sys.modules, "playwright", fake_pkg)
        monkeypatch.setitem(sys.modules, "playwright.async_api", fake_async_api)

        agent = BrowserAgent()
        with pytest.raises(RuntimeError):
            await agent.start()

    @pytest.mark.asyncio
    async def test_stop_closes_everything(self, monkeypatch, screenshot_dir):
        _install_fake_playwright(monkeypatch)
        agent = BrowserAgent()
        await agent.start()
        ctx, browser, pw = agent._context, agent._browser, agent._playwright
        await agent.stop()
        assert ctx.closed is True
        assert browser.closed is True
        assert pw.stopped is True
        assert agent._started is False

    @pytest.mark.asyncio
    async def test_stop_when_never_started(self):
        # All handles None -> stop is a no-op and does not raise.
        agent = BrowserAgent()
        await agent.stop()
        assert agent._started is False

    @pytest.mark.asyncio
    async def test_ensure_started_triggers_start(self, monkeypatch, screenshot_dir):
        _install_fake_playwright(monkeypatch)
        agent = BrowserAgent()
        await agent._ensure_started()
        assert agent._started is True


# ══════════════════════════════════════════════════════════════════════════════
# 3. screenshot()
# ══════════════════════════════════════════════════════════════════════════════


class TestScreenshot:
    @pytest.mark.asyncio
    async def test_screenshot_success(self, monkeypatch, screenshot_dir):
        page = _install_fake_playwright(monkeypatch)
        agent = BrowserAgent()
        result = await agent.screenshot("https://example.com")
        assert result.success is True
        assert result.title == "Fake Title"
        assert result.screenshot_path.endswith(".png")
        assert result.duration >= 0
        assert page.closed is True

    @pytest.mark.asyncio
    async def test_screenshot_full_page_flag(self, monkeypatch, screenshot_dir):
        page = _install_fake_playwright(monkeypatch)
        agent = BrowserAgent()
        await agent.screenshot("https://example.com", full_page=True)
        assert page.last_screenshot[1] is True

    @pytest.mark.asyncio
    async def test_screenshot_blocked_url(self, monkeypatch, screenshot_dir):
        _install_fake_playwright(monkeypatch)
        agent = BrowserAgent()
        result = await agent.screenshot("http://localhost/")
        assert result.success is False
        assert "Blocked" in result.error

    @pytest.mark.asyncio
    async def test_screenshot_navigation_error(self, monkeypatch, screenshot_dir):
        _install_fake_playwright(monkeypatch, page=FakePage(fail_on={"goto"}))
        agent = BrowserAgent()
        result = await agent.screenshot("https://example.com")
        assert result.success is False
        assert "fail:goto" in result.error


# ══════════════════════════════════════════════════════════════════════════════
# 4. extract_text()
# ══════════════════════════════════════════════════════════════════════════════


class TestExtractText:
    @pytest.mark.asyncio
    async def test_extract_success(self, monkeypatch, screenshot_dir):
        page = _install_fake_playwright(monkeypatch, page=FakePage(text="hello world"))
        agent = BrowserAgent()
        result = await agent.extract_text("https://example.com", selector="article")
        assert result.success is True
        assert isinstance(result.data, PageInfo)
        assert result.data.text_content == "hello world"
        assert result.data.links

    @pytest.mark.asyncio
    async def test_extract_truncates_to_5000(self, monkeypatch, screenshot_dir):
        _install_fake_playwright(monkeypatch, page=FakePage(text="z" * 6000))
        agent = BrowserAgent()
        result = await agent.extract_text("https://example.com")
        assert len(result.data.text_content) == 5000

    @pytest.mark.asyncio
    async def test_extract_none_text_content(self, monkeypatch, screenshot_dir):
        # page.text_content returns None -> coerced to "".
        page = FakePage()
        page._text = None
        _install_fake_playwright(monkeypatch, page=page)
        agent = BrowserAgent()
        result = await agent.extract_text("https://example.com")
        assert result.success is True
        assert result.data.text_content == ""

    @pytest.mark.asyncio
    async def test_extract_blocked_url(self, monkeypatch, screenshot_dir):
        _install_fake_playwright(monkeypatch)
        agent = BrowserAgent()
        result = await agent.extract_text("http://10.0.0.1/")
        assert result.success is False
        assert "Blocked" in result.error

    @pytest.mark.asyncio
    async def test_extract_error_path(self, monkeypatch, screenshot_dir):
        _install_fake_playwright(monkeypatch, page=FakePage(fail_on={"text_content"}))
        agent = BrowserAgent()
        result = await agent.extract_text("https://example.com")
        assert result.success is False


# ══════════════════════════════════════════════════════════════════════════════
# 5. fill_form()
# ══════════════════════════════════════════════════════════════════════════════


class TestFillForm:
    @pytest.mark.asyncio
    async def test_fill_success(self, monkeypatch, screenshot_dir):
        page = _install_fake_playwright(monkeypatch)
        agent = BrowserAgent()
        result = await agent.fill_form(
            "https://example.com/form", {"#name": "John", "#email": "j@x.com"}
        )
        assert result.success is True
        assert set(result.data["filled"]) == {"#name", "#email"}
        assert result.data["total_fields"] == 2
        assert result.screenshot_path

    @pytest.mark.asyncio
    async def test_fill_partial_failure_logged(self, monkeypatch, screenshot_dir):
        # One selector fails to fill; the rest succeed and it's not fatal.
        _install_fake_playwright(monkeypatch, page=FakePage(fail_on={"#bad"}))
        agent = BrowserAgent()
        result = await agent.fill_form(
            "https://example.com/form", {"#good": "x", "#bad": "y"}
        )
        assert result.success is True
        assert result.data["filled"] == ["#good"]
        assert result.data["total_fields"] == 2

    @pytest.mark.asyncio
    async def test_fill_with_submit(self, monkeypatch, screenshot_dir):
        page = _install_fake_playwright(monkeypatch)
        agent = BrowserAgent()
        result = await agent.fill_form(
            "https://example.com/form", {"#q": "v"}, submit=True
        )
        assert result.success is True
        assert page.clicked  # submit button clicked

    @pytest.mark.asyncio
    async def test_fill_submit_failure_swallowed(self, monkeypatch, screenshot_dir):
        # Submit click raises -> caught and ignored; result still success.
        _install_fake_playwright(monkeypatch, page=FakePage(fail_on={"click"}))
        agent = BrowserAgent()
        result = await agent.fill_form(
            "https://example.com/form", {"#q": "v"}, submit=True
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_fill_blocked_url(self, monkeypatch, screenshot_dir):
        _install_fake_playwright(monkeypatch)
        agent = BrowserAgent()
        result = await agent.fill_form("http://127.0.0.1/", {"#a": "b"})
        assert result.success is False
        assert "Blocked" in result.error

    @pytest.mark.asyncio
    async def test_fill_navigation_error(self, monkeypatch, screenshot_dir):
        _install_fake_playwright(monkeypatch, page=FakePage(fail_on={"goto"}))
        agent = BrowserAgent()
        result = await agent.fill_form("https://example.com/form", {"#a": "b"})
        assert result.success is False


# ══════════════════════════════════════════════════════════════════════════════
# 6. run_script()
# ══════════════════════════════════════════════════════════════════════════════


class TestRunScript:
    @pytest.mark.asyncio
    async def test_run_script_success(self, monkeypatch, screenshot_dir):
        _install_fake_playwright(monkeypatch, page=FakePage(eval_result={"k": 1}))
        agent = BrowserAgent()
        result = await agent.run_script("https://example.com", "() => 1")
        assert result.success is True
        assert result.data == {"k": 1}

    @pytest.mark.asyncio
    async def test_run_script_error(self, monkeypatch, screenshot_dir):
        _install_fake_playwright(monkeypatch, page=FakePage(fail_on={"evaluate"}))
        agent = BrowserAgent()
        result = await agent.run_script("https://example.com", "boom()")
        assert result.success is False
        assert "fail:evaluate" in result.error


# ══════════════════════════════════════════════════════════════════════════════
# 7. get_page_info()
# ══════════════════════════════════════════════════════════════════════════════


class TestGetPageInfo:
    @pytest.mark.asyncio
    async def test_page_info_success(self, monkeypatch, screenshot_dir):
        _install_fake_playwright(
            monkeypatch, page=FakePage(text="body text", eval_result={"og:title": "T"})
        )
        agent = BrowserAgent()
        result = await agent.get_page_info("https://example.com")
        assert result.success is True
        assert result.data["title"] == "Fake Title"
        assert result.data["text_preview"] == "body text"
        assert result.data["meta"] == {"og:title": "T"}

    @pytest.mark.asyncio
    async def test_page_info_none_text(self, monkeypatch, screenshot_dir):
        page = FakePage(eval_result={})
        page._text = None
        _install_fake_playwright(monkeypatch, page=page)
        agent = BrowserAgent()
        result = await agent.get_page_info("https://example.com")
        assert result.success is True
        assert result.data["text_preview"] == ""

    @pytest.mark.asyncio
    async def test_page_info_error(self, monkeypatch, screenshot_dir):
        _install_fake_playwright(monkeypatch, page=FakePage(fail_on={"evaluate"}))
        agent = BrowserAgent()
        result = await agent.get_page_info("https://example.com")
        assert result.success is False


# ══════════════════════════════════════════════════════════════════════════════
# 8. Dataclasses + singleton
# ══════════════════════════════════════════════════════════════════════════════


class TestDataclassesAndSingleton:
    def test_browser_result_defaults(self):
        r = BrowserResult(success=True)
        assert r.data is None
        assert r.screenshot_path == ""
        assert r.duration == 0.0

    def test_page_info_defaults(self):
        p = PageInfo(url="http://x/", title="T")
        assert p.text_content == ""
        assert p.links == []

    def test_get_browser_agent_singleton(self, monkeypatch):
        monkeypatch.setattr(ba, "_browser_agent", None)
        a1 = get_browser_agent()
        a2 = get_browser_agent()
        assert a1 is a2
        assert isinstance(a1, BrowserAgent)
