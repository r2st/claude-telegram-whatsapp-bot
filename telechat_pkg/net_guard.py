"""One place that decides whether the bot is allowed to fetch a URL.

Three modules fetch URLs that arrive in a chat message — `link_understanding`
(automatically, for any link you paste), `web_fetch` (`/fetch`), and
`browser_automation`. Each had its own copy of the same check, and all three
copies shared the same two holes:

* **Only literal IP addresses were rejected.** `http://10.0.0.1/` was blocked,
  but a *hostname* that resolves to 10.0.0.1 was not, and neither was any of
  the public names that resolve to loopback by design.
* **Only the first URL was checked.** Redirects were followed automatically, so
  a public URL that answers `302 Location: http://169.254.169.254/…` reached
  the cloud metadata endpoint, or `http://127.0.0.1:8484/health` reached this
  bot's own health server — and the response was fed to Claude as context and
  could be summarised straight back to the chat.

That matters more here than in a typical server: telechat runs on a personal
machine, inside the network the operator cares about, and `link_understanding`
fires on any link in an incoming message without being asked.

What this module cannot do is close the DNS-rebinding gap. It resolves a name,
checks every address, and then hands the *name* to aiohttp, which resolves it
again — a record with a one-second TTL can answer differently the second time.
Closing that means pinning the connection to a vetted address, which is a
larger change to how these modules make requests. The checks here stop the
straightforward attacks, which is what was actually reachable.
"""
from __future__ import annotations

import asyncio
import logging
from ipaddress import ip_address
from urllib.parse import urlparse

log = logging.getLogger(__name__)

#: Names that must never be fetched, even before resolution.
BLOCKED_HOSTS = {"localhost", "0.0.0.0", "::", "::1", "[::1]", "0"}

#: How many redirects to follow. Each hop is re-checked.
MAX_REDIRECTS = 5


def is_blocked_address(host: str) -> bool:
    """True if ``host`` is a literal IP the bot must not talk to.

    Returns False for anything that is not an IP literal — a hostname needs
    :func:`resolves_to_blocked`, which requires DNS and therefore an event loop.
    """
    try:
        addr = ip_address(host.strip("[]"))
    except ValueError:
        return False
    return bool(
        addr.is_private          # RFC1918, and IPv4 link-local
        or addr.is_loopback
        or addr.is_reserved
        or addr.is_link_local    # explicit: 169.254.169.254 is the metadata endpoint
        or addr.is_multicast
        or addr.is_unspecified
    )


def is_blocked_url(url: str) -> bool:
    """Cheap, synchronous check: scheme, blocked names, and IP literals.

    This is the fast reject. It cannot see through a hostname — use
    :func:`check_url_allowed` before actually making a request.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return True
    if parsed.scheme not in ("http", "https"):
        return True
    host = (parsed.hostname or "").lower()
    if not host or host in BLOCKED_HOSTS:
        return True
    return is_blocked_address(host)


async def resolves_to_blocked(host: str, port: int | None = None) -> bool:
    """True if *any* address ``host`` resolves to is one we must not talk to.

    Any address, not just the first: a name with both a public and a private
    record would otherwise pass the check and connect to whichever the client
    library happened to pick. A name that does not resolve at all is left to
    the request itself to fail on, rather than being reported as blocked.
    """
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port or 0, proto=6)  # IPPROTO_TCP
    except Exception as e:                       # DNS failure, bad name, no network
        log.debug("could not resolve %s for the fetch check: %s", host, e)
        return False
    return any(is_blocked_address(str(info[4][0])) for info in infos)


async def check_url_allowed(url: str) -> str | None:
    """Return None if the URL may be fetched, else a reason it may not.

    The reason is safe to show a user: it names the rule, not the address it
    resolved to, so this cannot be used to map an internal network one probe at
    a time.
    """
    if is_blocked_url(url):
        return "Blocked: local or private address"
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if await resolves_to_blocked(host, parsed.port):
        return "Blocked: hostname resolves to a local or private address"
    return None
