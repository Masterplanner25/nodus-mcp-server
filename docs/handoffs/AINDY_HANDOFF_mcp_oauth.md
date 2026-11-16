# nodus-mcp-server: sign-in (OAuth) for `--http`, so the public tunnel can come back

Written 2026-11-11 from `aindy-apps-monolith` for whoever works in `nodus-mcp-server`. **One opt-in feature asked
for; stdio and today's loopback HTTP behave exactly as now unless it is turned on.**

## 1. Why

The owner's server runs at `127.0.0.1:8765` (`--http`), started at sign-in by the "nodus-mcp-server HTTP" task
(`~/.nodus-mcp-server/run_http.py`). Its public tunnel, `nodusmcpserver.ngrok.io`, has been off since 2026-11-06:
`~/.nodus-mcp-server/startup.ps1` says no public tunnel until the server has auth. **The owner confirmed on 2026-11-11
that it stays off until the server has sign-in, the way A.I.N.D.Y.'s `/mcp` does.**

Today, anyone who reaches `/mcp` gets all seven tools as the owner: `nodus_remember`, `nodus_recall`, `nodus_forget`,
`nodus_run_goal`, `nodus_run_workflow`, `nodus_resume_workflow` and `nodus_exec`. `nodus_exec` is sandboxed (no file
I/O, network, subprocess or env, 10 s), but it is still arbitrary code on the owner's laptop, and the memory store is
the owner's.

The goal: claude.ai and ChatGPT add the server by URL, the owner signs in once per client and approves the scopes, and
nothing else that reaches the URL can call a tool.

## 2. What A.I.N.D.Y. built, and what carries over

A.I.N.D.Y. went live for claude.ai and ChatGPT on 2026-11-11 with exactly this. The runtime's implementation is the
reference. It runs in the same SDK, the same transport and a Starlette app much like this one:

- `C:\dev\aindy-runtime\AINDY\platform_layer\mcp_oauth.py` (~1,000 lines; the provider, the consent page, storage);
- `C:\dev\aindy-runtime\docs\design\MCP_REMOTE_TRANSPORT_DESIGN.md` §5–§8 (the flow, tokens, tool policy, abuse cases).

**Carry over:**
- **The server is its own authorization server, built on the `mcp` SDK's handlers**:
  - `mcp.server.auth.routes.create_auth_routes(provider, issuer_url=…, client_registration_options=…, revocation_options=…)`
    serves `/authorize`, `/token`, `/register` and `/revoke`, plus `/.well-known/oauth-authorization-server`;
  - `create_protected_resource_routes(resource_url=…, authorization_servers=[issuer], scopes_supported=…)` serves
    `/.well-known/oauth-protected-resource`;
  - you write the provider (`authorize`, `exchange_authorization_code`, `exchange_refresh_token`, `load_access_token`,
    `register_client`, `get_client`, `revoke_token`) and one consent page.
- **The flow:** DCR, then authorize with PKCE (`S256` only), `state`, `scope` and `resource`, then sign-in and consent.
  The code is single-use, lives 60 s, and is bound to the client, PKCE, scopes and resource. The redirect URI must match
  exactly. Then token, refresh and revoke.
- **Tokens:** a 1-hour access token, and a refresh token that rotates on every use. Reusing a rotated refresh token
  revokes the whole grant. Codes and refresh tokens are stored only as SHA-256 hashes.
- **Gate `/mcp` only.** `RequireAuthMiddleware` wrapped around the whole app refuses `/authorize` too. The runtime wraps
  it in a small ASGI gate that applies it to the MCP path alone (`_BearerGate`). Its `401` carries the
  `WWW-Authenticate` header that sends clients to the protected-resource metadata. Here `/mcp` is a `Mount`, so gate
  the path **and everything under it**, not only an exact match.
- **Both SDK majors:** `BearerAuthBackend.__init__` takes `resource_server_url` on mcp 2.x only; the runtime checks the
  signature with `inspect`. This repo's CI matrix (1.x, 2.x, the 1.22.0 floor) has to cover the auth path too.
- **A public URL setting is the issuer and the resource.** `https://` only (loopback excepted), an origin with no path,
  and the server refuses to start with OAuth on and the URL missing. Its host also feeds a `Host` check
  (`TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=[…])`), which `build_http_app()` does
  not set today.
- **The consent page is server-rendered HTML in the same process**: a CSRF token on the form, no JavaScript. It shows
  the client's **redirect host** prominently beside its name, because a DCR client names itself. **A write scope is
  never pre-ticked.**
- **Open DCR, capped and rate-limited**, with clients that never got a grant pruned after 30 days. A registered client
  alone can do nothing; a token still needs the owner to sign in and approve.

**Does not carry over.** A.I.N.D.Y. has user accounts; this server has one owner and no users. That makes it much
smaller:
- **No multi-user anything.** One owner, one grant per client.
- **Opaque tokens, not JWTs.** The issuer and the resource are the same process, so a random token stored as a SHA-256
  hash with its expiry, scopes and grant is enough, and revocation is instant. There is no signing key to manage. The
  runtime needed JWTs to keep its tokens apart from its session tokens; nothing here has session tokens.
