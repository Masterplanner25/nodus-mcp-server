# nodus-mcp-server

> **Status:** published on [PyPI](https://pypi.org/project/nodus-mcp-server/).

An MCP server that connects AI assistants to the [Nodus](https://github.com/Masterplanner25/Nodus) language runtime — giving them persistent memory, sandboxed code execution, and checkpoint/resume orchestration workflows, all powered by `.nd` scripts running on the Nodus VM.

Supports **Claude Desktop** (stdio), and **claude.ai** and **ChatGPT** (streamable HTTP at `/mcp`, through a tunnel, with sign-in).

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
oauth.py           — sign-in for --http --public-url (OAuth provider, consent page, grants)
goals/             — .nd goal definitions (bundled + custom)
workflows/         — .nd workflow definitions (bundled + custom)
~/.nodus-mcp-server/data/memory.db  — SQLite DB (persists across upgrades)
~/.nodus-mcp-server/data/oauth.db   — OAuth clients and grants (with --public-url)
~/.nodus-mcp-server/passphrase.json — the sign-in passphrase, scrypt-hashed
```

## claude.ai and ChatGPT setup (remote, with sign-in)

claude.ai and ChatGPT call the server from their own cloud, not from your machine, so they need a public HTTPS URL. They cannot use `localhost` or `127.0.0.1`. Use a tunnel such as ngrok, and turn on **sign-in** so that the URL alone gets nobody in.

With `--public-url`, the server is its own OAuth authorization server. Each app registers itself, sends you to a consent page on your server, and gets a token only after you enter your passphrase and choose what it may do. Without `--public-url`, `--http` has no sign-in at all, exactly as before 0.2.0, and must stay on loopback.

### 1. Set a passphrase

```bash
nodus-mcp-server set-passphrase
```

It is prompted for, never passed as an argument, and stored as a scrypt hash in `~/.nodus-mcp-server/passphrase.json`. Setting a new one signs every app out.

### 2. Start the server with your public URL

```bash
nodus-mcp-server --http --port 8765 --public-url https://<your-static-domain>.ngrok.io
```

`--public-url` is the `https://` origin the tunnel serves, with no path. You can set `NODUS_MCP_PUBLIC_URL` instead. The server refuses to start with it if no passphrase is set.

### 3. Start the tunnel

```bash
ngrok http --url=<your-static-domain>.ngrok.io 8765
```

### 4. Connect

The server URL in both apps is `https://<your-static-domain>.ngrok.io/mcp`.

- **claude.ai:** Settings → Connectors → **Add custom connector**, enter the URL, and choose dynamic client registration (DCR). This server does not support a client metadata URL (CIMD).
- **ChatGPT:** Settings → Apps → Advanced settings → enable **Developer Mode**, then **Create app** with the URL and OAuth authentication.

Your browser opens the consent page. It shows the address the app will send you back to. Check that address, since apps choose their own names. Tick what to allow, enter your passphrase, and press **Allow**.

> **Created a ChatGPT app before 0.1.11?** Its URL probably ends in `/sse`. 0.1.11 replaced the SSE transport with streamable HTTP at `/mcp`, and `/sse` now returns 404. ChatGPT's app settings have no field for changing the URL: delete the app and create it again with the `/mcp` URL.

### What an app can be allowed to do

| Scope | Tools | Ticked by default |
|---|---|---|
| `memory.read` | `nodus_recall` | yes |
| `memory.write` | `nodus_remember`, `nodus_forget` | no |
| `workflow.run` | `nodus_run_goal`, `nodus_run_workflow`, `nodus_resume_workflow` | no |
| `exec` | `nodus_exec` | no, and only offered with `--allow-remote-exec` |

An app sees only the tools its token allows. `nodus_exec` runs code on your computer. It is sandboxed, but a sandbox bug there is a bug on your machine, so it is not offered over the public URL unless you start the server with `--allow-remote-exec`.

### Managing apps

```bash
nodus-mcp-server grants list            # who is connected, with what, and when it was last used
nodus-mcp-server grants revoke <id>     # sign one out now
```

Access tokens last an hour. Refresh tokens last 30 days and change on every use. If an old refresh token is used again, that app is signed out.

> **Note:** Memory is shared with the Claude Desktop instance (same SQLite database at `~/.nodus-mcp-server/data/memory.db`).

### Local HTTP clients

`nodus-mcp-server --http` with no `--public-url` serves `http://127.0.0.1:8765/mcp` with **no authentication**. Anything that can reach it can use every tool. Use it only for local clients, and never put a tunnel in front of it. With `--public-url` on, local HTTP clients sign in too, because the server cannot tell a tunnelled request from a local one. Claude Desktop uses stdio and is unaffected either way.

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
