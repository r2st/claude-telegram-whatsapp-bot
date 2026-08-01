"""
Invite codes and referral attribution for telechat.

Telechat is self-hosted: the allowlist is a static env var of numeric user
IDs, so letting a friend use your bot means asking for their numeric ID,
editing ``.env``, and restarting. That friction is why nobody shares their
bot. This module removes it.

An operator mints a code, shares the link, and whoever taps it is granted
access — persistently, in the database, with no restart and without the
operator ever seeing a user ID. Every grant records who invited whom, so
``/invites`` can show a referral tree and a leaderboard.

Codes are stored in the same SQLite database as everything else
(``bot.db``). Redemption is atomic: the use counter is incremented by a
conditional UPDATE inside a ``BEGIN IMMEDIATE`` transaction, so two people
tapping a single-use link at the same moment cannot both get in.

Usage:
    from telechat_pkg.invites import get_store

    inv = get_store()
    invite = inv.create("telegram", "12345", max_uses=5, ttl_days=7)
    invite.code                      # 'K7QW2M9XPT'
    inv.deep_link("mybot", invite.code)
    result = inv.redeem(invite.code, "telegram", "67890", display_name="Ada")
    result.ok                        # True
    inv.is_granted("telegram", "67890")   # True
    inv.stats("telegram", "12345")   # {'direct': 1, 'network': 1, ...}

Security posture:

- ``INVITE_ALLOW_CHAINING`` (default off) decides whether a user who was
  themselves invited may mint further invites. Off means invites fan out
  exactly one level from an operator, which is what you want when "anyone
  with a link" is spending your Claude quota.
- A code grants access. It does not grant operator powers, and it never
  widens the env allowlist on disk.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

log = logging.getLogger(__name__)

# ─── Code format ──────────────────────────────────────────────────────────────

# Uppercase alphanumerics minus the four characters people misread out loud
# or off a screen: I/1 and O/0. What is left is safe to read aloud, safe to
# type, and safe in a Telegram deep-link payload (A–Z, 0–9, _ and - only).
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 10
_CODE_RE = re.compile(rf"^[{CODE_ALPHABET}]{{{CODE_LENGTH}}}$")

# Telegram's start payload is capped at 64 chars; the prefix keeps invite
# payloads distinguishable from any other deep link the bot might grow.
DEEP_LINK_PREFIX = "inv_"

DEFAULT_MAX_USES = 1
DEFAULT_TTL_DAYS = 7
MAX_TTL_DAYS = 365
MAX_USES_CEILING = 10_000


def _chaining_allowed() -> bool:
    """Whether an invited user may mint invites of their own.

    Read per call, not at import, so tests and ``telechat doctor`` see the
    current environment rather than whatever was set when the module loaded.
    """
    return os.getenv("INVITE_ALLOW_CHAINING", "").strip().lower() in ("1", "true", "yes", "on")


def generate_code() -> str:
    """Return a fresh random invite code.

    ~50 bits of entropy (32^10). Codes are bearer tokens for bot access, so
    they come from ``secrets``, never ``random``.
    """
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_code(raw: str) -> str:
    """Canonicalise user-typed input into a code, or '' if it cannot be one.

    Accepts the deep-link payload form (``inv_ABC…``), surrounding
    whitespace, lowercase, and the dashes people insert when reading a code
    aloud. Anything that does not then look like a code returns ''.
    """
    if not raw:
        return ""
    s = str(raw).strip()
    if s.lower().startswith(DEEP_LINK_PREFIX):
        s = s[len(DEEP_LINK_PREFIX):]
    s = re.sub(r"[^A-Za-z0-9]", "", s).upper()
    return s if _CODE_RE.match(s) else ""


def deep_link(bot_username: str, code: str) -> str:
    """Return the one-tap Telegram link that redeems ``code``."""
    return f"https://t.me/{str(bot_username).lstrip('@')}?start={DEEP_LINK_PREFIX}{code}"


# ─── Records ──────────────────────────────────────────────────────────────────


@dataclass
class Invite:
    code: str
    platform: str
    created_by: str
    created_at: float
    expires_at: Optional[float] = None   # None = never expires
    max_uses: int = DEFAULT_MAX_USES     # 0 = unlimited
    uses: int = 0
    revoked: bool = False
    note: str = ""

    @property
    def unlimited(self) -> bool:
        return self.max_uses == 0

    def is_expired(self, now: Optional[float] = None) -> bool:
        if self.expires_at is None:
            return False
        return (now if now is not None else time.time()) >= self.expires_at

    def is_exhausted(self) -> bool:
        return not self.unlimited and self.uses >= self.max_uses

    def is_usable(self, now: Optional[float] = None) -> bool:
        return not (self.revoked or self.is_expired(now) or self.is_exhausted())

    def status(self, now: Optional[float] = None) -> str:
        """One word for a list view: revoked / expired / used / active."""
        if self.revoked:
            return "revoked"
        if self.is_expired(now):
            return "expired"
        if self.is_exhausted():
            return "used"
        return "active"

    def remaining(self) -> Optional[int]:
        """Uses left, or None when unlimited."""
        return None if self.unlimited else max(0, self.max_uses - self.uses)


@dataclass
class Grant:
    platform: str
    user_id: str
    granted_at: float
    code: Optional[str] = None
    invited_by: Optional[str] = None
    display_name: str = ""


@dataclass
class RedeemResult:
    ok: bool
    reason: str                       # ok | unknown | revoked | expired | exhausted | self | already
    invite: Optional[Invite] = None
    grant: Optional[Grant] = None

    #: Human-readable explanations, safe to show a stranger — they say why the
    #: link did not work without confirming whether a code exists.
    MESSAGES = {
        "ok": "Access granted.",
        "unknown": "That invite code is not valid.",
        "revoked": "That invite has been revoked.",
        "expired": "That invite has expired.",
        "exhausted": "That invite has already been used the maximum number of times.",
        "self": "You cannot redeem your own invite.",
        "already": "You already have access.",
    }

    @property
    def message(self) -> str:
        return self.MESSAGES.get(self.reason, "That invite could not be redeemed.")


@dataclass
class ReferralStats:
    created: int = 0          # invites minted
    active: int = 0           # of those, still usable
    redemptions: int = 0      # total uses across own invites
    direct: int = 0           # users invited personally
    network: int = 0          # direct + everyone they went on to invite
    names: list[str] = field(default_factory=list)   # display names of direct invitees


# ─── Store ────────────────────────────────────────────────────────────────────


class InviteStore:
    """Invite codes, access grants, and the referral graph over them."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            from . import store
            db_path = store.DB_PATH
        self._db_path = db_path
        self._local = threading.local()
        # Guards the read-modify-write in redeem() against other threads in
        # this process; BEGIN IMMEDIATE guards it against other processes.
        self._lock = threading.RLock()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        if getattr(self._local, "conn", None) is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            # Explicit transaction control — redeem() needs BEGIN IMMEDIATE,
            # which python's implicit transaction handling will not emit.
            conn.isolation_level = None
            self._local.conn = conn
        return self._local.conn

    def _init_schema(self) -> None:
        self._conn().executescript("""
            CREATE TABLE IF NOT EXISTS invites (
                code        TEXT PRIMARY KEY,
                platform    TEXT NOT NULL,
                created_by  TEXT NOT NULL,
                created_at  REAL NOT NULL,
                expires_at  REAL,
                max_uses    INTEGER NOT NULL DEFAULT 1,
                uses        INTEGER NOT NULL DEFAULT 0,
                revoked     INTEGER NOT NULL DEFAULT 0,
                note        TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_invites_creator
                ON invites(platform, created_by, created_at DESC);

            CREATE TABLE IF NOT EXISTS invite_grants (
                platform     TEXT NOT NULL,
                user_id      TEXT NOT NULL,
                granted_at   REAL NOT NULL,
                code         TEXT,
                invited_by   TEXT,
                display_name TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (platform, user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_grants_inviter
                ON invite_grants(platform, invited_by);
        """)

    # ── Creating ─────────────────────────────────────────────────────────────

    def create(
        self,
        platform: str,
        created_by: str,
        *,
        max_uses: int = DEFAULT_MAX_USES,
        ttl_days: Optional[float] = DEFAULT_TTL_DAYS,
        note: str = "",
    ) -> Invite:
        """Mint an invite. ``max_uses=0`` is unlimited, ``ttl_days=None`` never expires."""
        max_uses = max(0, min(int(max_uses), MAX_USES_CEILING))
        now = time.time()
        expires_at: Optional[float] = None
        if ttl_days is not None:
            ttl = max(0.0, min(float(ttl_days), MAX_TTL_DAYS))
            expires_at = now + ttl * 86400

        # A collision is vanishingly unlikely, but "vanishingly unlikely"
        # silently handing someone else's invite to a stranger is not a
        # failure mode worth accepting. Retry on the primary-key conflict.
        for _ in range(8):
            code = generate_code()
            try:
                self._conn().execute(
                    "INSERT INTO invites (code, platform, created_by, created_at, "
                    "expires_at, max_uses, uses, revoked, note) VALUES (?,?,?,?,?,?,0,0,?)",
                    (code, platform, str(created_by), now, expires_at, max_uses, note[:200]),
                )
                break
            except sqlite3.IntegrityError:  # pragma: no cover - needs a real collision
                continue
        else:  # pragma: no cover
            raise RuntimeError("could not allocate an unused invite code")

        return Invite(
            code=code, platform=platform, created_by=str(created_by), created_at=now,
            expires_at=expires_at, max_uses=max_uses, uses=0, revoked=False, note=note[:200],
        )

    def can_invite(self, platform: str, user_id: str, *, is_operator: bool) -> bool:
        """Whether this user may mint invites.

        Operators (present in the env allowlist, or running an open bot) always
        may. Users who were themselves invited may only when
        ``INVITE_ALLOW_CHAINING`` is on.
        """
        if is_operator:
            return True
        return _chaining_allowed() and self.is_granted(platform, user_id)

    # ── Reading ──────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_invite(row: sqlite3.Row) -> Invite:
        return Invite(
            code=row["code"], platform=row["platform"], created_by=row["created_by"],
            created_at=row["created_at"], expires_at=row["expires_at"],
            max_uses=row["max_uses"], uses=row["uses"], revoked=bool(row["revoked"]),
            note=row["note"] or "",
        )

    def get(self, code: str) -> Optional[Invite]:
        norm = normalize_code(code)
        if not norm:
            return None
        row = self._conn().execute("SELECT * FROM invites WHERE code = ?", (norm,)).fetchone()
        return self._row_to_invite(row) if row else None

    def list_created_by(self, platform: str, user_id: str, *, limit: int = 25) -> list[Invite]:
        rows = self._conn().execute(
            "SELECT * FROM invites WHERE platform = ? AND created_by = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (platform, str(user_id), max(1, int(limit))),
        ).fetchall()
        return [self._row_to_invite(r) for r in rows]

    # ── Redeeming ────────────────────────────────────────────────────────────

    def redeem(
        self,
        code: str,
        platform: str,
        user_id: str,
        *,
        display_name: str = "",
    ) -> RedeemResult:
        """Consume one use of ``code`` and grant ``user_id`` access.

        Atomic: the use counter is bumped by a conditional UPDATE, so a
        single-use link tapped simultaneously by two people admits exactly
        one of them. Redeeming when already granted is a no-op that does not
        burn a use.
        """
        norm = normalize_code(code)
        uid = str(user_id)
        if not norm:
            return RedeemResult(False, "unknown")

        with self._lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM invites WHERE code = ?", (norm,)).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    return RedeemResult(False, "unknown")
                invite = self._row_to_invite(row)

                if invite.platform != platform:
                    # A Slack code is not a Telegram code. Report it as unknown
                    # rather than leaking that the code exists elsewhere.
                    conn.execute("ROLLBACK")
                    return RedeemResult(False, "unknown")

                existing = conn.execute(
                    "SELECT * FROM invite_grants WHERE platform = ? AND user_id = ?",
                    (platform, uid),
                ).fetchone()
                if existing is not None:
                    conn.execute("ROLLBACK")
                    return RedeemResult(False, "already", invite, self._row_to_grant(existing))

                if invite.created_by == uid:
                    conn.execute("ROLLBACK")
                    return RedeemResult(False, "self", invite)
                if invite.revoked:
                    conn.execute("ROLLBACK")
                    return RedeemResult(False, "revoked", invite)

                now = time.time()
                if invite.is_expired(now):
                    conn.execute("ROLLBACK")
                    return RedeemResult(False, "expired", invite)

                cur = conn.execute(
                    "UPDATE invites SET uses = uses + 1 WHERE code = ? AND revoked = 0 "
                    "AND (expires_at IS NULL OR expires_at > ?) "
                    "AND (max_uses = 0 OR uses < max_uses)",
                    (norm, now),
                )
                if cur.rowcount != 1:
                    conn.execute("ROLLBACK")
                    return RedeemResult(False, "exhausted", invite)

                conn.execute(
                    "INSERT INTO invite_grants (platform, user_id, granted_at, code, "
                    "invited_by, display_name) VALUES (?,?,?,?,?,?)",
                    (platform, uid, now, norm, invite.created_by, (display_name or "")[:100]),
                )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover
                    pass
                raise

        invite.uses += 1
        grant = Grant(
            platform=platform, user_id=uid, granted_at=now, code=norm,
            invited_by=invite.created_by, display_name=(display_name or "")[:100],
        )
        log.info("invite %s redeemed by %s/%s (invited by %s)",
                 norm, platform, uid, invite.created_by)
        return RedeemResult(True, "ok", invite, grant)

    def revoke(self, code: str, *, requester: Optional[str] = None) -> bool:
        """Revoke an invite. With ``requester``, only its creator may do so.

        Already-granted users keep their access — revoking closes the door,
        it does not evict whoever already walked through. Use
        ``revoke_access`` for that.
        """
        norm = normalize_code(code)
        if not norm:
            return False
        if requester is None:
            cur = self._conn().execute("UPDATE invites SET revoked = 1 WHERE code = ?", (norm,))
        else:
            cur = self._conn().execute(
                "UPDATE invites SET revoked = 1 WHERE code = ? AND created_by = ?",
                (norm, str(requester)),
            )
        return cur.rowcount > 0

    # ── Grants ───────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_grant(row: sqlite3.Row) -> Grant:
        return Grant(
            platform=row["platform"], user_id=row["user_id"], granted_at=row["granted_at"],
            code=row["code"], invited_by=row["invited_by"], display_name=row["display_name"] or "",
        )

    def is_granted(self, platform: str, user_id: str) -> bool:
        row = self._conn().execute(
            "SELECT 1 FROM invite_grants WHERE platform = ? AND user_id = ?",
            (platform, str(user_id)),
        ).fetchone()
        return row is not None

    def get_grant(self, platform: str, user_id: str) -> Optional[Grant]:
        row = self._conn().execute(
            "SELECT * FROM invite_grants WHERE platform = ? AND user_id = ?",
            (platform, str(user_id)),
        ).fetchone()
        return self._row_to_grant(row) if row else None

    def grant_direct(
        self,
        platform: str,
        user_id: str,
        *,
        invited_by: Optional[str] = None,
        display_name: str = "",
    ) -> Grant:
        """Grant access without a code — the operator adding someone by hand."""
        now = time.time()
        self._conn().execute(
            "INSERT OR REPLACE INTO invite_grants (platform, user_id, granted_at, code, "
            "invited_by, display_name) VALUES (?,?,?,NULL,?,?)",
            (platform, str(user_id), now, str(invited_by) if invited_by else None,
             (display_name or "")[:100]),
        )
        return Grant(platform=platform, user_id=str(user_id), granted_at=now, code=None,
                     invited_by=str(invited_by) if invited_by else None,
                     display_name=(display_name or "")[:100])

    def revoke_access(self, platform: str, user_id: str) -> bool:
        """Remove someone's granted access. Their own invitees keep theirs."""
        cur = self._conn().execute(
            "DELETE FROM invite_grants WHERE platform = ? AND user_id = ?",
            (platform, str(user_id)),
        )
        return cur.rowcount > 0

    def invitees(self, platform: str, user_id: str) -> list[Grant]:
        """Everyone this user invited directly, newest first."""
        rows = self._conn().execute(
            "SELECT * FROM invite_grants WHERE platform = ? AND invited_by = ? "
            "ORDER BY granted_at DESC",
            (platform, str(user_id)),
        ).fetchall()
        return [self._row_to_grant(r) for r in rows]

    def network_size(self, platform: str, user_id: str, *, max_depth: int = 10) -> int:
        """Everyone downstream of this user — invitees, their invitees, and so on.

        Breadth-first with a visited set and a depth cap: a grant can only
        name an existing user as inviter so a cycle should be impossible, but
        a corrupted row should not hang the bot.
        """
        seen: set[str] = {str(user_id)}
        frontier = [str(user_id)]
        total = 0
        conn = self._conn()
        for _ in range(max_depth):
            if not frontier:
                break
            placeholders = ",".join("?" * len(frontier))
            rows = conn.execute(
                f"SELECT user_id FROM invite_grants WHERE platform = ? "
                f"AND invited_by IN ({placeholders})",
                (platform, *frontier),
            ).fetchall()
            nxt = [r["user_id"] for r in rows if r["user_id"] not in seen]
            seen.update(nxt)
            total += len(nxt)
            frontier = nxt
        return total

    # ── Aggregates ───────────────────────────────────────────────────────────

    def stats(self, platform: str, user_id: str) -> ReferralStats:
        uid = str(user_id)
        conn = self._conn()
        now = time.time()
        invites = self.list_created_by(platform, uid, limit=MAX_USES_CEILING)
        direct = self.invitees(platform, uid)
        row = conn.execute(
            "SELECT COALESCE(SUM(uses), 0) AS total FROM invites "
            "WHERE platform = ? AND created_by = ?",
            (platform, uid),
        ).fetchone()
        return ReferralStats(
            created=len(invites),
            active=sum(1 for i in invites if i.is_usable(now)),
            redemptions=int(row["total"] if row else 0),
            direct=len(direct),
            network=self.network_size(platform, uid),
            names=[g.display_name or g.user_id for g in direct[:10]],
        )

    def leaderboard(self, platform: str, *, limit: int = 10) -> list[tuple[str, int]]:
        """Top inviters as (user_id, direct invite count), most first."""
        rows = self._conn().execute(
            "SELECT invited_by, COUNT(*) AS n FROM invite_grants "
            "WHERE platform = ? AND invited_by IS NOT NULL "
            "GROUP BY invited_by ORDER BY n DESC, invited_by ASC LIMIT ?",
            (platform, max(1, int(limit))),
        ).fetchall()
        return [(r["invited_by"], int(r["n"])) for r in rows]

    def granted_users(self, platform: str) -> list[str]:
        rows = self._conn().execute(
            "SELECT user_id FROM invite_grants WHERE platform = ? ORDER BY granted_at",
            (platform,),
        ).fetchall()
        return [r["user_id"] for r in rows]

    def purge_expired(self, *, older_than_days: float = 30) -> int:
        """Delete invites that expired more than ``older_than_days`` ago.

        Grants are never touched — deleting one would evict a real user.
        """
        cutoff = time.time() - max(0.0, float(older_than_days)) * 86400
        cur = self._conn().execute(
            "DELETE FROM invites WHERE expires_at IS NOT NULL AND expires_at < ?", (cutoff,)
        )
        return cur.rowcount

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


