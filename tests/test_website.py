"""The website is the whole funnel, so it gets tests like anything else.

`website/` is a hand-written, no-build static site. That is a virtue — there is
nothing to compile and nothing to break in a toolchain — but it also means a
dead asset reference, a broken in-page anchor, a JSON-LD block with a trailing
comma, a post missing from the sitemap, or an RSS item pointing at a file that
was renamed all ship silently. Every one of those costs real traffic: a search
engine that cannot parse the structured data just drops the rich result, and
nobody gets an error.

These tests assert only what is checkable without a browser: that referenced
files exist, that the metadata search engines and social cards read is present
and parseable, that the blog index, sitemap and feed agree with the posts on
disk, and that the site and the package agree about what Telechat is called and
how you install it.

Run:
    pytest tests/test_website.py -v
"""
from __future__ import annotations

import json
import re
import tomllib
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SITE = REPO_ROOT / "website"
INDEX = SITE / "index.html"
BLOG = SITE / "blog"

CANONICAL = "https://telechat.fyi/"

#: Every page on the site, as a path relative to `website/`.
PAGES = sorted(p.relative_to(SITE).as_posix() for p in SITE.rglob("*.html"))

#: Blog posts, i.e. every blog page that is not the index.
POSTS = sorted(p for p in PAGES if p.startswith("blog/") and p != "blog/index.html")

#: Void elements never have a closing tag, so the nesting check must skip them.
VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

#: Tags inside <svg> use foreign-content rules (`<path/>`, `<rect/>`) that the
#: stdlib parser reports inconsistently. The nesting check ignores that subtree.
FOREIGN = {"svg"}


def read(page: str) -> str:
    return (SITE / page).read_text(encoding="utf-8")


def refs_in(page: str) -> list[str]:
    """Every href/src on a page, in document order."""
    return re.findall(r'(?:href|src)="([^"]+)"', read(page))


def canonical_of(page: str) -> str:
    """The URL a page claims for itself."""
    m = re.search(r'<link\s+rel="canonical"\s+href="([^"]+)"', read(page))
    assert m, f"{page} has no canonical URL"
    return m.group(1)


def _meta(html: str, key: str, attr: str = "name") -> str | None:
    m = re.search(rf'<meta\s+{attr}="{re.escape(key)}"\s+content="([^"]*)"', html)
    return m.group(1) if m else None


@pytest.fixture(scope="module")
def html() -> str:
    return read("index.html")


@pytest.fixture(scope="module")
def blocks(html) -> list[dict]:
    """Every JSON-LD block on the landing page, parsed."""
    raw = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    assert raw, "no JSON-LD on the page"
    return [json.loads(chunk) for chunk in raw]


@pytest.fixture(scope="module")
def refs(html) -> list[str]:
    return re.findall(r'(?:href|src)="([^"]+)"', html)


@pytest.fixture(scope="module")
def channel() -> ET.Element:
    """The RSS <channel>, after asserting the envelope is RSS 2.0."""
    root = ET.fromstring((SITE / "feed.xml").read_bytes())
    assert root.tag == "rss" and root.get("version") == "2.0"
    return root.find("channel")


