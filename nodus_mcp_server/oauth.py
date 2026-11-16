"""Sign-in (OAuth 2.1) for `--http`, so the server can sit behind a public URL (0.2.0).

Off unless `--http` is given a public URL (`--public-url` or `NODUS_MCP_PUBLIC_URL`).
With it, this server is its own authorization server for `/mcp`, built on the
`mcp` SDK's handlers (`/authorize`, `/token`, `/register`, `/revoke` and both
metadata documents). This module is the provider those handlers call, the
sign-in and consent page, and the storage.

One owner, no users. That keeps it small:

* **Sign-in is the owner's passphrase**, set with `nodus-mcp-server set-passphrase`
  and kept as a scrypt hash in `~/.nodus-mcp-server/passphrase.json`. Setting a
  new one ends every grant.
* **Tokens are opaque.** The issuer and the resource are the same process, so a
  random token stored as a SHA-256 hash with its grant, scopes and expiry is
  enough, and revoking one takes effect on the next request. Access tokens live
  an hour. Refresh tokens live 30 days and rotate on every use; using a rotated
  one again revokes the whole grant. Authorization codes are single-use, live
  60 seconds, and are stored as hashes too. Using a code twice revokes its grant.
* **One grant per client.** A new consent for a client replaces its earlier one.
  `nodus-mcp-server grants list | revoke <id>` shows and ends them.
* **Scopes are per tool** (`TOOL_SCOPES`). `exec` is offered only with
  `--allow-remote-exec`. Without it, `nodus_exec` cannot be reached over the
  public URL at all, whatever a client registered for.
* **Registration is open, capped and rate-limited.** claude.ai and ChatGPT both
  register themselves (RFC 7591). A registered client can do nothing on its own:
  a token still needs the owner to sign in and approve on the consent page.
  Clients that never got a grant are pruned after 30 days.

Client secrets that dynamic registration issues are stored as given, because the
SDK's client authenticator compares against the stored value. They sit in the
owner's home directory beside the memory store, under the same protection.
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from collections import deque
from contextlib import closing
from dataclasses import dataclass, field
from urllib.parse import urlparse

# ── policy ────────────────────────────────────────────────────────────────────

ACCESS_TOKEN_TTL_S = 3600
REFRESH_TOKEN_TTL_S = 30 * 24 * 3600
CODE_TTL_S = 60
PENDING_TTL_S = 600
CLIENT_PRUNE_AFTER_S = 30 * 24 * 3600
MAX_CLIENTS = 50
REGISTRATIONS_PER_HOUR = 20
# Global, not per address: behind a tunnel every request comes from one address
# (the tunnel agent on loopback), and `X-Forwarded-For` is whatever the caller
# says. With one owner, one global limit on passphrase attempts is the right one.
CONSENT_ATTEMPTS_PER_MINUTE = 5
MIN_PASSPHRASE_LEN = 12

CONSENT_PATH = "/oauth/consent"
MCP_PATH = "/mcp"
SUBJECT = "owner"

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}

#: Which scope each tool needs. A tool missing here can never be called over OAuth.
TOOL_SCOPES = {
    "nodus_recall": "memory.read",
    "nodus_remember": "memory.write",
    "nodus_forget": "memory.write",
    "nodus_run_goal": "workflow.run",
    "nodus_run_workflow": "workflow.run",
    "nodus_resume_workflow": "workflow.run",
    "nodus_exec": "exec",
}

_SCOPE_LABELS = {
    "memory.read": "Search your memory",
    "memory.write": "Add to and delete from your memory",
    "workflow.run": "Run goals and workflows",
    "exec": "Run arbitrary Nodus code on this computer (sandboxed)",
}

#: Ticked on the consent page by default. A write scope never is.
_PRE_TICKED = ("memory.read",)


def default_home() -> str:
    return os.path.join(os.path.expanduser("~"), ".nodus-mcp-server")


# ── configuration ─────────────────────────────────────────────────────────────


def normalize_public_url(raw: str) -> str:
    """The origin clients reach this server at. It is the token issuer.

    `https://` only, except a loopback host (local testing), and an origin with no
    path: the OAuth endpoints are served at the root and the MCP endpoint at `/mcp`.
    """
    raw = (raw or "").strip().rstrip("/")
    parsed = urlparse(raw)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError(f"the public URL must be an absolute URL like https://example.com, got {raw!r}")
    if parsed.scheme != "https" and parsed.hostname not in _LOOPBACK:
        raise ValueError(f"the public URL must be https:// (got {raw!r}); http is allowed only for a loopback host")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(f"the public URL must be an origin with no path, got {raw!r}")
    return f"{parsed.scheme}://{parsed.netloc}"


@dataclass(frozen=True)
class OAuthConfig:
    issuer: str
    home: str
    allow_remote_exec: bool = False
    offered_scopes: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        scopes = ["memory.read", "memory.write", "workflow.run"]
        if self.allow_remote_exec:
            scopes.append("exec")
        object.__setattr__(self, "offered_scopes", tuple(scopes))

    @property
    def resource(self) -> str:
        return f"{self.issuer}{MCP_PATH}"

    @property
    def public_host(self) -> str:
        return urlparse(self.issuer).netloc

    @property
    def db_path(self) -> str:
        return os.path.join(self.home, "data", "oauth.db")

    @property
    def passphrase_path(self) -> str:
        return os.path.join(self.home, "passphrase.json")


def tool_allowed(tool: str, scopes) -> bool:
    needed = TOOL_SCOPES.get(tool)
    return needed is not None and needed in scopes


# ── passphrase ────────────────────────────────────────────────────────────────

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def _scrypt(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32)


def passphrase_is_set(home: str) -> bool:
    return os.path.isfile(os.path.join(home, "passphrase.json"))


def set_passphrase(home: str, passphrase: str) -> int:
    """Store a new passphrase hash and end every grant. Returns how many grants ended."""
    if len(passphrase) < MIN_PASSPHRASE_LEN:
        raise ValueError(f"use at least {MIN_PASSPHRASE_LEN} characters")
    os.makedirs(home, exist_ok=True)
    salt = secrets.token_bytes(16)
    record = {
        "alg": "scrypt",
        **_SCRYPT,
        "salt": base64.b64encode(salt).decode(),
        "hash": base64.b64encode(_scrypt(passphrase, salt, **_SCRYPT)).decode(),
    }
    path = os.path.join(home, "passphrase.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows ignores most of chmod
        pass
    return OAuthStore(os.path.join(home, "data", "oauth.db")).revoke_all()


def check_passphrase(home: str, passphrase: str) -> bool:
    try:
        with open(os.path.join(home, "passphrase.json"), encoding="utf-8") as f:
            record = json.load(f)
        salt = base64.b64decode(record["salt"])
        expected = base64.b64decode(record["hash"])
        actual = _scrypt(passphrase, salt, int(record["n"]), int(record["r"]), int(record["p"]))
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# ── storage ───────────────────────────────────────────────────────────────────


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class OAuthStore:
    """Clients, grants, codes and tokens in SQLite. Every secret is stored hashed."""

    def __init__(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._path = db_path
        self._lock = threading.Lock()
        with self._lock, closing(self._conn()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    info TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_grants (
                    grant_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_used_at REAL
                );
                CREATE TABLE IF NOT EXISTS oauth_codes (
                    code_hash TEXT PRIMARY KEY,
                    grant_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    redirect_explicit INTEGER NOT NULL,
                    resource TEXT,
                    expires_at REAL NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS oauth_tokens (
                    token_hash TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    grant_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0
                );
                """
            )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # clients

    def get_client(self, client_id: str) -> dict | None:
        with self._lock, closing(self._conn()) as conn:
            row = conn.execute("SELECT info FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
        return json.loads(row["info"]) if row else None

    def client_count_after_prune(self, now: float) -> int:
        """Drop clients older than the prune age that never got a grant, then count."""
        with self._lock, closing(self._conn()) as conn, conn:
            conn.execute(
                "DELETE FROM oauth_clients WHERE created_at < ? AND client_id NOT IN "
                "(SELECT client_id FROM oauth_grants)",
                (now - CLIENT_PRUNE_AFTER_S,),
            )
            return conn.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0]

    def add_client(self, client_id: str, info: dict, now: float) -> None:
        with self._lock, closing(self._conn()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO oauth_clients (client_id, info, created_at) VALUES (?, ?, ?)",
                (client_id, json.dumps(info), now),
            )

    # grants

    def create_grant_with_code(self, *, client_id: str, scopes: list[str], code: str, code_challenge: str,
                               redirect_uri: str, redirect_explicit: bool, resource: str, now: float) -> str:
        """One grant per client: a new consent ends the earlier grant."""
        grant_id = secrets.token_hex(8)
        with self._lock, closing(self._conn()) as conn, conn:
            old = [r["grant_id"] for r in conn.execute(
                "SELECT grant_id FROM oauth_grants WHERE client_id = ?", (client_id,))]
            for gid in old:
                self._delete_grant(conn, gid)
            conn.execute(
                "INSERT INTO oauth_grants (grant_id, client_id, scopes, created_at) VALUES (?, ?, ?, ?)",
                (grant_id, client_id, " ".join(scopes), now),
            )
            conn.execute(
                "INSERT INTO oauth_codes (code_hash, grant_id, client_id, scopes, code_challenge, redirect_uri,"
                " redirect_explicit, resource, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_sha256(code), grant_id, client_id, " ".join(scopes), code_challenge, redirect_uri,
                 int(redirect_explicit), resource, now + CODE_TTL_S),
            )
        return grant_id

    @staticmethod
    def _delete_grant(conn: sqlite3.Connection, grant_id: str) -> int:
        conn.execute("DELETE FROM oauth_tokens WHERE grant_id = ?", (grant_id,))
        conn.execute("DELETE FROM oauth_codes WHERE grant_id = ?", (grant_id,))
        return conn.execute("DELETE FROM oauth_grants WHERE grant_id = ?", (grant_id,)).rowcount

    def revoke_grant(self, grant_id: str) -> bool:
        with self._lock, closing(self._conn()) as conn, conn:
            return self._delete_grant(conn, grant_id) > 0

    def revoke_all(self) -> int:
        with self._lock, closing(self._conn()) as conn, conn:
            n = conn.execute("DELETE FROM oauth_grants").rowcount
            conn.execute("DELETE FROM oauth_tokens")
            conn.execute("DELETE FROM oauth_codes")
            return n

    def list_grants(self) -> list[dict]:
        with self._lock, closing(self._conn()) as conn:
            rows = conn.execute(
                "SELECT g.grant_id, g.client_id, g.scopes, g.created_at, g.last_used_at, c.info "
                "FROM oauth_grants g LEFT JOIN oauth_clients c ON c.client_id = g.client_id "
                "ORDER BY g.created_at"
            ).fetchall()
        out = []
        for r in rows:
            info = json.loads(r["info"]) if r["info"] else {}
            uris = info.get("redirect_uris") or []
            out.append({
                "grant_id": r["grant_id"],
                "client_id": r["client_id"],
                "client_name": info.get("client_name") or "(unnamed client)",
                "redirect_host": urlparse(str(uris[0])).netloc if uris else "",
                "scopes": r["scopes"].split(),
                "created_at": r["created_at"],
                "last_used_at": r["last_used_at"],
            })
        return out

    # codes

    def load_code(self, client_id: str, code: str) -> dict | None:
        """The code's row, if it is this client's and unused. A second use revokes the grant."""
        with self._lock, closing(self._conn()) as conn, conn:
            row = conn.execute("SELECT * FROM oauth_codes WHERE code_hash = ?", (_sha256(code),)).fetchone()
            if row is None or row["client_id"] != client_id:
                return None
            if row["used"]:
                self._delete_grant(conn, row["grant_id"])
                return None
            return dict(row)

    def consume_code(self, code: str) -> dict | None:
        with self._lock, closing(self._conn()) as conn, conn:
            h = _sha256(code)
            if conn.execute("UPDATE oauth_codes SET used = 1 WHERE code_hash = ? AND used = 0", (h,)).rowcount != 1:
                return None
            return dict(conn.execute("SELECT * FROM oauth_codes WHERE code_hash = ?", (h,)).fetchone())

    # tokens

    def issue(self, *, grant_id: str, client_id: str, scopes: list[str], now: float) -> tuple[str, str]:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self._lock, closing(self._conn()) as conn, conn:
            if conn.execute("SELECT 1 FROM oauth_grants WHERE grant_id = ?", (grant_id,)).fetchone() is None:
                raise LookupError("grant is gone")
            for token, kind, ttl in ((access, "access", ACCESS_TOKEN_TTL_S), (refresh, "refresh", REFRESH_TOKEN_TTL_S)):
                conn.execute(
                    "INSERT INTO oauth_tokens (token_hash, kind, grant_id, client_id, scopes, expires_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (_sha256(token), kind, grant_id, client_id, " ".join(scopes), now + ttl),
                )
        return access, refresh

    def load_token(self, token: str, kind: str) -> dict | None:
        with self._lock, closing(self._conn()) as conn:
            row = conn.execute(
                "SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = ?", (_sha256(token), kind)
            ).fetchone()
        return dict(row) if row else None

    def touch_grant(self, grant_id: str, now: float) -> None:
        with self._lock, closing(self._conn()) as conn, conn:
            conn.execute("UPDATE oauth_grants SET last_used_at = ? WHERE grant_id = ?", (now, grant_id))

    def rotate_refresh(self, token: str) -> dict | None:
        """Mark a refresh token used, once. A token already used means a replay: end its grant."""
        h = _sha256(token)
        with self._lock, closing(self._conn()) as conn, conn:
            row = conn.execute("SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = 'refresh'", (h,)).fetchone()
            if row is None:
                return None
            if row["used"] or conn.execute(
                "UPDATE oauth_tokens SET used = 1 WHERE token_hash = ? AND used = 0", (h,)
            ).rowcount != 1:
                self._delete_grant(conn, row["grant_id"])
                return None
            return dict(row)


