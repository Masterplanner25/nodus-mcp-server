"""Sign-in for `--http --public-url` (0.2.0): one test per abuse case.

Every test drives the real ASGI app the way `test_http_transport.py` does --
lifespan entered, real requests -- through the full flow a client runs:
register (DCR), `/authorize` with PKCE, the consent page, `/token`, then MCP
over `/mcp` with the bearer token. The suite runs on both SDK majors in CI,
so the auth path is covered on both.
"""
import base64
import hashlib
import json
import re
import secrets
import sqlite3
from contextlib import closing
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from nodus_mcp_server import oauth, server
from nodus_mcp_server.memory_store import MemoryStore

ISSUER = "https://nodus.example"
RESOURCE = ISSUER + "/mcp"
REDIRECT = "https://client.example/callback"
PASSPHRASE = "correct horse battery staple"

MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    oauth.set_passphrase(str(tmp_path), PASSPHRASE)
    # Tool calls must not touch the owner's real memory store.
    monkeypatch.setattr(server, "_store", MemoryStore(str(tmp_path / "data" / "memory.db")))
    return str(tmp_path)


def make_client(home, allow_remote_exec=False):
    cfg = oauth.OAuthConfig(issuer=ISSUER, home=home, allow_remote_exec=allow_remote_exec)
    return TestClient(server.build_http_app(cfg), base_url=ISSUER, follow_redirects=False)


def _first_json(response):
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(response.text[:200])


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def register(client, redirect=REDIRECT):
    resp = client.post("/register", json={
        "redirect_uris": [redirect], "client_name": "Test App",
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["client_id"]


def start_authorize(client, client_id, challenge, *, scope="memory.read", redirect=REDIRECT,
                    resource=RESOURCE, method="S256"):
    params = {
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect,
        "code_challenge": challenge, "code_challenge_method": method, "state": "st8",
        "scope": scope, "resource": resource,
    }
    return client.get("/authorize", params=params)


def open_consent(client, authorize_resp):
    assert authorize_resp.status_code == 302, authorize_resp.text
    loc = urlparse(authorize_resp.headers["location"])
    assert loc.path == oauth.CONSENT_PATH, authorize_resp.headers["location"]
    key = parse_qs(loc.query)["p"][0]
    page = client.get(oauth.CONSENT_PATH, params={"p": key})
    assert page.status_code == 200, page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    return key, csrf, page


def submit_consent(client, key, csrf, scopes, passphrase=PASSPHRASE, action="approve"):
    return client.post(oauth.CONSENT_PATH, data={
        "p": key, "csrf": csrf, "scope": scopes, "passphrase": passphrase, "action": action,
    })


def code_from(resp):
    assert resp.status_code == 302, resp.text
    loc = resp.headers["location"]
    assert loc.startswith(REDIRECT), loc
    q = parse_qs(urlparse(loc).query)
    assert q["state"] == ["st8"]
    return q["code"][0]


def exchange(client, client_id, code, verifier, redirect=REDIRECT):
    return client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
        "client_id": client_id, "code_verifier": verifier, "resource": RESOURCE,
    })


def refresh(client, client_id, refresh_token):
    return client.post("/token", data={
        "grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id,
    })


def sign_in(client, scopes=("memory.read",), requested=None):
    """The whole flow. Returns (client_id, token response JSON)."""
    client_id = register(client)
    verifier, challenge = pkce()
    key, csrf, _ = open_consent(client, start_authorize(client, client_id, challenge,
                                                        scope=requested or " ".join(scopes)))
    code = code_from(submit_consent(client, key, csrf, list(scopes)))
    resp = exchange(client, client_id, code, verifier)
    assert resp.status_code == 200, resp.text
    return client_id, resp.json()