@pytest.fixture(scope="module")
def locs() -> list[str]:
    """Every <loc> in the sitemap, after asserting the namespace is right."""
    root = ET.fromstring((SITE / "sitemap.xml").read_bytes())
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    assert root.tag == f"{{{ns['sm']}}}urlset"
    return [el.text.strip() for el in root.findall(".//sm:loc", ns)]


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

    def handle_starttag(self, tag, attrs):
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
        "feed.xml",
        "blog/index.html",
        "blog/blog.css",
    ])
    def test_present(self, name):
        assert (SITE / name).is_file(), f"website/{name} is missing"

    def test_og_image_is_a_real_png(self):
        """Social cards silently fail on an SVG, so the shipped card must be raster."""
        assert (SITE / "og.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_there_are_posts(self):
        assert POSTS, "the blog has no posts"


@pytest.mark.parametrize("page", PAGES)
class TestEveryPage:
    """Checks that must hold for the landing page and every blog page alike."""

    def test_tags_are_balanced(self, page):
        parser = _Nesting()
        parser.feed(read(page))
        assert not parser.errors, f"{page}: " + "; ".join(parser.errors[:5])
        assert not parser.stack, f"{page} never closes: {parser.stack}"

    def test_exactly_one_h1(self, page):
        assert len(re.findall(r"<h1\b", read(page))) == 1, f"{page} needs exactly one <h1>"

    def test_declares_language_and_charset(self, page):
        html = read(page)
        assert re.search(r'<html[^>]*\blang="en"', html), page
        assert re.search(r'<meta\s+charset="UTF-8">', html, re.I), page

    def test_is_responsive(self, page):
        assert "width=device-width" in read(page), f"{page} has no responsive viewport"

    def test_every_image_has_alt_text(self, page):
        for tag in re.findall(r"<img\b[^>]*>", read(page)):
            assert 'alt="' in tag, f"{page}: <img> without alt: {tag}"

    def test_no_third_party_requests(self, page):
        """No CDN, no webfont, no analytics — every byte is served from our origin."""
        html = read(page)
        for tag in re.findall(r"<script\b[^>]*>", html):
            src = re.search(r'\ssrc="([^"]+)"', tag)
            assert not src, f"{page}: external script {src.group(1) if src else ''}"
        for tag in re.findall(r"<link\b[^>]*>", html):
            if 'rel="stylesheet"' not in tag:
                continue
            href = re.search(r'href="([^"]+)"', tag).group(1)
            assert not href.startswith(("http://", "https://", "//")), (
                f"{page}: stylesheet from another origin: {href}"
            )

    def test_new_tab_links_are_safe(self, page):
        for tag in re.findall(r"<a\b[^>]*>", read(page)):
            if 'target="_blank"' in tag:
                assert "noopener" in tag, f"{page}: target=_blank without rel=noopener"

    def test_local_assets_exist(self, page):
        base = (SITE / page).parent
        for ref in refs_in(page):
            if ref.startswith(("http://", "https://", "mailto:", "#", "/")):
                continue
            target = (base / ref.split("#", 1)[0]).resolve()
            if target.is_dir():
                target = target / "index.html"
            assert target.is_file(), f"{page} references {ref}, which does not exist"

    def test_in_page_anchors_resolve(self, page):
        html = read(page)
        ids = set(re.findall(r'\bid="([^"]+)"', html))
        for ref in refs_in(page):
            if ref.startswith("#") and len(ref) > 1:
                assert ref[1:] in ids, f"{page}: {ref} points at no element"

    def test_no_insecure_links(self, page):
        for ref in refs_in(page):
            assert not ref.startswith("http://"), f"{page}: insecure link {ref}"

    def test_declares_its_own_canonical_url(self, page):
        expected = CANONICAL + ("" if page == "index.html" else page)
        expected = expected.replace("blog/index.html", "blog/")
        assert canonical_of(page) == expected, f"{page} claims the wrong canonical URL"

    def test_is_indexable(self, page):
        robots = _meta(read(page), "robots")
        assert robots is None or "noindex" not in robots, f"{page} is noindex"

    def test_has_a_description(self, page):
        desc = _meta(read(page), "description")
        assert desc, f"{page} has no meta description"
        assert 80 <= len(desc) <= 165, f"{page}: description is {len(desc)} chars"

    def test_has_social_cards(self, page):
        html = read(page)
        for prop in ("og:type", "og:title", "og:description", "og:image", "og:url"):
            assert _meta(html, prop, attr="property"), f"{page} is missing {prop}"
        for name in ("twitter:card", "twitter:title", "twitter:description", "twitter:image"):
            assert _meta(html, name), f"{page} is missing {name}"

    def test_social_images_are_absolute_and_committed(self, page):
        html = read(page)
        for url in (_meta(html, "og:image", attr="property"), _meta(html, "twitter:image")):
            assert url.startswith("https://"), f"{page}: social image must be absolute"
            assert (SITE / url[len(CANONICAL):]).is_file(), f"{page}: {url} is not in website/"

    def test_json_ld_parses_and_is_typed(self, page):
        raw = re.findall(r'<script type="application/ld\+json">(.*?)</script>', read(page), re.S)
        assert raw, f"{page} carries no structured data"
        for chunk in raw:
            block = json.loads(chunk)               # raises on malformed JSON
            assert block["@context"] == "https://schema.org", page
            assert block["@type"], page

    def test_title_fits_a_search_result(self, page):
        title = re.search(r"<title>(.*?)</title>", read(page), re.S).group(1).strip()
        assert "Telechat" in title, f"{page}: title does not name the product"
        # Google renders roughly 60 characters; past that the tail is elided.
        assert len(title) <= 70, f"{page}: title is {len(title)} chars"

    def test_documentation_links_resolve_in_this_repo(self, page):
        """A /blob/main/… link is a promise that the file is there."""
        prefix = "https://github.com/telechatai/telechat/blob/main/"
        for ref in refs_in(page):
            if ref.startswith(prefix):
                path = ref[len(prefix):].split("#", 1)[0]
                assert (REPO_ROOT / path).is_file(), f"{page} links to missing {path}"

    def test_github_links_point_at_this_repository(self, page):
        for ref in refs_in(page):
            if "github.com" in ref:
                assert ref.startswith("https://github.com/telechatai/telechat"), (
                    f"{page}: unexpected GitHub link {ref}"
                )


# ─── landing page specifics ──────────────────────────────────────────────────


class TestLandingPage:
    def test_canonical_is_the_site_root(self):
        assert canonical_of("index.html") == CANONICAL

    def test_theme_colour_for_both_schemes(self, html):
        tags = re.findall(r'<meta\s+name="theme-color"[^>]*>', html)
        assert any("prefers-color-scheme: dark" in t for t in tags)
        assert any("prefers-color-scheme: light" in t for t in tags)

    def test_has_a_mobile_layout(self, html):
        # A landing page with a fixed desktop layout is unusable on the device
        # most people will open it on.
        assert "@media(max-width:" in html.replace("@media (max-width:", "@media(max-width:")

    def test_links_to_the_blog(self, refs):
        assert "blog/" in refs, "the landing page does not link to the blog"

    def test_advertises_the_feed(self, html):
        assert 'type="application/rss+xml"' in html, "no RSS autodiscovery"

    def test_describes_the_software(self, blocks, project_version):
        app = next(b for b in blocks if b["@type"] == "SoftwareApplication")
        assert app["name"] == "Telechat"
        assert app["url"] == CANONICAL
        assert app["isAccessibleForFree"] is True
        # A stale version in the structured data is what search engines quote.
        assert app["softwareVersion"] == project_version

    def test_faq_block_matches_the_visible_faq(self, blocks, html):
        faq = next(b for b in blocks if b["@type"] == "FAQPage")
        for q in faq["mainEntity"]:
            assert q["acceptedAnswer"]["text"].strip(), f"empty answer for {q['name']}"

        marked_up = {q["name"].rstrip("?") for q in faq["mainEntity"]}
        visible = {
            re.sub(r"<[^>]+>", "", s).strip().rstrip("?")
            for s in re.findall(r"<summary>(.*?)</summary>", html, re.S)
        }
        # Structured data promising a question the page does not answer is
        # exactly what earns a manual action.
        assert marked_up <= visible, f"in JSON-LD but not on the page: {marked_up - visible}"


# ─── blog ────────────────────────────────────────────────────────────────────


class TestBlog:
    def test_index_links_every_post(self):
        listed = {r for r in refs_in("blog/index.html") if r.endswith(".html")}
        for post in POSTS:
            assert post.removeprefix("blog/") in listed, f"{post} is not on the blog index"

    def test_index_advertises_the_blog_type(self):
        raw = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', read("blog/index.html"), re.S
        )
        types = {json.loads(c)["@type"] for c in raw}
        assert "Blog" in types

    @pytest.mark.parametrize("post", POSTS)
    def test_post_is_marked_up_as_an_article(self, post):
        raw = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', read(post), re.S
        )
        posting = next(
            (json.loads(c) for c in raw if json.loads(c)["@type"] == "BlogPosting"), None
        )
        assert posting, f"{post} has no BlogPosting structured data"
        assert posting["headline"].strip()
        assert posting["url"] == canonical_of(post), f"{post}: JSON-LD url disagrees with canonical"
        # An ISO date, because Google reads this one and a d/m/y string is
        # parsed as neither.
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", posting["datePublished"]), post
        assert _meta(read(post), "og:type", attr="property") == "article"

    @pytest.mark.parametrize("post", POSTS)
    def test_post_headline_matches_the_page(self, post):
        html = read(post)
        h1 = re.sub(r"<[^>]+>", "", re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S).group(1)).strip()
        raw = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
        posting = next(json.loads(c) for c in raw if json.loads(c)["@type"] == "BlogPosting")
        assert posting["headline"].strip() == h1, (
            f"{post}: structured headline {posting['headline']!r} != <h1> {h1!r}"
        )

    @pytest.mark.parametrize("post", POSTS)
    def test_post_dateline_is_machine_readable(self, post):
        assert re.search(r'<time datetime="\d{4}-\d{2}-\d{2}">', read(post)), (
            f"{post}: the visible date has no <time datetime=…>"
        )