# ── the provider the SDK's handlers call ──────────────────────────────────────


class _RateWindow:
    def __init__(self, limit: int, window_s: float) -> None:
        self.limit, self.window_s = limit, window_s
        self._hits: deque = deque()
        self._lock = threading.Lock()

    def hit(self) -> bool:
        """Record an attempt; False when over the limit."""
        now = time.monotonic()
        with self._lock:
            while self._hits and self._hits[0] < now - self.window_s:
                self._hits.popleft()
            if len(self._hits) >= self.limit:
                return False
            self._hits.append(now)
            return True


def _redirect_uri_allowed(uri: str) -> bool:
    """OAuth 2.1: https, or http to a loopback host (a native or local client). No fragment."""
    parsed = urlparse(str(uri))
    if parsed.fragment:
        return False
    if parsed.scheme == "https":
        return bool(parsed.hostname)
    return parsed.scheme == "http" and parsed.hostname in _LOOPBACK


class NodusOAuthProvider:
    """`OAuthAuthorizationServerProvider` and `TokenVerifier`, for one owner."""

    def __init__(self, cfg: OAuthConfig) -> None:
        self.cfg = cfg
        self.store = OAuthStore(cfg.db_path)
        self._pending: dict[str, dict] = {}
        self._pending_lock = threading.Lock()
        self.registrations = _RateWindow(REGISTRATIONS_PER_HOUR, 3600)
        self.consent_attempts = _RateWindow(CONSENT_ATTEMPTS_PER_MINUTE, 60)

    # pending authorizations live in memory: a restart only means "start again from the app"

    def _put_pending(self, entry: dict) -> str:
        key = secrets.token_urlsafe(24)
        now = time.time()
        with self._pending_lock:
            for k in [k for k, v in self._pending.items() if v["expires_at"] < now]:
                del self._pending[k]
            self._pending[key] = {**entry, "expires_at": now + PENDING_TTL_S, "csrf": secrets.token_urlsafe(24)}
        return key

    def get_pending(self, key: str) -> dict | None:
        with self._pending_lock:
            entry = self._pending.get(key)
            if entry and entry["expires_at"] < time.time():
                del self._pending[key]
                return None
            return entry

    def pop_pending(self, key: str) -> dict | None:
        with self._pending_lock:
            return self._pending.pop(key, None)

    # clients

    async def get_client(self, client_id: str):
        from mcp.shared.auth import OAuthClientInformationFull

        info = self.store.get_client(client_id)
        return OAuthClientInformationFull.model_validate(info) if info else None

    async def register_client(self, client_info) -> None:
        from mcp.server.auth.provider import RegistrationError

        for uri in client_info.redirect_uris or []:
            if not _redirect_uri_allowed(str(uri)):
                raise RegistrationError(
                    "invalid_redirect_uri", f"{uri} is not https:// or http:// to a loopback host"
                )
        now = time.time()
        if self.store.client_count_after_prune(now) >= MAX_CLIENTS:
            raise RegistrationError("invalid_client_metadata", "this server has reached its client limit")
        if not self.registrations.hit():
            raise RegistrationError("invalid_client_metadata", "too many registrations; try again later")
        self.store.add_client(client_info.client_id, client_info.model_dump(mode="json", exclude_none=True), now)

    # authorization

    async def authorize(self, client, params) -> str:
        from mcp.server.auth.provider import AuthorizeError

        if params.resource and params.resource.rstrip("/") != self.cfg.resource:
            raise AuthorizeError("invalid_request", f"this server is {self.cfg.resource}, not {params.resource}")
        requested = params.scopes or (client.scope.split() if client.scope else [])
        scopes = [s for s in self.cfg.offered_scopes if s in requested]
        if not scopes:
            raise AuthorizeError("invalid_scope", "none of the requested scopes is offered here")
        key = self._put_pending({
            "client_id": client.client_id,
            "client_name": client.client_name or "(unnamed client)",
            "redirect_uri": str(params.redirect_uri),
            "redirect_explicit": bool(params.redirect_uri_provided_explicitly),
            "code_challenge": params.code_challenge,
            "state": params.state,
            "scopes": scopes,
        })
        return f"{self.cfg.issuer}{CONSENT_PATH}?p={key}"

    async def load_authorization_code(self, client, authorization_code: str):
        from mcp.server.auth.provider import AuthorizationCode

        row = self.store.load_code(client.client_id, authorization_code)
        if row is None:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=row["scopes"].split(),
            expires_at=row["expires_at"],
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=bool(row["redirect_explicit"]),
            resource=row["resource"],
            subject=SUBJECT,
        )

    async def exchange_authorization_code(self, client, authorization_code):
        from mcp.server.auth.provider import TokenError

        row = self.store.consume_code(authorization_code.code)
        if row is None:
            raise TokenError("invalid_grant", "authorization code already used")
        return self._tokens(row["grant_id"], client.client_id, row["scopes"].split())

    def _tokens(self, grant_id: str, client_id: str, scopes: list[str]):
        from mcp.server.auth.provider import TokenError
        from mcp.shared.auth import OAuthToken

        try:
            access, refresh = self.store.issue(grant_id=grant_id, client_id=client_id, scopes=scopes, now=time.time())
        except LookupError:
            raise TokenError("invalid_grant", "this grant was revoked")
        return OAuthToken(
            access_token=access, token_type="Bearer", expires_in=ACCESS_TOKEN_TTL_S,
            refresh_token=refresh, scope=" ".join(scopes),
        )

    async def load_refresh_token(self, client, refresh_token: str):
        from mcp.server.auth.provider import RefreshToken

        row = self.store.load_token(refresh_token, "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        if row["used"]:
            self.store.revoke_grant(row["grant_id"])  # replay of a rotated token
            return None
        return RefreshToken(
            token=refresh_token, client_id=row["client_id"], scopes=row["scopes"].split(),
            expires_at=int(row["expires_at"]), subject=SUBJECT,
        )

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        from mcp.server.auth.provider import TokenError

        row = self.store.rotate_refresh(refresh_token.token)
        if row is None:
            raise TokenError("invalid_grant", "refresh token already used; the grant is revoked")
        held = row["scopes"].split()
        return self._tokens(row["grant_id"], client.client_id, [s for s in held if s in (scopes or held)])

    async def load_access_token(self, token: str):
        from mcp.server.auth.provider import AccessToken

        row = self.store.load_token(token, "access")
        now = time.time()
        if row is None or row["expires_at"] < now:
            return None
        self.store.touch_grant(row["grant_id"], now)
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            # Held to what is offered *now*: dropping --allow-remote-exec takes
            # `exec` away from tokens already issued.
            scopes=[s for s in row["scopes"].split() if s in self.cfg.offered_scopes],
            expires_at=int(row["expires_at"]),
            resource=self.cfg.resource,
            subject=SUBJECT,
            claims={"iss": self.cfg.issuer, "gid": row["grant_id"]},
        )

    async def verify_token(self, token: str):  # TokenVerifier, for the bearer backend
        return await self.load_access_token(token)

    async def revoke_token(self, token) -> None:
        for kind in ("access", "refresh"):
            row = self.store.load_token(token.token, kind)
            if row:
                self.store.revoke_grant(row["grant_id"])
                return