- **Storage in SQLite under `~/.nodus-mcp-server/data/`**, beside `memory.db`: `oauth_clients` and `oauth_grants`.
- **Sign-in is the owner's passphrase** (decision D1 below), not an account. A hash of it is kept under
  `~/.nodus-mcp-server/`. `hashlib.scrypt` keeps this at zero new dependencies; `nodus-auth` (`hash_password`) is the
  ecosystem option if you would rather reuse it, but it brings passlib and a `bcrypt<5` pin.

## 3. The asks

### A1: OAuth mode for `--http` (required)

- **Off by default.** It is a published package and other people run `--http` on loopback today.
- **Turned on by a public URL**, e.g. `--public-url https://nodusmcpserver.ngrok.io` (or an env var). With it, every
  request under `/mcp` needs a valid access token. Starting it refuses without a passphrase set, naming the command
  below.
- **A passphrase command**, e.g. `nodus-mcp-server set-passphrase` (prompted, never an argument, so it stays out of
  shell history). Changing it ends every grant, as A.I.N.D.Y.'s password change does through `token_version`.
- **A grants command**, e.g. `nodus-mcp-server grants list | revoke <id>`. It is the single-user version of
  A.I.N.D.Y.'s Settings, Connected apps.
- **stdio is untouched** (Claude Desktop), and so is `--http` without a public URL.

### A2: scopes per tool (required)

| Scope | Tools | Pre-ticked |
|---|---|---|
| `memory.read` | `nodus_recall` | yes |
| `memory.write` | `nodus_remember`, `nodus_forget` | no |
| `workflow.run` | `nodus_run_goal`, `nodus_run_workflow`, `nodus_resume_workflow` | no |
| `exec` | `nodus_exec` | no (and see D2) |

- `tools/call` refuses a tool whose scope the token lacks, as an error result naming the missing scope, never a crash.
- `tools/list` should list only what the token may call, so a client never plans around a tool it cannot use.
- Both `_SDK_V2` branches get the check. Reading the request's auth inside a handler differs between 1.x and 2.x, so
  find that seam once and test it on both.

### A3: tests, one per abuse case (required)

Taken from the runtime's design §8, minus the multi-user cases:
- `/mcp` without a token, or with an expired or revoked one, gets `401` with `WWW-Authenticate`;
- a reused code, a wrong PKCE verifier, a `plain` challenge, a redirect URI one character off and a foreign `resource`
  are all refused;
- a rotated refresh token used twice revokes the grant;
- a wrong passphrase on the consent page is refused and rate-limited;
- a new passphrase ends every grant;
- a token without `exec` cannot call `nodus_exec`, and does not see it in `tools/list`;
- the OAuth routes stay reachable while `/mcp` is gated;
- without a public URL, `--http` behaves exactly as 0.1.16.

Drive them through the real ASGI app the way `tests/test_http_transport.py` does today, on both SDK majors.

## 4. Decisions for the owner

| # | Decision | Recommended |
|---|---|---|
| D1 | How the owner signs in on the consent page | **A passphrase set locally**, hashed. Not GitHub sign-in or another identity provider: one user doesn't need one, and it would be one more account to keep |
| D2 | `nodus_exec` over the public URL | **Not offered in the first release.** It is the one tool where a sandbox bug is code running on the laptop. Add `exec` later behind its own flag (`--allow-remote-exec`), never pre-ticked |
| D3 | Local HTTP clients while OAuth is on | **They sign in too.** The tunnel forwards to loopback, so "local" cannot be told apart from "through the tunnel" by address. Claude Desktop keeps stdio |
| D4 | Dynamic client registration | **Open, capped and rate-limited**, because claude.ai and ChatGPT both register this way. A closed mode with a register-client command can come later |
| D5 | Version | **0.2.0**: a new opt-in mode, no change without it |

## 5. Learned live on A.I.N.D.Y. (2026-11-11), so it costs nothing here

- **claude.ai:** "Add custom connector", choose **Sign in now** and **Register automatically (DCR)**. It also offers
  CIMD (a client metadata URL), which needs extra support; serve DCR and say so in the README.
- **ChatGPT:** a developer-mode connector with OAuth. It uses `/mcp` (streamable HTTP), not SSE, as this README already
  says.
- **Behind a tunnel every request comes from one address.** A per-IP limit on the consent page becomes one global limit
  (A.I.N.D.Y.'s FR-68). For one owner that is acceptable; say so in the code rather than trusting `X-Forwarded-For`
  from anyone.
- **ngrok's free domains show a browser warning page once** before the consent page ("Visit Site"). A reserved domain
  like `nodusmcpserver.ngrok.io` may not; check it once.
- **The evidence that it works is a real claude.ai connector and a real ChatGPT connector**, each calling `nodus_recall`
  through the public URL. A test client is not that. A.I.N.D.Y. closed its request on exactly that evidence.

## 6. After the release (the owner's laptop, outside this repo)

1. `pipx upgrade nodus-mcp-server`, then `nodus-mcp-server set-passphrase`.
2. Add `--public-url https://nodusmcpserver.ngrok.io` to `~/.nodus-mcp-server/run_http.py`, and restart the
   "nodus-mcp-server HTTP" task.
3. Only then start the ngrok agent for `nodusmcpserver.ngrok.io` again, and update `startup.ps1`'s comment.
4. Connect claude.ai and ChatGPT (§5) and record the evidence.
