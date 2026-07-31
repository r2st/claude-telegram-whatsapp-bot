"""The landing page is the whole funnel, so it gets tests like anything else.

`website/` is a hand-written, no-build static site. That is a virtue — there is
nothing to compile and nothing to break in a toolchain — but it also means a
dead asset reference, a broken in-page anchor, a JSON-LD block with a trailing
comma, or a sitemap listing a page that no longer exists all ship silently.
Every one of those costs real traffic: a search engine that cannot parse the
structured data just drops the rich result, and nobody gets an error.

These tests assert only what is checkable without a browser: that the files
referenced exist, that the metadata search engines and social cards read is
present and parseable, and that the page and the package agree about what
Telechat is called and how you install it.

Run:
    pytest tests/test_website.py -v
"""
from __future__ import annotations

import json
import re
import tomllib
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SITE = REPO_ROOT / "website"
INDEX = SITE / "index.html"

CANONICAL = "https://telechat.fyi/"

#: Void elements never have a closing tag, so the nesting check must skip them.
VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

#: Tags inside <svg> use foreign-content rules (`<path/>`, `<rect/>`) that the
#: stdlib parser reports inconsistently. The nesting check ignores that subtree.
FOREIGN = {"svg"}


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def blocks(html) -> list[dict]:
    """Every JSON-LD block on the page, parsed. Malformed JSON fails here."""
    raw = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    assert raw, "no JSON-LD on the page"
    return [json.loads(chunk) for chunk in raw]


@pytest.fixture(scope="module")
def refs(html) -> list[str]:
    """Every href/src on the page, in document order."""
    return re.findall(r'(?:href|src)="([^"]+)"', html)


@pytest.fixture(scope="module")
def project_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


# ─── structure ───────────────────────────────────────────────────────────────


