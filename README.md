# nodus-mcp-server

> **Status:** published on [PyPI](https://pypi.org/project/nodus-mcp-server/).

An MCP server that connects AI assistants to the [Nodus](https://github.com/Masterplanner25/Nodus) language runtime — giving them persistent memory, sandboxed code execution, and checkpoint/resume orchestration workflows, all powered by `.nd` scripts running on the Nodus VM.

Supports **Claude Desktop** (stdio) and **ChatGPT Desktop** (streamable HTTP at `/mcp`, through a tunnel).

## Tools

| Tool | What it does |
|------|-------------|
| `nodus_remember` | Store a fact in persistent memory with optional tags |
| `nodus_recall` | Search memory by free-text query and/or tags |
| `nodus_forget` | Delete a memory entry by ID |
| `nodus_run_goal` | Run a built-in Nodus goal (structured multi-step result) |
| `nodus_run_workflow` | Run a built-in Nodus workflow (returns a `graph_id` for resuming) |
| `nodus_resume_workflow` | Resume a workflow from a checkpoint using its `graph_id` |
| `nodus_exec` | Execute arbitrary Nodus code in a sandbox (no file I/O, no network, no subprocess, no environment variables, 10 s timeout) |

## Requirements

- Python ≥ 3.10
- [pipx](https://pipx.pypa.io/) (recommended — keeps the server in its own isolated environment)
- Claude Desktop **or** ChatGPT Desktop (the downloadable apps, not browser versions)

## Install

```
pipx install nodus-mcp-server
```

## Claude Desktop setup

### 1. Find your config file

| Setup | Config path |
|-------|-------------|
| Standard install | `%APPDATA%\Claude\claude_desktop_config.json` |
| Windows Store app | `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json` |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |

### 2. Add the server

```json
{
  "mcpServers": {
    "nodus": {
      "command": "nodus-mcp-server",
      "args": ["--stdio"]
    }
  }
}
```

If `nodus-mcp-server` isn't on your PATH, use the full path to the executable. On Windows with pipx that's typically `C:\Users\<you>\.local\bin\nodus-mcp-server.exe`.

### 3. Restart Claude Desktop

The seven `nodus_*` tools will appear when you click the tools icon (the `+` button or tool picker) in a new conversation.

## How to use

### Memory

Store anything you want Claude to remember across conversations:

```
Use nodus_remember to store: "Project deadline is 2026-07-01" with tags ["project", "deadlines"]
```

Retrieve it later:

```
Use nodus_recall to find memories tagged "deadlines"
```

Or search by content:

```
Use nodus_recall to find memories about "deadline"
```

Memory is stored in a local SQLite database at `~/.nodus-mcp-server/data/memory.db` and persists across upgrades.

### Sandboxed code execution

Run Nodus (`.nd`) code in a fully sandboxed runtime:

```
Use nodus_exec to run: print("Hello from Nodus!")
```

The sandbox enforces: no file I/O, no network, no subprocess, no environment variables. Use `print()` to surface results — top-level return values are not captured.

### Goals (structured multi-step tasks)

Goals run a fixed sequence of named steps and return each step's result:

```
Use nodus_run_goal with name "summarize" and params {"text": "your text here"}
```

Built-in goals:

| Goal | Params | What it does |
|------|--------|-------------|
| `summarize` | `{text}` | Counts characters, classifies size (short/medium/long) |
| `pipeline` | `{items, label}` | Validates a list and produces a labelled report |

### Workflows (checkpoint/resume orchestration)

Workflows are like goals but support checkpoints — they can be paused and resumed from a saved state:

```
Use nodus_run_workflow with name "research" and params {"topic": "LLM context windows"}
```

The response includes a `graph_id`. Use it to resume the workflow later:

```
Use nodus_resume_workflow with graph_id "g_abc123" (and optionally a checkpoint label)
```

Built-in workflows:

| Workflow | Params | What it does |
|----------|--------|-------------|
| `research` | `{topic}` | Two-step plan + execute workflow with checkpoints at each step |

## Adding your own goals and workflows

Goals and workflows are `.nd` files (Nodus source) placed in the `goals/` or `workflows/` directory of the installed package. The file should **only define** the goal or workflow — the server calls it for you.

```nodus
// goals/my_goal.nd
goal my_goal {
    step process {
        if (input_text == nil) { throw "missing required param: input_text" }
        let result = len(input_text)
        return {"length": result, "has_content": result > 0i}
    }
}
```

Then call it:
```json
{"name": "my_goal", "params": {"input_text": "hello"}}
```

For a goal, `params` are injected as top-level variables in the `.nd` execution context. Check `nil` before using them — missing params surface as `nil`, not an error, unless you throw explicitly.

A workflow declares its params instead, so they are stored with the run and restored when it resumes. A missing or unknown param is an error:

```nodus
// workflows/my_workflow.nd
workflow my_workflow(input_text) {
    step process {
        checkpoint "processed"
        return {"length": len(input_text)}
    }
}
```

See the [Nodus language guide](https://github.com/Masterplanner25/Nodus/tree/main/docs/guide) for the full `.nd` syntax reference.

## About Nodus

The goals, workflows, and `nodus_exec` sandbox all run on the [Nodus](https://github.com/Masterplanner25/Nodus) VM — a lightweight, embeddable language runtime designed for AI-native orchestration. Nodus scripts (`.nd` files) define the step logic; the MCP server wires them to Claude over the Model Context Protocol.

This package is part of the **Nodus ecosystem** — a family of companion libraries built around the [`nodus-lang`](https://pypi.org/project/nodus-lang/) runtime (MCP and A2A servers, an SDK, memory and workflow engines, and more; see the [ecosystem overview](https://github.com/Masterplanner25/Nodus/blob/main/docs/ecosystem/README.md)). The runtime's own suite runs this server's tests on every release, so an upstream change that would break it is caught before it ships.

## Architecture

```
server.py          — MCP tool definitions, NodusRuntime setup, request dispatch
runner.py          — goal/workflow execution via ModuleLoader + VM
memory_store.py    — SQLite-backed thread-safe memory store
goals/             — .nd goal definitions (bundled + custom)
workflows/         — .nd workflow definitions (bundled + custom)
~/.nodus-mcp-server/data/memory.db  — SQLite DB (persists across upgrades)
```

## ChatGPT Desktop setup

> **This server has no authentication.** Anyone who can reach its URL can use every tool: read, change and delete your memory, and run code in the `nodus_exec` sandbox. Do not publish the URL, and stop the tunnel when you are not using it. By default `--http` listens only on `127.0.0.1`.

ChatGPT calls the server from OpenAI's cloud, not from your machine, so it needs a public HTTPS URL. It cannot use `localhost` or `127.0.0.1`. Use ngrok to expose the server.

### 1. Start the HTTP server

```bash
nodus-mcp-server --http --port 8765
```

This prints:
```
[nodus-mcp-server] HTTP listening on http://127.0.0.1:8765/mcp
[nodus-mcp-server] Point ChatGPT / your MCP client at: http://127.0.0.1:8765/mcp
```

That is the **local** address. A local MCP client can use it directly, but ChatGPT needs the tunnel URL from step 2.

### 2. Expose via ngrok

```bash
ngrok http --url=<your-static-domain>.ngrok.io 8765
```

Keep both terminals open while using ChatGPT, and close them when you finish.

### 3. Connect in ChatGPT Desktop

1. Click your profile icon → **Settings** → **Apps**
2. Go to **Advanced Settings** → enable **Developer Mode**
3. Click **Create App** (or **Connect more**)
4. Enter a name (e.g. `Nodus`), description, and base URL: `https://<your-static-domain>.ngrok.io/mcp`

> **Created the app before 0.1.11?** Its URL probably ends in `/sse`. 0.1.11 replaced the SSE transport with streamable HTTP at `/mcp`, and `/sse` now returns 404, so ChatGPT can no longer reach the server. ChatGPT's app settings have no field for changing the URL: delete the app and create it again with the `/mcp` URL.

### 4. Use in a chat

Open a new chat → click `+` → **More** → **Developer Mode** → enable your Nodus app. The seven `nodus_*` tools are now available.

> **Note:** Memory is shared with the Claude Desktop instance (same SQLite database at `~/.nodus-mcp-server/data/memory.db`).

---

## Upgrading

```
Stop-Process -Name "nodus-mcp-server" -Force   # Windows — close before reinstalling
pipx install nodus-mcp-server --force
```

Then restart Claude Desktop / ChatGPT Desktop.

## Development

```
git clone https://github.com/Masterplanner25/nodus-mcp-server
cd nodus-mcp-server
python -m pip install . mcp pytest httpx
python -m pytest -q
```

CI runs the suite across a matrix of OS (Ubuntu, Windows), Python (3.10 and 3.13 — the ends of `requires-python`), and the `mcp` SDK (1.x and 2.x, since the server branches on the SDK major). A separate job builds the wheel and completes a real `initialize` handshake against the installed server. `main` is branch-protected: changes land through a branch, a pull request, and green CI.

## License

MIT
