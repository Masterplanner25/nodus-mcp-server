"""`--http` mode answers MCP requests (0.1.13).

Through 0.1.12 it did not: 0.1.11 mounted `StreamableHTTPSessionManager` and
never entered its `run()`, so every request to /mcp raised
``RuntimeError: Task group is not initialized`` and the client saw a 500. The
suite could not see it because nothing drove the HTTP app; the two transport
fixes shipped on the strength of the process starting and printing its URL.

This drives the real app the way uvicorn does -- lifespan entered -- and
completes an MCP handshake plus a `tools/list`, which is what ChatGPT does
first. Without the lifespan the first POST is a 500; `test_the_app_needs_its_
lifespan` pins that reading rather than leaving it as lore.
"""
import json

import pytest
from starlette.testclient import TestClient

from nodus_mcp_server import server

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


def _first_json(response):
    """The one JSON-RPC message in a response, whether it came as JSON or SSE."""
    ctype = response.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    raise AssertionError(f"no JSON-RPC message in response: {ctype!r} {response.text[:200]!r}")


def test_initialize_and_list_tools_over_http():
    with TestClient(server.build_http_app()) as client:
        resp = client.post("/mcp/", json=INITIALIZE, headers=HEADERS)
        assert resp.status_code == 200, resp.text
        msg = _first_json(resp)
        assert msg["id"] == 1 and "serverInfo" in msg["result"], msg
        session = resp.headers.get("mcp-session-id")
        assert session, "the server must issue a session id on initialize"

        headers = {**HEADERS, "mcp-session-id": session}
        client.post("/mcp/", headers=headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })
        resp = client.post("/mcp/", headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list",
        })
        assert resp.status_code == 200, resp.text
        names = {t["name"] for t in _first_json(resp)["result"]["tools"]}
        assert {"nodus_remember", "nodus_run_goal", "nodus_run_workflow", "nodus_exec"} <= names, names

        # A tool call *through* the transport. `test_runner.py` drives the
        # runner directly and this file drove the handshake; neither ran a
        # tool over MCP, and the two halves have broken independently
        # (0.1.11, 0.1.13). The product of the two is what a client does.
        resp = client.post("/mcp/", headers=headers, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "nodus_exec", "arguments": {"code": "print(6i * 7i)"}},
        })
        assert resp.status_code == 200, resp.text
        payload = json.loads(_first_json(resp)["result"]["content"][0]["text"])
        assert payload == {"ok": True, "stdout": "42"}, payload


def test_the_app_needs_its_lifespan():
    """What 0.1.11/0.1.12 shipped: the manager mounted but never run."""
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    manager = StreamableHTTPSessionManager(server.app)
    unrun = Starlette(routes=[Mount("/mcp", app=manager.handle_request)])
    with TestClient(unrun, raise_server_exceptions=False) as client:
        resp = client.post("/mcp/", json=INITIALIZE, headers=HEADERS)
    assert resp.status_code == 500


if __name__ == "__main__":
    pytest.main([__file__])
