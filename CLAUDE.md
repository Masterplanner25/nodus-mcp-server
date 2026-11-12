# nodus-mcp-server — Claude instructions

## What this is, and where it sits

An MCP server that exposes the [Nodus](https://github.com/Masterplanner25/Nodus)
runtime to AI assistants (Claude Desktop over stdio, ChatGPT Desktop over HTTP).
It is a **companion package in the Nodus ecosystem**, not a standalone project:

- It depends on **`nodus-lang`** (PyPI) and embeds a `NodusRuntime`. The language
  lives at `C:\dev\Coding Language` (repo `Masterplanner25/Nodus`); its `CLAUDE.md`
  is the authority for anything about the runtime, the embedding API, capabilities,
  and `.nd` syntax.
- It is a **registered dependent**: the language repo runs this package's suite as
  part of its release Gate 10a (`tools/nodus_lang_dependents.json`), and lists it in
  `docs/ecosystem/COMPANION_REPOS.md`. A nodus-lang release that breaks this server
  is caught there — which is how #893 (nodus-lang's broken Python 3.10 floor) was
  found: this repo's CI went red on its 3.10 cells.
- Published to **PyPI** as `nodus-mcp-server`. Sibling MCP package: `nodus-mcp`
  (a different server; keep the two SDK-major ports in step — see below).

## Running and testing

```powershell
cd C:\dev\nodus-mcp-server
python -m pytest -q
```

The suite needs `nodus-lang` and `mcp` installed in the environment. It writes a
`.nodus/` cache and a `data/` SQLite store under the repo — both are gitignored.
There is no `PYTHONPATH` trick here: unlike the language repo, this package is
imported as installed (or editable). To test against **dev** nodus-lang, install
the language from its source (`pip install -e "C:\dev\Coding Language"`); otherwise
you get whatever `nodus-lang` is on PyPI.

## Version — one source

`nodus_mcp_server/__init__.py` holds `__version__`, and `pyproject.toml` reads it
dynamically (`[tool.setuptools.dynamic]`). **Bump the one place.** `serverInfo.version`
and `tests/test_http_transport.py` both assert against `__version__`, so a handshake
proves the running server reports the package version, not the SDK's.

## Release

1. Bump `__version__`; add a `CHANGELOG.md` entry **in the same change** (Keep a
   Changelog format; the file starts at 0.1.12).
2. `python -m build` → `python -m twine check dist/*`.
3. Install the built wheel into a **clean venv from a neutral CWD** and complete a
   real `initialize` handshake (the CI `package` job does exactly this — mirror it).
4. `python -m twine upload dist/*` (account-scoped token in `~/.pypirc`; never write
   a token into a repo file).
5. Tag and create the GitHub release **only after** the PyPI upload succeeds.

**Release notes / any file another tool reads: write with the editor tools, not a
PowerShell heredoc or `Out-File` that adds a UTF-8 BOM** — a BOM in a `gh release`
notes file has bitten the ecosystem before.

## CI (`.github/workflows/ci.yml`)

- `test`: matrix of **os** {ubuntu, windows} × **python** {3.10, 3.13} × **mcp**
  {1.x, 2.x}, plus an `mcp==1.8.0` floor cell. Installs `.` (so `nodus-lang` resolves
  from PyPI), then `pytest -q`.
- `package`: builds the wheel, asserts it does **not** ship a top-level `tests`
  package and **does** ship `nodus_mcp_server/workflows/research.nd`, then installs
  the wheel clean and runs a real handshake.

Pushing to a PR branch re-runs the matrix. A nodus-lang release on PyPI changes what
the matrix resolves, so re-running CI is how you confirm this server against a new
language version.

## Gotchas (each cost time at least once)

- **Two runtimes in `server.py`.** `_exec_runtime` (the `nodus_exec` arbitrary-code
  path) denies **env, network, subprocess** explicitly — `allow_env=False` is
  load-bearing (below nodus-lang 5.0.0 the default was *on*, #7). `_runtime` (goals/
  workflows) relies on 5.0.0 deny-by-default. Do not loosen either.
- **`graph_id` is validated before it becomes a path.** `nodus_resume_workflow` feeds
  `graph_id` to nodus-lang, which builds a file path from it; an unvalidated `../x`
  escaped the state directory. `runner.py`'s `_GRAPH_ID_RE` (letters/digits/`_`/`-`,
  ≤128) gates it. This is a security boundary — keep it, and keep the test.
- **`server.py` branches on the mcp SDK major** (`_SDK_V2`, chosen once at import).
  Both 1.x and 2.x must keep working; `tests/test_http_transport.py` drives a real
  `initialize` → `tools/list` through the ASGI app on both. The same port exists in
  `nodus-mcp` — fix a transport bug in both so they do not drift.
- **`.nd` files only *define* a goal/workflow; the server runs them.** Host globals
  go to the `ModuleLoader`, never the `VM` constructor. A workflow must **declare its
  params** (`workflow name(param) { ... }`) so they survive a resume; goals take
  params as injected globals and see `nil` for a missing one.
- **Keep `include = ["nodus_mcp_server*"]` in pyproject.** Without it the wheel ships
  a top-level `tests` package into `site-packages` (#8). The `package` CI job fails if
  it regresses.
- **The `mcp` floor is `>=1.22.0`, and it is pydantic-driven, not feature-driven.**
  mcp below 1.22 imports `eval_type_backport` from `pydantic._internal._typing_extra`,
  which pydantic 2.14 removed, so `import mcp` fails on a current install. 1.22.0 is
  the lowest release that imports with current pydantic and still has
  `StreamableHTTPSessionManager`. The CI floor cell pins `mcp==1.22.0`; if a future
  pydantic breaks 1.22, re-bisect and raise the floor again (0.1.16 found this by
  re-running CI once nodus-lang 5.15.1 unmasked it).

  **How to bisect it.** For each candidate `V`, in a throwaway venv with a current
  pydantic:
  - `pip install "mcp==V"`, then `import nodus_mcp_server.server`. That module imports
    `mcp.server`, `mcp.server.stdio`, `StreamableHTTPSessionManager` and `mcp.types`
    at the top, so the probe exercises the real imports, not `import mcp` alone.
  - Read the installed version with `importlib.metadata.version("mcp")`, not
    `mcp.__version__`. The failure happens inside `import mcp`, so a probe that reads
    the version from the module dies before its own `try` can report which `V` broke.

  The lowest `V` that imports cleanly *and* still has `StreamableHTTPSessionManager` is
  the floor. Update `pyproject.toml` and the CI floor cell together.
- **The `nodus-lang` floor is `>=5.15.1`** because 5.15.0's bare `import tomllib`
  breaks every Python 3.10 install (nodus-lang #893); 5.15.1 is the first that imports
  on 3.10, so the floor must be 5.15.1 for this package's `requires-python = ">=3.10"`
  to hold. Do **not** add an upper cap on nodus-lang (ecosystem policy — the suite
  catches a real break; a cap earns its place once a break is known).

## PR workflow

`main` is branch-protected: no direct pushes, changes go through a branch + PR + CI.
Branch, push, `gh pr create`, wait for CI, then `gh pr merge --squash`.