# ─── Module-level default store ───────────────────────────────────────────────

_store: Optional[InviteStore] = None
_store_lock = threading.Lock()


def get_store() -> InviteStore:
    """The shared InviteStore over the canonical bot.db."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = InviteStore()
    return _store


def reset_store() -> None:
    """Drop the cached store — for tests that repoint DB_PATH."""
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
        _store = None


def is_granted(platform: str, user_id: str) -> bool:
    """Convenience wrapper that never raises — adapters call this on the hot path.

    A database problem must not lock out an operator whose access comes from
    the env allowlist, so failure is reported as "not granted" and logged.
    """
    try:
        return get_store().is_granted(platform, str(user_id))
    except Exception:
        log.debug("invite grant lookup failed", exc_info=True)
        return False


def format_invite_line(inv: Invite, *, now: Optional[float] = None) -> str:
    """One-line summary of an invite for a chat listing."""
    now = now if now is not None else time.time()
    status = inv.status(now)
    icon = {"active": "🟢", "used": "⚪", "expired": "🕘", "revoked": "🚫"}.get(status, "•")
    if inv.unlimited:
        uses = f"{inv.uses} uses"
    else:
        uses = f"{inv.uses}/{inv.max_uses}"
    bits = [f"{icon} `{inv.code}`", uses]
    if status == "active" and inv.expires_at:
        hours = max(0, int((inv.expires_at - now) // 3600))
        bits.append(f"{hours}h left" if hours < 48 else f"{hours // 24}d left")
    elif status != "active":
        bits.append(status)
    if inv.note:
        bits.append(f"— {inv.note}")
    return " · ".join(bits)


def summarize(stats: ReferralStats, invitees: Iterable[str] = ()) -> str:
    """Short referral summary for a chat message."""
    lines = [
        f"Invites minted: *{stats.created}* ({stats.active} still active)",
        f"People you invited: *{stats.direct}*",
    ]
    if stats.network > stats.direct:
        lines.append(f"Your network: *{stats.network}* (including their invitees)")
    names = [n for n in (invitees or stats.names) if n]
    if names:
        lines.append("Recently joined: " + ", ".join(names[:5]))
    return "\n".join(lines)