def mcp_session(client, access_token):
    """initialize + initialized; returns headers for further calls."""
    auth = {**MCP_HEADERS, "Authorization": f"Bearer {access_token}"}
    resp = client.post("/mcp/", json=INITIALIZE, headers=auth)
    assert resp.status_code == 200, resp.text
    headers = {**auth, "mcp-session-id": resp.headers["mcp-session-id"]}
    client.post("/mcp/", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return headers


def list_tools(client, headers):
    resp = client.post("/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert resp.status_code == 200, resp.text
    return {t["name"] for t in _first_json(resp)["result"]["tools"]}


def call_tool(client, headers, name, arguments):
    resp = client.post("/mcp/", headers=headers, json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": arguments},
    })
    assert resp.status_code == 200, resp.text
    return json.loads(_first_json(resp)["result"]["content"][0]["text"])


def assert_unauthorized(client, token=None):
    headers = dict(MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = client.post("/mcp/", json=INITIALIZE, headers=headers)
    assert resp.status_code == 401, resp.text
    www = resp.headers["www-authenticate"]
    assert www.startswith("Bearer ") and "resource_metadata=" in www, www
    assert "/.well-known/oauth-protected-resource/mcp" in www, www


# ── the happy path ────────────────────────────────────────────────────────────


def test_full_flow_then_tools_over_mcp(home):
    with make_client(home) as client:
        _, tokens = sign_in(client, scopes=("memory.read", "memory.write"))
        assert tokens["token_type"].lower() == "bearer" and tokens["expires_in"] == 3600
        assert set(tokens["scope"].split()) == {"memory.read", "memory.write"}
        headers = mcp_session(client, tokens["access_token"])
        assert list_tools(client, headers) == {"nodus_recall", "nodus_remember", "nodus_forget"}
        stored = call_tool(client, headers, "nodus_remember", {"content": "oauth works", "tags": ["t"]})
        assert stored.get("id"), stored
        found = call_tool(client, headers, "nodus_recall", {"query": "oauth"})
        assert found["count"] == 1, found


def test_oauth_routes_stay_reachable_while_mcp_is_gated(home):
    with make_client(home) as client:
        assert_unauthorized(client)
        # Without the trailing slash too: the gate covers the mount and everything under it.
        assert client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS).status_code == 401
        meta = client.get("/.well-known/oauth-authorization-server")
        assert meta.status_code == 200 and meta.json()["issuer"].rstrip("/") == ISSUER
        assert meta.json()["code_challenge_methods_supported"] == ["S256"]
        prm = client.get("/.well-known/oauth-protected-resource/mcp")
        assert prm.status_code == 200, prm.text
        assert prm.json()["resource"].rstrip("/") == RESOURCE
        assert "exec" not in prm.json()["scopes_supported"]


# ── tokens on /mcp ────────────────────────────────────────────────────────────


def test_expired_access_token_is_refused(home):
    with make_client(home) as client:
        _, tokens = sign_in(client)
        mcp_session(client, tokens["access_token"])
        with closing(sqlite3.connect(oauth.OAuthConfig(ISSUER, home).db_path)) as db, db:
            db.execute("UPDATE oauth_tokens SET expires_at = 0 WHERE kind = 'access'")
        assert_unauthorized(client, tokens["access_token"])


def test_revoked_token_is_refused(home):
    with make_client(home) as client:
        client_id, tokens = sign_in(client)
        mcp_session(client, tokens["access_token"])
        # mcp 1.x's RevocationRequest declares `client_secret: str | None` with no
        # default, so pydantic requires the field even from a public client.
        resp = client.post("/revoke", data={
            "token": tokens["access_token"], "client_id": client_id, "client_secret": "",
        })
        assert resp.status_code == 200, resp.text
        assert_unauthorized(client, tokens["access_token"])
        assert refresh(client, client_id, tokens["refresh_token"]).status_code == 400


def test_garbage_token_is_refused(home):
    with make_client(home) as client:
        assert_unauthorized(client, "not-a-token")


# ── the authorization request ─────────────────────────────────────────────────


def test_reused_code_is_refused_and_revokes_the_grant(home):
    with make_client(home) as client:
        client_id = register(client)
        verifier, challenge = pkce()
        key, csrf, _ = open_consent(client, start_authorize(client, client_id, challenge))
        code = code_from(submit_consent(client, key, csrf, ["memory.read"]))
        first = exchange(client, client_id, code, verifier)
        assert first.status_code == 200, first.text
        second = exchange(client, client_id, code, verifier)
        assert second.status_code == 400 and second.json()["error"] == "invalid_grant", second.text
        assert_unauthorized(client, first.json()["access_token"])


def test_wrong_pkce_verifier_is_refused(home):
    with make_client(home) as client:
        client_id = register(client)
        _, challenge = pkce()
        key, csrf, _ = open_consent(client, start_authorize(client, client_id, challenge))
        code = code_from(submit_consent(client, key, csrf, ["memory.read"]))
        resp = exchange(client, client_id, code, "x" * 50)
        assert resp.status_code == 400 and resp.json()["error"] == "invalid_grant", resp.text


def test_plain_pkce_is_refused(home):
    with make_client(home) as client:
        client_id = register(client)
        verifier = secrets.token_urlsafe(48)
        resp = start_authorize(client, client_id, verifier, method="plain")
        assert oauth.CONSENT_PATH not in resp.headers.get("location", ""), resp.headers
        assert resp.status_code in (302, 400)
        if resp.status_code == 302:
            assert "error=" in resp.headers["location"]


def test_redirect_uri_one_character_off_is_refused(home):
    with make_client(home) as client:
        client_id = register(client)
        _, challenge = pkce()
        resp = start_authorize(client, client_id, challenge, redirect=REDIRECT + "x")
        assert resp.status_code == 400, resp.text
        assert "location" not in resp.headers  # never redirect to an unregistered URI


def test_foreign_resource_is_refused(home):
    with make_client(home) as client:
        client_id = register(client)
        _, challenge = pkce()
        resp = start_authorize(client, client_id, challenge, resource="https://elsewhere.example/mcp")
        loc = resp.headers.get("location", "")
        assert oauth.CONSENT_PATH not in loc and "error=" in loc, (resp.status_code, loc)


def test_registration_refuses_a_plain_http_redirect(home):
    with make_client(home) as client:
        resp = client.post("/register", json={
            "redirect_uris": ["http://attacker.example/cb"], "token_endpoint_auth_method": "none",
        })
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"] == "invalid_redirect_uri"


def test_deny_sends_access_denied(home):
    with make_client(home) as client:
        client_id = register(client)
        _, challenge = pkce()
        key, csrf, _ = open_consent(client, start_authorize(client, client_id, challenge))
        resp = submit_consent(client, key, csrf, [], passphrase="", action="deny")
        assert resp.status_code == 302
        q = parse_qs(urlparse(resp.headers["location"]).query)
        assert q["error"] == ["access_denied"] and q["state"] == ["st8"]


def test_consent_post_without_the_csrf_cookie_is_refused(home):
    with make_client(home) as client:
        client_id = register(client)
        _, challenge = pkce()
        key, csrf, _ = open_consent(client, start_authorize(client, client_id, challenge))
        client.cookies.clear()
        resp = submit_consent(client, key, csrf, ["memory.read"])
        assert resp.status_code == 400 and "location" not in resp.headers


def test_consent_page_lets_its_form_redirect_to_the_approved_app_only(home):
    # Chrome applies form-action to the redirect after a submit: 'self' alone blocked the 302 to claude.ai.
    with make_client(home) as client:
        client_id = register(client)
        _, challenge = pkce()
        _, _, page = open_consent(client, start_authorize(client, client_id, challenge))
        csp = page.headers["content-security-policy"]
        origin = "{0.scheme}://{0.netloc}".format(urlparse(REDIRECT))
        assert f"form-action 'self' {origin};" in csp, csp
        assert "frame-ancestors 'none'" in csp and "default-src 'none'" in csp


def test_a_redirect_origin_cannot_widen_the_policy():
    headers = oauth._consent_page_headers("https://evil.example; script-src *")
    assert "script-src" not in headers["Content-Security-Policy"]
    assert "form-action 'self';" in headers["Content-Security-Policy"]


def test_consent_page_shows_the_redirect_host_and_never_pre_ticks_a_write(home):
    with make_client(home) as client:
        client_id = register(client)
        _, challenge = pkce()
        _, _, page = open_consent(client, start_authorize(
            client, client_id, challenge, scope="memory.read memory.write workflow.run"))
        assert "client.example" in page.text
        assert re.search(r'value="memory.read" checked', page.text)
        assert not re.search(r'value="memory.write" checked', page.text)
        assert not re.search(r'value="workflow.run" checked', page.text)
        assert page.headers["x-frame-options"] == "DENY"


# ── the passphrase ────────────────────────────────────────────────────────────


def test_wrong_passphrase_is_refused_and_rate_limited(home):
    with make_client(home) as client:
        client_id = register(client)
        _, challenge = pkce()
        key, csrf, _ = open_consent(client, start_authorize(client, client_id, challenge))
        for _ in range(oauth.CONSENT_ATTEMPTS_PER_MINUTE):
            resp = submit_consent(client, key, csrf, ["memory.read"], passphrase="wrong passphrase!")
            assert resp.status_code == 401 and "location" not in resp.headers
        # Over the limit even the right passphrase waits.
        resp = submit_consent(client, key, csrf, ["memory.read"])
        assert resp.status_code == 429


def test_a_new_passphrase_ends_every_grant(home):
    with make_client(home) as client:
        client_id, tokens = sign_in(client)
        mcp_session(client, tokens["access_token"])
        assert oauth.set_passphrase(home, "a whole new passphrase") == 1
        assert_unauthorized(client, tokens["access_token"])
        assert refresh(client, client_id, tokens["refresh_token"]).status_code == 400


def test_short_passphrase_is_refused(tmp_path):
    with pytest.raises(ValueError):
        oauth.set_passphrase(str(tmp_path), "short")
    assert not oauth.passphrase_is_set(str(tmp_path))


# ── refresh ───────────────────────────────────────────────────────────────────


def test_refresh_rotates_and_a_replay_revokes_the_grant(home):
    with make_client(home) as client:
        client_id, tokens = sign_in(client)
        first = refresh(client, client_id, tokens["refresh_token"])
        assert first.status_code == 200, first.text
        new = first.json()
        assert new["refresh_token"] != tokens["refresh_token"]
        mcp_session(client, new["access_token"])
        replay = refresh(client, client_id, tokens["refresh_token"])
        assert replay.status_code == 400, replay.text
        # The replay ended the grant: the rotated-in tokens die with it.
        assert_unauthorized(client, new["access_token"])
        assert refresh(client, client_id, new["refresh_token"]).status_code == 400


# ── scopes per tool ───────────────────────────────────────────────────────────


def test_read_only_token_cannot_write(home):
    with make_client(home) as client:
        _, tokens = sign_in(client, scopes=("memory.read",), requested="memory.read memory.write")
        assert tokens["scope"] == "memory.read"  # the owner ticked only read
        headers = mcp_session(client, tokens["access_token"])
        assert list_tools(client, headers) == {"nodus_recall"}
        result = call_tool(client, headers, "nodus_remember", {"content": "x"})
        assert "memory.write" in result["error"], result


def test_exec_is_not_offered_without_allow_remote_exec(home):
    with make_client(home) as client:
        # Asking for it is refused outright: it was never a scope a client could register for.
        client_id = register(client)
        _, challenge = pkce()
        resp = start_authorize(client, client_id, challenge, scope="memory.read exec")
        loc = resp.headers.get("location", "")
        assert oauth.CONSENT_PATH not in loc and "error=invalid_scope" in loc, loc

        _, tokens = sign_in(client, scopes=("memory.read", "workflow.run"))
        headers = mcp_session(client, tokens["access_token"])
        names = list_tools(client, headers)
        assert "nodus_exec" not in names and "nodus_run_goal" in names
        result = call_tool(client, headers, "nodus_exec", {"code": "print(1)"})
        assert "exec" in result["error"] and "stdout" not in result, result


def test_exec_with_allow_remote_exec_and_the_scope(home):
    with make_client(home, allow_remote_exec=True) as client:
        _, tokens = sign_in(client, scopes=("exec",))
        headers = mcp_session(client, tokens["access_token"])
        assert list_tools(client, headers) == {"nodus_exec"}
        assert call_tool(client, headers, "nodus_exec", {"code": "print(6i * 7i)"}) == {"ok": True, "stdout": "42"}


def test_dropping_allow_remote_exec_takes_exec_from_issued_tokens(home):
    with make_client(home, allow_remote_exec=True) as client:
        _, tokens = sign_in(client, scopes=("memory.read", "exec"))
    with make_client(home, allow_remote_exec=False) as client:
        headers = mcp_session(client, tokens["access_token"])
        assert list_tools(client, headers) == {"nodus_recall"}


# ── grants CLI ────────────────────────────────────────────────────────────────


def test_grants_list_and_revoke(home, capsys):
    with make_client(home) as client:
        _, tokens = sign_in(client)
        grants = oauth.OAuthStore(oauth.OAuthConfig(ISSUER, home).db_path).list_grants()
        assert len(grants) == 1 and grants[0]["client_name"] == "Test App"
        assert grants[0]["redirect_host"] == "client.example"
        assert oauth.cli(["grants", "list"], home=home) == 0
        assert grants[0]["grant_id"] in capsys.readouterr().out
        assert oauth.cli(["grants", "revoke", grants[0]["grant_id"]], home=home) == 0
        assert_unauthorized(client, tokens["access_token"])
        assert oauth.cli(["grants", "revoke", grants[0]["grant_id"]], home=home) == 1


def test_a_new_consent_replaces_the_clients_earlier_grant(home):
    with make_client(home) as client:
        client_id, first = sign_in(client)
        verifier, challenge = pkce()
        key, csrf, _ = open_consent(client, start_authorize(client, client_id, challenge))
        code = code_from(submit_consent(client, key, csrf, ["memory.read"]))
        assert exchange(client, client_id, code, verifier).status_code == 200
        assert_unauthorized(client, first["access_token"])


# ── configuration ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "http://nodus.example", "https://nodus.example/mcp", "nodus.example", "https://nodus.example?x=1",
])
def test_bad_public_urls_are_refused(url):
    with pytest.raises(ValueError):
        oauth.normalize_public_url(url)


def test_public_url_normalizes():
    assert oauth.normalize_public_url("https://nodus.example/") == ISSUER
    assert oauth.normalize_public_url("http://127.0.0.1:8765") == "http://127.0.0.1:8765"


def test_without_a_public_url_http_is_unchanged():
    with TestClient(server.build_http_app()) as client:
        assert client.get("/.well-known/oauth-authorization-server").status_code == 404
        resp = client.post("/mcp/", json=INITIALIZE, headers=MCP_HEADERS)
        assert resp.status_code == 200, resp.text


def test_host_check_refuses_a_foreign_host(home):
    cfg = oauth.OAuthConfig(issuer=ISSUER, home=home)
    with TestClient(server.build_http_app(cfg), base_url="https://evil.example", follow_redirects=False) as client:
        _, tokens = sign_in(client)
        resp = client.post("/mcp/", json=INITIALIZE,
                           headers={**MCP_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"})
        assert resp.status_code in (400, 421), resp.text
