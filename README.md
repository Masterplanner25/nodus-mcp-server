# nodus-mcp-server

An MCP (Model Context Protocol) server that exposes the [Nodus](https://github.com/Masterplanner25/Nodus) orchestration runtime as tools for Claude Desktop and other MCP-compatible hosts.

## What it does

Six tools over a single server process:

| Tool | Description |
|------|-------------|
| `nodus.remember` | Store a fact in persistent SQLite memory with optional tags |
| `nodus.recall` | Search memory by free-text query and/or tags |
| `nodus.forget` | Delete a memory entry by ID |
| `nodus.run_goal` | Run a Nodus goal (sandboxed, structured step results) |
| `nodus.run_workflow` | Run a Nodus workflow (checkpoint/resume capable) |
| `nodus.exec` | Execute arbitrary `.nd` code (10 s timeout, no file I/O) |

## Requirements

- Python ≥ 3.10
- `nodus-lang >= 4.0.4`
- `nodus-mcp >= 0.1.0`

## Install

```
pip install nodus-lang nodus-mcp nodus-mcp-server
```

## Claude Desktop setup

Add to your `claude_desktop_config.json`:

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

Restart Claude Desktop. The six `nodus.*` tools will appear in the tool list.

Memory persists at `~/.nodus-mcp-server/data/memory.db` and survives upgrades.

## HTTP mode

For remote or multi-client use:

```
nodus-mcp-server --http --port 8080
nodus-mcp-server --http --port 8080 --bearer-token <secret>
```

## Built-in goals and workflows

### Goals

**`summarize`** — `params: {text: string}`

Counts characters and classifies text size (short / medium / long).

```json
{
  "name": "summarize",
  "params": {"text": "Your text here"}
}
```

Returns:
```json
{
  "steps": {
    "measure": 14,
    "classify": {"chars": 14, "size": "short", "empty": false}
  }
}
```

**`pipeline`** — `params: {items: list, label: string}`

Validates an item list and produces a labelled report.

```json
{
  "name": "pipeline",
  "params": {"items": [1, 2, 3], "label": "batch-1"}
}
```

Returns:
```json
{
  "steps": {
    "validate": 3,
    "report": {"label": "batch-1", "item_count": 3, "has_items": true, "status": "complete"}
  }
}
```

### Workflows

**`research`** — `params: {topic: string}`

Two-step planning + execution workflow with checkpoints at each step.

```json
{
  "name": "research",
  "params": {"topic": "LLM context windows"}
}
```

Returns:
```json
{
  "steps": {
    "plan": {"query": "LLM context windows", "strategy": "step-by-step"},
    "execute": {"topic": "LLM context windows", "query": "LLM context windows", "strategy": "step-by-step", "status": "complete"}
  }
}
```

## Adding your own goals and workflows

Drop a `.nd` file into `goals/` or `workflows/`. The file should **only define** the goal or workflow — do not call `run_goal()` or `run_workflow()` at the bottom (the server calls it for you).

```nodus
// goals/my_goal.nd  — input variable injected via params
goal my_goal {
    step process {
        let result = len(input_text)
        return {"length": result, "has_content": result > 0i}
    }
}
```

Then call it:
```json
{"name": "my_goal", "params": {"input_text": "hello"}}
```

## Sandbox

`.nd` scripts run with:
- No file system access (`allowed_paths=[]`)
- No network access
- Goal timeout: 30 s
- Workflow timeout: 60 s
- `nodus.exec` timeout: 10 s

## Architecture

```
server.py          — NodusRuntime, tool registration, MCP transport
runner.py          — goal/workflow execution via ModuleLoader + VM
memory_store.py    — SQLite-backed thread-safe memory store
goals/             — .nd goal definitions
workflows/         — .nd workflow definitions
~/.nodus-mcp-server/data/memory.db  — SQLite DB (persists across upgrades)
```

## License

MIT