class TestFeed:
    def test_channel_metadata(self, channel):
        assert channel.findtext("title")
        assert channel.findtext("description")
        assert channel.findtext("link") == f"{CANONICAL}blog/"

    def test_self_link_is_declared(self, channel):
        atom = "{http://www.w3.org/2005/Atom}link"
        self_links = [el for el in channel.findall(atom) if el.get("rel") == "self"]
        assert self_links, "the feed does not declare its own URL"
        assert self_links[0].get("href") == f"{CANONICAL}feed.xml"

    def test_every_post_is_in_the_feed(self, channel):
        links = {el.findtext("link") for el in channel.findall("item")}
        for post in POSTS:
            assert CANONICAL + post in links, f"{post} is missing from feed.xml"

    def test_items_are_well_formed(self, channel):
        for item in channel.findall("item"):
            link = item.findtext("link")
            assert item.findtext("title", "").strip(), f"{link} has no title"
            assert item.findtext("description", "").strip(), f"{link} has no description"
            assert item.findtext("guid") == link, f"{link}: guid should be the permalink"
            # RFC 822, not ISO 8601 — readers reject the wrong one silently.
            parsedate_to_datetime(item.findtext("pubDate"))
            assert (SITE / link[len(CANONICAL):]).is_file(), f"{link} has no file behind it"


