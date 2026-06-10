"""Nodus MCP Server — exposes Nodus goals, workflows, and memory as MCP tools."""
from __future__ import annotations

import argparse
import os
import sys

from nodus.runtime.embedding import NodusRuntime
from nodus_mcp import McpServer, StdioServerTransport, HttpServerTransport

from .memory_store import MemoryStore
from . import runner

# ── Data directory ────────────────────────────────────────────────────────────
# Stored in the user's home directory so it survives upgrades and pip reinstalls.

_DATA_DIR = os.path.join(os.path.expanduser("~"), ".nodus-mcp-server", "data")
os.makedirs(_DATA_DIR, exist_ok=True)

# ── Runtime ──────────────────────────────────────────────────────────────────
# timeout_ms=None, max_steps=None: no wall-clock or step limit for the server
# process. Individual tool handlers apply their own per-call timeouts.
# allowed_paths=[]: .nd scripts cannot perform file I/O — sandbox is tight.

_runtime = NodusRuntime(timeout_ms=None, max_steps=None, allowed_paths=[])

# ── Memory store ─────────────────────────────────────────────────────────────

_store = MemoryStore(os.path.join(_DATA_DIR, "memory.db"))

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
    try:
        return runner.run_goal(_runtime, name, params)
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc)}


def _run_workflow(args: dict) -> dict:
    name = str(args.get("name") or "")
    params = dict(args.get("params") or {})
    if not name:
        return {"ok": False, "error": "name is required"}
    try:
        return runner.run_workflow(_runtime, name, params)
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc)}


def _exec(args: dict) -> dict:
    code = str(args.get("code") or "")
    if not code.strip():
        return {"ok": False, "error": "code is required"}
    return runner.exec_code(_runtime, code)


# ── Tool registration ─────────────────────────────────────────────────────────

_runtime.tool_registry.register({
    "name": "nodus.remember",
    "description": (
        "Store a piece of information in persistent memory. "
        "Supply optional tags to make it easier to recall later."
    ),
    "handler": _remember,
    "schema": {
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
})

_runtime.tool_registry.register({
    "name": "nodus.recall",
    "description": (
        "Search persistent memory. Filter by free-text query and/or tags. "
        "Returns up to `limit` matching entries (default 5, max 20)."
    ),
    "handler": _recall,
    "schema": {
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
})

_runtime.tool_registry.register({
    "name": "nodus.forget",
    "description": "Remove a memory entry by its ID (returned by nodus.remember).",
    "handler": _forget,
    "schema": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "The memory ID to delete."},
        },
        "required": ["id"],
    },
})

_runtime.tool_registry.register({
    "name": "nodus.run_goal",
    "description": (
        "Execute a pre-defined Nodus goal by name. "
        "Built-in goals: 'summarize' (params: {text}), 'pipeline' (params: {items, label}). "
        "Returns structured step results."
    ),
    "handler": _run_goal,
    "schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Goal name (e.g. 'summarize')."},
            "params": {"type": "object", "description": "Input variables for the goal."},
        },
        "required": ["name"],
    },
})

_runtime.tool_registry.register({
    "name": "nodus.run_workflow",
    "description": (
        "Execute a pre-defined Nodus workflow by name. "
        "Built-in workflows: 'research' (params: {topic}). "
        "Workflows support checkpoint/resume for durable multi-step execution."
    ),
    "handler": _run_workflow,
    "schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Workflow name (e.g. 'research')."},
            "params": {"type": "object", "description": "Input variables for the workflow."},
        },
        "required": ["name"],
    },
})

_runtime.tool_registry.register({
    "name": "nodus.exec",
    "description": (
        "Execute arbitrary Nodus (.nd) code in a sandboxed runtime "
        "(no file I/O, no network, 10s timeout). "
        "Returns the final expression value and any stdout."
    ),
    "handler": _exec,
    "schema": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Nodus source code to execute."},
        },
        "required": ["code"],
    },
})

# ── Server ────────────────────────────────────────────────────────────────────

_server = McpServer(runtime=_runtime)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nodus-mcp-server",
        description="Nodus MCP Server — memory, goals, workflows, and sandboxed execution over MCP",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--stdio", action="store_true",
                       help="Serve on stdin/stdout (Claude Desktop spawned-child mode)")
    group.add_argument("--http", action="store_true",
                       help="Serve on HTTP")
    parser.add_argument("--port", type=int, default=8080,
                        help="HTTP port (default: 8080)")
    parser.add_argument("--bearer-token", metavar="TOKEN",
                        help="Require this bearer token on all inbound HTTP requests")
    args = parser.parse_args()

    if args.stdio:
        print("[nodus-mcp-server] serving on stdio", file=sys.stderr)
        transport = StdioServerTransport()
        transport.serve(_server.dispatch)
    else:
        port = args.port
        bearer = args.bearer_token
        print(f"[nodus-mcp-server] serving on http://localhost:{port}", file=sys.stderr)
        if bearer:
            print("[nodus-mcp-server] bearer auth enabled", file=sys.stderr)
        transport = HttpServerTransport("localhost", port, bearer_token=bearer)
        try:
            transport.serve(_server.dispatch)
        except KeyboardInterrupt:
            transport.close()


if __name__ == "__main__":
    main()
