"""Nodus MCP Server — exposes Nodus goals, workflows, and memory as MCP tools."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp import types

from nodus.runtime.embedding import NodusRuntime
from . import __version__, oauth, runner
from .memory_store import MemoryStore

_DATA_DIR = os.path.join(os.path.expanduser("~"), ".nodus-mcp-server", "data")
os.makedirs(_DATA_DIR, exist_ok=True)

# Claude Desktop spawns servers with CWD=system32 on Windows, which is not writable.
# Any nodus-lang path that resolves .nodus relative to CWD (task_graph, snapshots)
# will fail with WinError 5. Anchor CWD to the user's home dir at startup.
os.chdir(os.path.expanduser("~"))

_runtime = NodusRuntime(timeout_ms=None, max_steps=None, allowed_paths=[])
_exec_runtime = NodusRuntime(
    timeout_ms=None,
    max_steps=None,
    allowed_paths=[],
    allow_network=False,
    allow_subprocess=False,
    allow_env=False,
)
_store = MemoryStore(os.path.join(_DATA_DIR, "memory.db"))

# Without a version the SDK reports its own, so clients saw mcp's version here.
app = Server("nodus-mcp-server", version=__version__)

_TOOLS = [
    types.Tool(
        name="nodus_remember",
        description=(
            "Store a piece of information in persistent memory. "
            "Supply optional tags to make it easier to recall later."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The information to store."},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags for filtering on recall.",
                },
            },
            "required": ["content"],
        },
    ),
    types.Tool(
        name="nodus_recall",
        description=(
            "Search persistent memory. Filter by free-text query and/or tags. "
            "Returns up to `limit` matching entries (default 5, max 20)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Full-text search string."},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Only return memories that have at least one of these tags.",
                },
                "limit": {"type": "integer", "description": "Max results to return (default 5)."},
            },
        },
    ),
    types.Tool(
        name="nodus_forget",
        description="Remove a memory entry by its ID (returned by nodus.remember).",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The memory ID to delete."},
            },
            "required": ["id"],
        },
    ),
    types.Tool(
        name="nodus_run_goal",
        description=(
            "Run a single-shot Nodus goal to completion. "
            "Goals execute a fixed sequence of named steps and return all step results at once — "
            "use this when the task runs straight through with no need to pause or resume. "
            "Built-in goals: 'summarize' (params: {text}), 'pipeline' (params: {items, label})."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Goal name (e.g. 'summarize')."},
                "params": {"type": "object", "description": "Input variables for the goal."},
            },
            "required": ["name"],
        },
    ),
    types.Tool(
        name="nodus_run_workflow",
        description=(
            "Start a resumable Nodus workflow. "
            "Workflows execute step-by-step and save a checkpoint after each step — "
            "use this when you need to pause mid-execution, inspect intermediate results, "
            "or resume later from a known point. "
            "Returns a graph_id; pass it to nodus_resume_workflow to continue from any checkpoint. "
            "Built-in workflows: 'research' (params: {topic})."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Workflow name (e.g. 'research')."},
                "params": {"type": "object", "description": "Input variables for the workflow."},
            },
            "required": ["name"],
        },
    ),
    types.Tool(
        name="nodus_resume_workflow",
        description=(
            "Resume a previously started Nodus workflow from a checkpoint. "
            "Skips already-completed steps and re-runs from the checkpoint forward. "
            "Use this to continue a paused workflow, retry a failed step, or hand off "
            "execution after reviewing intermediate results. "
            "Pass the graph_id returned by nodus_run_workflow and optionally a checkpoint label."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "graph_id": {"type": "string", "description": "The graph_id returned by nodus_run_workflow."},
                "checkpoint": {"type": "string", "description": "Checkpoint label to resume from (optional)."},
            },
            "required": ["graph_id"],
        },
    ),
    types.Tool(
        name="nodus_exec",
        description=(
            "Execute arbitrary Nodus (.nd) code in a sandboxed runtime "
            "(no file I/O, no network, no subprocess, no environment variables, 10s timeout). "
            "Output is captured via print() — use print(value) to surface results. "
            "Top-level return is not supported; use print() instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Nodus source code to execute."},
            },
            "required": ["code"],
        },
    ),
]


def _dispatch(name: str, arguments: dict | None, scopes: frozenset | None = None) -> list[types.TextContent]:
    """One tool call -> text content. Transport- and SDK-agnostic.

    `scopes` is None without OAuth (stdio, or `--http` with no public URL), and
    the calling token's scopes with it: then a tool its scopes do not cover is
    refused as a result naming the missing scope, never run.
    """
    if scopes is not None and not oauth.tool_allowed(name, scopes):
        needed = oauth.TOOL_SCOPES.get(name)
        error = (f"{name} needs the {needed!r} scope, which this app was not granted"
                 if needed else f"Unknown tool: {name}")
        return [types.TextContent(type="text", text=json.dumps({"error": error}))]
    handlers = {
        "nodus_remember": _remember,
        "nodus_recall": _recall,
        "nodus_forget": _forget,
        "nodus_run_goal": _run_goal,
        "nodus_run_workflow": _run_workflow,
        "nodus_resume_workflow": _resume_workflow,
        "nodus_exec": _exec,
    }
    handler = handlers.get(name)
    if handler is None:
        result: dict = {"error": f"Unknown tool: {name}"}
    else:
        result = handler(dict(arguments or {}))
    return [types.TextContent(type="text", text=json.dumps(result))]


#: Which SDK generation is installed (#3). mcp 2.0 removed the decorator
#: registration (`@app.list_tools()` / `@app.call_tool()`); a handler is
#: registered with `add_request_handler(method, params_type, handler)`, takes
#: `(ctx, params)` and returns a result model. One question, answered once;
#: both branches are driven by the same tests through a real client
#: (`tests/test_http_transport.py`), and the same port as nodus-mcp#11 so the
#: two servers do not drift.
_SDK_V2 = hasattr(Server, "add_request_handler")

def _request_scopes(request) -> frozenset | None:
    """The OAuth scopes `oauth.BearerGate` put on this HTTP request, or None without OAuth."""
    scope = getattr(request, "scope", None)
    return scope.get(oauth.SCOPE_KEY) if isinstance(scope, dict) else None


def _visible_tools(scopes: frozenset | None) -> list[types.Tool]:
    """A client never sees a tool it may not call, so it never plans around one."""
    if scopes is None:
        return _TOOLS
    return [t for t in _TOOLS if oauth.tool_allowed(t.name, scopes)]


if _SDK_V2:

    async def _list_tools_v2(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=_visible_tools(_request_scopes(ctx.request)))

    async def _call_tool_v2(ctx, params) -> types.CallToolResult:
        scopes = _request_scopes(ctx.request)
        return types.CallToolResult(content=_dispatch(params.name, params.arguments, scopes))

    app.add_request_handler("tools/list", types.PaginatedRequestParams, _list_tools_v2)
    app.add_request_handler("tools/call", types.CallToolRequestParams, _call_tool_v2)

else:

    def _current_scopes() -> frozenset | None:
        try:
            return _request_scopes(app.request_context.request)
        except LookupError:  # no request in flight
            return None

    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        return _visible_tools(_current_scopes())

    @app.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        return _dispatch(name, arguments, _current_scopes())


# ── Tool handlers ─────────────────────────────────────────────────────────────

def _remember(args: dict) -> dict:
    content = str(args.get("content") or "")
    raw_tags = args.get("tags") or []
    if isinstance(raw_tags, str):
        raw_tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
    return _store.remember(content, list(raw_tags))


def _recall(args: dict) -> dict:
    query = str(args.get("query") or "")
    raw_tags = args.get("tags") or []
    if isinstance(raw_tags, str):
        raw_tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
    limit = min(int(args.get("limit") or 5), 20)
    memories = _store.recall(query, list(raw_tags), limit)
    return {"count": len(memories), "memories": memories}


def _forget(args: dict) -> dict:
    return _store.forget(str(args.get("id") or ""))


def _run_goal(args: dict) -> dict:
    name = str(args.get("name") or "")
    params = dict(args.get("params") or {})
    if not name:
        return {"ok": False, "error": "name is required"}
    return runner.run_goal(_runtime, name, params)


def _run_workflow(args: dict) -> dict:
    name = str(args.get("name") or "")
    params = dict(args.get("params") or {})
    if not name:
        return {"ok": False, "error": "name is required"}
    return runner.run_workflow(_runtime, name, params)


def _resume_workflow(args: dict) -> dict:
    graph_id = str(args.get("graph_id") or "")
    checkpoint = args.get("checkpoint") or None
    if checkpoint is not None:
        checkpoint = str(checkpoint)
    if not graph_id:
        return {"ok": False, "error": "graph_id is required"}
    return runner.resume_workflow_tool(graph_id, checkpoint)


def _exec(args: dict) -> dict:
    code = str(args.get("code") or "")
    if not code.strip():
        return {"ok": False, "error": "code is required"}
    return runner.exec_code(_exec_runtime, code)


# ── Entry point ───────────────────────────────────────────────────────────────

async def _serve_stdio() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def build_http_app(oauth_cfg: "oauth.OAuthConfig | None" = None):
    """The ASGI app `--http` serves: the MCP session manager mounted at /mcp.

    `StreamableHTTPSessionManager` must be *running* -- its task group is
    created inside `run()` -- or every request fails with
    ``RuntimeError: Task group is not initialized. Make sure to use run()``.
    0.1.11 swapped this manager in to fix a `TypeError` on every request and
    never entered `run()`, so every request still failed, differently. The
    manager's lifetime is the app's lifespan now, which is the one place uvicorn
    (and Starlette's `TestClient`) will start and stop it.

    With `oauth_cfg` (a public URL), the server is also its own OAuth
    authorization server and `/mcp` needs an access token (`oauth.py`). Without
    it the app is exactly what 0.1.16 served.
    """
    from contextlib import asynccontextmanager

    from starlette.applications import Starlette
    from starlette.routing import Mount

    routes: list = []
    middleware: list = []
    security = None
    if oauth_cfg is not None:
        provider = oauth.NodusOAuthProvider(oauth_cfg)
        routes, middleware = oauth.routes_and_middleware(provider)
        security = oauth.transport_security(oauth_cfg)

    session_manager = StreamableHTTPSessionManager(app, security_settings=security)

    @asynccontextmanager
    async def lifespan(_app):
        async with session_manager.run():
            yield

    return Starlette(
        routes=[
            *routes,
            Mount("/mcp", app=session_manager.handle_request),
        ],
        middleware=middleware,
        lifespan=lifespan,
    )


async def _serve_http(host: str, port: int, ssl_cert: str | None, ssl_key: str | None,
                      oauth_cfg: "oauth.OAuthConfig | None" = None) -> None:
    import uvicorn

    starlette_app = build_http_app(oauth_cfg)

    scheme = "https" if ssl_cert else "http"
    url = f"{scheme}://{host}:{port}/mcp"
    print(f"[nodus-mcp-server] HTTP listening on {url}", file=sys.stderr)
    if oauth_cfg is not None:
        print(f"[nodus-mcp-server] sign-in on; clients connect to {oauth_cfg.resource} "
              f"(scopes offered: {', '.join(oauth_cfg.offered_scopes)})", file=sys.stderr)
    else:
        print("[nodus-mcp-server] no sign-in: anything that reaches this URL can call every tool. "
              "ChatGPT and claude.ai need a public https URL; see --public-url.", file=sys.stderr)

    config = uvicorn.Config(
        starlette_app,
        host=host,
        port=port,
        log_level="warning",
        ssl_certfile=ssl_cert or None,
        ssl_keyfile=ssl_key or None,
    )
    server = uvicorn.Server(config)
    await server.serve()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in {"set-passphrase", "grants"}:
        sys.exit(oauth.cli(sys.argv[1:]))

    parser = argparse.ArgumentParser(
        prog="nodus-mcp-server",
        description="Nodus MCP Server — memory, goals, workflows, and sandboxed execution over MCP",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--stdio", action="store_true",
                       help="Serve on stdin/stdout (Claude Desktop spawned-child mode)")
    group.add_argument("--http", action="store_true",
                       help="Serve streamable HTTP at /mcp (ChatGPT, claude.ai and other HTTP MCP clients)")
    parser.add_argument("--port", type=int, default=8765,
                        help="HTTP port (default: 8765)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="HTTP host (default: 127.0.0.1)")
    parser.add_argument("--ssl-cert", default="",
                        help="Path to SSL certificate file (enables HTTPS)")
    parser.add_argument("--ssl-key", default="",
                        help="Path to SSL private key file")
    parser.add_argument("--public-url", default=os.environ.get("NODUS_MCP_PUBLIC_URL", ""),
                        help="The https origin clients reach this server at (e.g. a tunnel). "
                             "Turns on sign-in: /mcp then needs an OAuth token. "
                             "Also NODUS_MCP_PUBLIC_URL. Run `nodus-mcp-server set-passphrase` first.")
    parser.add_argument("--allow-remote-exec", action="store_true",
                        help="With --public-url, also offer the `exec` scope (nodus_exec). Off by default.")
    args = parser.parse_args()

    if args.stdio:
        print("[nodus-mcp-server] serving on stdio", file=sys.stderr)
        asyncio.run(_serve_stdio())
    else:
        ssl_cert = args.ssl_cert or None
        ssl_key = args.ssl_key or None
        oauth_cfg = None
        if args.public_url:
            try:
                issuer = oauth.normalize_public_url(args.public_url)
            except ValueError as exc:
                parser.error(str(exc))
            home = oauth.default_home()
            if not oauth.passphrase_is_set(home):
                parser.error("sign-in needs a passphrase. Run `nodus-mcp-server set-passphrase` first.")
            oauth_cfg = oauth.OAuthConfig(issuer=issuer, home=home, allow_remote_exec=args.allow_remote_exec)
        elif args.allow_remote_exec:
            parser.error("--allow-remote-exec only means something with --public-url")
        asyncio.run(_serve_http(args.host, args.port, ssl_cert, ssl_key, oauth_cfg))


if __name__ == "__main__":
    main()