class _Nesting(HTMLParser):
    """Minimal well-formedness check: every opened tag gets closed, in order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self._foreign_depth = 0

    def handle_starttag(self, tag, _attrs):
        if self._foreign_depth:
            if tag in FOREIGN:
                self._foreign_depth += 1
            return
        if tag in FOREIGN:
            self._foreign_depth = 1
            return
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if self._foreign_depth:
            if tag in FOREIGN:
                self._foreign_depth -= 1
            return
        if tag in VOID:
            return
        if not self.stack:
            self.errors.append(f"</{tag}> with nothing open")
        elif self.stack[-1] != tag:
            self.errors.append(f"</{tag}> closes <{self.stack[-1]}>")
            if tag in self.stack:                      # resync so one slip is one error
                while self.stack and self.stack.pop() != tag:
                    pass
        else:
            self.stack.pop()


class TestSiteFiles:
    """Everything the deployed site needs is committed."""

    @pytest.mark.parametrize("name", [
        "index.html",
        "favicon.svg",
        "qrcode.svg",
        "og.png",
        "og.svg",
        "robots.txt",
        "sitemap.xml",
    ])
    def test_present(self, name):
        assert (SITE / name).is_file(), f"website/{name} is missing"

    def test_og_image_is_a_real_png(self):
        """Social cards silently fail on an SVG, so the shipped card must be raster."""
        assert (SITE / "og.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


class TestMarkup:
    def test_tags_are_balanced(self, html):
        parser = _Nesting()
        parser.feed(html)
        assert not parser.errors, "unbalanced markup: " + "; ".join(parser.errors[:5])
        assert not parser.stack, f"never closed: {parser.stack}"

    def test_exactly_one_h1(self, html):
        assert len(re.findall(r"<h1\b", html)) == 1

    def test_declares_language_and_charset(self, html):
        assert re.search(r'<html[^>]*\blang="en"', html)
        assert re.search(r'<meta\s+charset="UTF-8">', html, re.I)

    def test_is_responsive(self, html):
        assert 'name="viewport"' in html
        assert "width=device-width" in html
        # A landing page with a fixed desktop layout is unusable on the device
        # most people will open it on.
        assert "@media(max-width:" in html.replace("@media (max-width:", "@media(max-width:")

    def test_every_image_has_alt_text(self, html):
        for tag in re.findall(r"<img\b[^>]*>", html):
            assert 'alt="' in tag, f"<img> without alt: {tag}"

    def test_page_is_self_contained(self, html):
        """No CDN, no webfont, no analytics — the site is three files on a CDN edge."""
        for tag in re.findall(r"<script\b[^>]*>", html):
            assert " src=" not in tag, f"external script: {tag}"
        for tag in re.findall(r"<link\b[^>]*>", html):
            if 'rel="stylesheet"' in tag:
                pytest.fail(f"external stylesheet: {tag}")

    def test_new_tab_links_are_safe(self, html):
        for tag in re.findall(r"<a\b[^>]*>", html):
            if 'target="_blank"' in tag:
                assert "noopener" in tag, f"target=_blank without rel=noopener: {tag}"


# ─── SEO / discoverability ───────────────────────────────────────────────────


def _meta(html: str, key: str, attr: str = "name") -> str | None:
    m = re.search(
        rf'<meta\s+{attr}="{re.escape(key)}"\s+content="([^"]*)"',
        html,
    )
    return m.group(1) if m else None


class TestSEO:
    def test_title_fits_a_search_result(self, html):
        title = re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip()
        assert "Telechat" in title
        # Google renders roughly 60 characters; past that the tail is elided.
        assert len(title) <= 70, f"title is {len(title)} chars: {title!r}"

    def test_description_is_present_and_sized(self, html):
        desc = _meta(html, "description")
        assert desc, "no meta description"
        assert 80 <= len(desc) <= 165, f"description is {len(desc)} chars"

    def test_canonical_url(self, html):
        m = re.search(r'<link\s+rel="canonical"\s+href="([^"]+)"', html)
        assert m and m.group(1) == CANONICAL

    def test_indexable(self, html):
        robots = _meta(html, "robots")
        assert robots is None or "noindex" not in robots

    @pytest.mark.parametrize("prop", [
        "og:type", "og:site_name", "og:url", "og:title",
        "og:description", "og:image", "og:image:alt",
    ])
    def test_open_graph(self, html, prop):
        assert _meta(html, prop, attr="property"), f"missing {prop}"

    @pytest.mark.parametrize("name", [
        "twitter:card", "twitter:title", "twitter:description", "twitter:image",
    ])
    def test_twitter_card(self, html, name):
        assert _meta(html, name), f"missing {name}"

    def test_social_images_are_absolute_and_committed(self, html):
        for url in (_meta(html, "og:image", attr="property"), _meta(html, "twitter:image")):
            assert url.startswith("https://"), f"social image must be absolute: {url}"
            assert (SITE / url[len(CANONICAL):]).is_file(), f"{url} is not in website/"

    def test_theme_colour_for_both_schemes(self, html):
        tags = re.findall(r'<meta\s+name="theme-color"[^>]*>', html)
        assert any("prefers-color-scheme: dark" in t for t in tags)
        assert any("prefers-color-scheme: light" in t for t in tags)


class TestStructuredData:
    def test_every_block_is_typed(self, blocks):
        for block in blocks:
            assert block["@context"] == "https://schema.org"
            assert block["@type"]

    def test_describes_the_software(self, blocks, project_version):
        app = next(b for b in blocks if b["@type"] == "SoftwareApplication")
        assert app["name"] == "Telechat"
        assert app["url"] == CANONICAL
        assert app["isAccessibleForFree"] is True
        # A stale version in the structured data is what search engines quote.
        assert app["softwareVersion"] == project_version

    def test_faq_block_matches_the_visible_faq(self, blocks, html):
        faq = next(b for b in blocks if b["@type"] == "FAQPage")
        marked_up = {q["name"].rstrip("?") for q in faq["mainEntity"]}
        for q in faq["mainEntity"]:
            assert q["acceptedAnswer"]["text"].strip(), f"empty answer for {q['name']}"

        visible = {
            re.sub(r"<[^>]+>", "", s).strip().rstrip("?")
            for s in re.findall(r"<summary>(.*?)</summary>", html, re.S)
        }
        # Structured data that promises a question the page does not answer is
        # exactly what earns a manual action.
        assert marked_up <= visible, f"in JSON-LD but not on the page: {marked_up - visible}"


class TestCrawlerFiles:
    def test_robots_allows_crawling_and_points_at_the_sitemap(self):
        robots = (SITE / "robots.txt").read_text(encoding="utf-8")
        assert re.search(r"^User-agent:\s*\*", robots, re.M)
        assert not re.search(r"^Disallow:\s*/\s*$", robots, re.M)
        assert f"Sitemap: {CANONICAL}sitemap.xml" in robots

    def test_sitemap_is_valid_and_only_lists_pages_that_exist(self):
        root = ET.fromstring((SITE / "sitemap.xml").read_bytes())
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        assert root.tag == f"{{{ns['sm']}}}urlset"

        locs = [el.text.strip() for el in root.findall(".//sm:loc", ns)]
        assert CANONICAL in locs, "the homepage is not in the sitemap"
        for loc in locs:
            assert loc.startswith(CANONICAL), f"foreign URL in sitemap: {loc}"
            path = loc[len(CANONICAL):] or "index.html"
            target = SITE / path
            if target.is_dir():
                target = target / "index.html"
            assert target.is_file(), f"sitemap lists {loc}, which has no file"


# ─── links ───────────────────────────────────────────────────────────────────


class TestLinks:
    def test_local_assets_exist(self, refs):
        for ref in refs:
            if ref.startswith(("http://", "https://", "mailto:", "#", "/")):
                continue
            assert (SITE / ref).is_file(), f"referenced but missing: website/{ref}"

    def test_in_page_anchors_resolve(self, html, refs):
        ids = set(re.findall(r'\bid="([^"]+)"', html))
        for ref in refs:
            if ref.startswith("#") and len(ref) > 1:
                assert ref[1:] in ids, f"{ref} points at no element"

    def test_repository_links_use_https(self, refs):
        for ref in refs:
            assert not ref.startswith("http://"), f"insecure link: {ref}"

    def test_github_links_point_at_this_repository(self, refs):
        github = [r for r in refs if "github.com" in r]
        assert github, "the page never links to the source"
        for ref in github:
            assert ref.startswith("https://github.com/telechatai/telechat"), ref

    def test_documentation_links_resolve_in_this_repo(self, refs):
        """A /blob/main/… link is a promise that the file is there."""
        prefix = "https://github.com/telechatai/telechat/blob/main/"
        for ref in refs:
            if ref.startswith(prefix):
                path = ref[len(prefix):].split("#", 1)[0]
                assert (REPO_ROOT / path).is_file(), f"links to {path}, which does not exist"


# ─── the page agrees with the package ────────────────────────────────────────


class TestPageMatchesReality:
    def test_install_command_matches_the_published_names(self, html):
        npm_name = json.loads((REPO_ROOT / "npm" / "package.json").read_text())["name"]
        assert f"npm install -g {npm_name}" in html
        assert "pip install telechatai" in html

    def test_copy_buttons_copy_what_they_display(self, html):
        """The button's data-copy and the visible <code> must not drift apart."""
        for block in re.findall(r'<div class="cmd">(.*?)</div>', html, re.S):
            shown = re.search(r"<code[^>]*>(.*?)</code>", block, re.S)
            copied = re.search(r'data-copy="([^"]+)"', block)
            assert shown and copied, f"a .cmd block is missing its code or button: {block[:80]}"
            text = re.sub(r"<[^>]+>", "", shown.group(1)).replace("$ ", "", 1).strip()
            assert text == copied.group(1).strip(), (
                f"copy button pastes {copied.group(1)!r} but shows {text!r}"
            )

    def test_advertised_version_matches_the_package(self, html, project_version):
        for claimed in re.findall(r"\bv(\d+\.\d+\.\d+)\b", html):
            assert claimed == project_version, (
                f"page advertises v{claimed}, pyproject says {project_version}"
            )

    def test_platform_claims_have_adapters_behind_them(self, html):
        pkg = REPO_ROOT / "telechat_pkg"
        for platform, module in [
            ("Telegram", "telegram_bot.py"),
            ("WhatsApp", "whatsapp_bot.py"),
            ("Slack", "slack_bot.py"),
            ("web chat", "web_chat.py"),
        ]:
            assert platform in html
            assert (pkg / module).is_file(), f"page claims {platform} but {module} is gone"
