"""Nodus MCP Server — exposes Nodus goals, workflows, and memory as MCP tools."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.sse import SseServerTransport
from mcp import types

from nodus.runtime.embedding import NodusRuntime
from .memory_store import MemoryStore
from . import runner

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
)
_store = MemoryStore(os.path.join(_DATA_DIR, "memory.db"))

app = Server("nodus-mcp-server")

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
            "Execute a pre-defined Nodus goal by name. "
            "Built-in goals: 'summarize' (params: {text}), 'pipeline' (params: {items, label}). "
            "Returns structured step results."
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
            "Execute a pre-defined Nodus workflow by name. "
            "Built-in workflows: 'research' (params: {topic}). "
            "Returns graph_id which can be passed to nodus_resume_workflow to resume from a checkpoint."
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
            "Resume a Nodus workflow from a checkpoint. "
            "Pass the graph_id returned by nodus_run_workflow and optionally a checkpoint label. "
            "Skips already-completed steps and re-runs from the checkpoint."
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
            "(no file I/O, no network, no subprocess, 10s timeout). "
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


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return _TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
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
        result = handler(arguments)
    return [types.TextContent(type="text", text=json.dumps(result))]


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


async def _serve_http(host: str, port: int) -> None:
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route

    sse = SseServerTransport("/messages/")

    async def handle_sse(request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await app.run(
                streams[0], streams[1], app.create_initialization_options()
            )

    starlette_app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ]
    )

    url = f"http://{host}:{port}/sse"
    print(f"[nodus-mcp-server] HTTP/SSE listening on {url}", file=sys.stderr)
    print(f"[nodus-mcp-server] Point ChatGPT / your MCP client at: {url}", file=sys.stderr)

    config = uvicorn.Config(starlette_app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nodus-mcp-server",
        description="Nodus MCP Server — memory, goals, workflows, and sandboxed execution over MCP",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--stdio", action="store_true",
                       help="Serve on stdin/stdout (Claude Desktop spawned-child mode)")
    group.add_argument("--http", action="store_true",
                       help="Serve over HTTP/SSE (ChatGPT Desktop and other HTTP MCP clients)")
    parser.add_argument("--port", type=int, default=8765,
                        help="HTTP port (default: 8765)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="HTTP host (default: 127.0.0.1)")
    args = parser.parse_args()

    if args.stdio:
        print("[nodus-mcp-server] serving on stdio", file=sys.stderr)
        asyncio.run(_serve_stdio())
    else:
        asyncio.run(_serve_http(args.host, args.port))


if __name__ == "__main__":
    main()