# ── the sign-in and consent page ──────────────────────────────────────────────

_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
}
_CSRF_COOKIE = "nodus_oauth_csrf"


def _page(cfg: OAuthConfig, key: str, pending: dict, *, error: str = "", status: int = 200):
    from starlette.responses import HTMLResponse

    e = html.escape
    redirect_host = urlparse(pending["redirect_uri"]).netloc
    rows = []
    for scope in pending["scopes"]:
        checked = " checked" if scope in _PRE_TICKED else ""
        tools = ", ".join(t for t, s in TOOL_SCOPES.items() if s == scope)
        rows.append(
            f'<label><input type="checkbox" name="scope" value="{e(scope)}"{checked}> '
            f"<b>{e(_SCOPE_LABELS.get(scope, scope))}</b><br><small>{e(scope)}: {e(tools)}</small></label>"
        )
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Connect to Nodus</title>
<style>body{{font:16px system-ui,sans-serif;max-width:32rem;margin:2rem auto;padding:0 1rem;color:#1a1a1a;background:#fff}}
label{{display:block;margin:.6rem 0}} input[type=password]{{width:100%;padding:.5rem;margin:.2rem 0 .8rem;box-sizing:border-box}}
.host{{font-size:1.25rem;font-weight:700;word-break:break-all}} .err{{color:#a00}} button{{padding:.6rem 1rem;margin-right:.5rem}}</style></head><body>
<h1>Connect an app to your Nodus server</h1>
<p>An app is asking to use the Nodus MCP server at <b>{e(cfg.issuer)}</b>. After you answer, you'll be sent back to:</p>
<p class="host">{e(redirect_host)}</p>
<p>It calls itself <i>{e(pending["client_name"])}</i>. Apps choose their own names, so check the address above.</p>
{f'<p class="err">{e(error)}</p>' if error else ''}
<form method="post" action="{CONSENT_PATH}">
<input type="hidden" name="p" value="{e(key)}"><input type="hidden" name="csrf" value="{e(pending["csrf"])}">
<fieldset><legend>Allow it to</legend>{''.join(rows)}</fieldset>
<label>Passphrase <input type="password" name="passphrase" autocomplete="current-password" required></label>
<button type="submit" name="action" value="approve">Allow</button>
<button type="submit" name="action" value="deny" formnovalidate>Deny</button>
</form></body></html>"""
    response = HTMLResponse(body, status_code=status, headers=_PAGE_HEADERS)
    response.set_cookie(_CSRF_COOKIE, pending["csrf"], httponly=True, samesite="strict",
                        secure=cfg.issuer.startswith("https://"), path=CONSENT_PATH, max_age=PENDING_TTL_S)
    return response


def consent_routes(provider: NodusOAuthProvider) -> list:
    import anyio
    from mcp.server.auth.provider import construct_redirect_uri
    from starlette.responses import PlainTextResponse, RedirectResponse
    from starlette.routing import Route

    cfg = provider.cfg
    expired = "This sign-in link has expired. Start again from the app."

    async def consent(request):
        if request.method == "GET":
            key = request.query_params.get("p", "")
            pending = provider.get_pending(key)
            if not pending:
                return PlainTextResponse(expired, 400, headers=_PAGE_HEADERS)
            return _page(cfg, key, pending)

        form = await request.form()
        key = str(form.get("p", ""))
        pending = provider.get_pending(key)
        if not pending:
            return PlainTextResponse(expired, 400, headers=_PAGE_HEADERS)
        cookie, posted = request.cookies.get(_CSRF_COOKIE, ""), str(form.get("csrf", ""))
        if not (cookie and secrets.compare_digest(cookie, posted) and secrets.compare_digest(posted, pending["csrf"])):
            return PlainTextResponse("The form expired. Reload the page and try again.", 400, headers=_PAGE_HEADERS)

        if form.get("action") == "deny":
            provider.pop_pending(key)
            return RedirectResponse(
                construct_redirect_uri(pending["redirect_uri"], error="access_denied", state=pending["state"]),
                status_code=302, headers=_PAGE_HEADERS,
            )

        if not provider.consent_attempts.hit():
            return _page(cfg, key, pending, error="Too many attempts. Wait a minute and try again.", status=429)
        chosen = [s for s in form.getlist("scope") if s in pending["scopes"]]
        if not chosen:
            return _page(cfg, key, pending, error="Choose at least one thing to allow.", status=400)
        passphrase = str(form.get("passphrase", ""))
        if not await anyio.to_thread.run_sync(lambda: check_passphrase(cfg.home, passphrase)):
            return _page(cfg, key, pending, error="Wrong passphrase.", status=401)

        if provider.pop_pending(key) is None:  # a double submit raced this one
            return PlainTextResponse(expired, 400, headers=_PAGE_HEADERS)
        code = secrets.token_urlsafe(32)
        provider.store.create_grant_with_code(
            client_id=pending["client_id"], scopes=chosen, code=code, code_challenge=pending["code_challenge"],
            redirect_uri=pending["redirect_uri"], redirect_explicit=pending["redirect_explicit"],
            resource=cfg.resource, now=time.time(),
        )
        print(f"[nodus-mcp-server] granted {chosen} to {pending['client_name']!r} "
              f"({urlparse(pending['redirect_uri']).netloc})", file=sys.stderr)
        return RedirectResponse(
            construct_redirect_uri(pending["redirect_uri"], code=code, state=pending["state"]),
            status_code=302, headers=_PAGE_HEADERS,
        )

    return [Route(CONSENT_PATH, endpoint=consent, methods=["GET", "POST"])]


# ── wiring ────────────────────────────────────────────────────────────────────

#: Set in the ASGI scope of every request the gate lets through to `/mcp`, so a
#: tool handler can tell "OAuth on, these scopes" from "no OAuth" (stdio, or
#: `--http` without a public URL), and never mistakes a missing token for the latter.
SCOPE_KEY = "nodus.oauth_scopes"


class BearerGate:
    """Require a valid access token on `/mcp` and everything under it; the OAuth routes stay open.

    A `401` carries the `WWW-Authenticate` header that points clients at the
    protected-resource metadata, which is how claude.ai and ChatGPT find the
    authorization server.
    """

    def __init__(self, app, resource_metadata_url) -> None:
        from mcp.server.auth.middleware.bearer_auth import RequireAuthMiddleware

        self.app = app
        self.require = RequireAuthMiddleware(self._admitted, [], resource_metadata_url)

    async def _admitted(self, scope, receive, send):
        scope[SCOPE_KEY] = frozenset(scope["user"].scopes)
        await self.app(scope, receive, send)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope.get("type") == "http" and (path == MCP_PATH or path.startswith(MCP_PATH + "/")):
            await self.require(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def routes_and_middleware(provider: NodusOAuthProvider) -> tuple[list, list]:
    import inspect

    from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
    from mcp.server.auth.routes import build_resource_metadata_url, create_auth_routes, create_protected_resource_routes
    from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
    from pydantic import AnyHttpUrl
    from starlette.middleware import Middleware
    from starlette.middleware.authentication import AuthenticationMiddleware

    cfg = provider.cfg
    issuer, resource = AnyHttpUrl(cfg.issuer), AnyHttpUrl(cfg.resource)
    offered = list(cfg.offered_scopes)
    routes = [
        *create_auth_routes(
            provider,
            issuer_url=issuer,
            # A client that registers without naming scopes may ask for any offered
            # one; the consent page is where the owner narrows it.
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=offered, default_scopes=offered,
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
        *create_protected_resource_routes(
            resource_url=resource, authorization_servers=[issuer], scopes_supported=offered,
        ),
        *consent_routes(provider),
    ]
    backend_kwargs = {}
    if "resource_server_url" in inspect.signature(BearerAuthBackend.__init__).parameters:  # mcp 2.x
        backend_kwargs["resource_server_url"] = resource
    middleware = [
        Middleware(AuthenticationMiddleware, backend=BearerAuthBackend(provider, **backend_kwargs)),
        Middleware(BearerGate, resource_metadata_url=build_resource_metadata_url(resource)),
    ]
    return routes, middleware


def transport_security(cfg: OAuthConfig):
    """The `Host`/`Origin` check for `/mcp` with a public URL: the public host, plus loopback."""
    from mcp.server.transport_security import TransportSecuritySettings

    names = ("127.0.0.1", "localhost", "[::1]")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[cfg.public_host, *names, *(f"{n}:*" for n in names)],
        allowed_origins=[cfg.issuer],
    )


# ── CLI: set-passphrase, grants ───────────────────────────────────────────────


def cli(argv: list[str], home: str | None = None) -> int:
    import argparse

    home = home or default_home()
    parser = argparse.ArgumentParser(prog="nodus-mcp-server", description="Manage sign-in for --http --public-url")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("set-passphrase", help="Set the owner's passphrase (prompted). Ends every grant.")
    grants = sub.add_parser("grants", help="Apps you have let in")
    gsub = grants.add_subparsers(dest="action", required=True)
    gsub.add_parser("list", help="List grants")
    revoke = gsub.add_parser("revoke", help="End a grant now")
    revoke.add_argument("grant_id")
    args = parser.parse_args(argv)

    if args.cmd == "set-passphrase":
        first = getpass.getpass("New passphrase: ")
        if first != getpass.getpass("Again: "):
            print("The two entries differ; nothing changed.", file=sys.stderr)
            return 1
        try:
            ended = set_passphrase(home, first)
        except ValueError as exc:
            print(f"Not set: {exc}.", file=sys.stderr)
            return 1
        print(f"Passphrase set. {ended} grant(s) ended; each app signs in again.")
        return 0

    store = OAuthStore(os.path.join(home, "data", "oauth.db"))
    if args.action == "list":
        rows = store.list_grants()
        if not rows:
            print("No grants.")
        for g in rows:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(g["created_at"]))
            used = time.strftime("%Y-%m-%d %H:%M", time.localtime(g["last_used_at"])) if g["last_used_at"] else "never"
            print(f"{g['grant_id']}  {g['client_name']} ({g['redirect_host']})  "
                  f"{' '.join(g['scopes'])}  granted {when}, last used {used}")
        return 0
    if store.revoke_grant(args.grant_id):
        print(f"Revoked {args.grant_id}.")
        return 0
    print(f"No grant {args.grant_id}.", file=sys.stderr)
    return 1