class TestCrawlerFiles:
    def test_robots_allows_crawling_and_points_at_the_sitemap(self):
        robots = (SITE / "robots.txt").read_text(encoding="utf-8")
        assert re.search(r"^User-agent:\s*\*", robots, re.M)
        assert not re.search(r"^Disallow:\s*/\s*$", robots, re.M)
        assert f"Sitemap: {CANONICAL}sitemap.xml" in robots

    def test_sitemap_only_lists_pages_that_exist(self, locs):
        assert CANONICAL in locs, "the homepage is not in the sitemap"
        for loc in locs:
            assert loc.startswith(CANONICAL), f"foreign URL in sitemap: {loc}"
            target = SITE / (loc[len(CANONICAL):] or "index.html")
            if target.is_dir():
                target = target / "index.html"
            assert target.is_file(), f"sitemap lists {loc}, which has no file"

    def test_sitemap_lists_every_page(self, locs):
        for page in PAGES:
            expected = CANONICAL + ("" if page == "index.html" else page)
            expected = expected.replace("blog/index.html", "blog/")
            assert expected in locs, f"{page} is not in the sitemap"

    def test_sitemap_dates_are_iso(self, locs):
        for lastmod in re.findall(r"<lastmod>([^<]+)</lastmod>",
                                  (SITE / "sitemap.xml").read_text()):
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", lastmod), f"bad lastmod: {lastmod}"


# ─── the site agrees with the package ────────────────────────────────────────


class TestSiteMatchesReality:
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

    @pytest.mark.parametrize("page", PAGES)
    def test_advertised_version_matches_the_package(self, page, project_version):
        for claimed in re.findall(r"\bv(\d+\.\d+\.\d+)\b", read(page)):
            assert claimed == project_version, (
                f"{page} advertises v{claimed}, pyproject says {project_version}"
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

    @pytest.mark.parametrize("page", PAGES)
    def test_env_vars_named_on_the_site_are_read_by_the_code(self, page):
        """A setting the page tells you to set must be one the program reads."""
        documented = set(re.findall(r"\b([A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]+)+)\b", read(page)))
        source = "\n".join(
            p.read_text(encoding="utf-8", errors="ignore")
            for p in (REPO_ROOT / "telechat_pkg").glob("*.py")
        )
        for name in documented:
            if name.startswith(("BOT_", "CLAUDE_", "TELEGRAM_", "SLACK_", "WHATSAPP_",
                                "GREEN_API_", "WEB_CHAT_", "BRIDGE_", "ANTHROPIC_")):
                assert name in source, f"{page} documents {name}, which no module reads"
