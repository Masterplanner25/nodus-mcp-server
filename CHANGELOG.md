# Changelog

Format: [Keep a Changelog](https://keepachangelog.com)
Versioning: [Semantic Versioning](https://semver.org)

This file starts at 0.1.12. Releases 0.1.0–0.1.11 predate it; their history is in
the git log and the GitHub releases.

## [0.1.16] — 2026-11-07

### Changed

- **Raised the `mcp` floor to `>=1.22.0`** (was `>=1.8`). mcp below 1.22 imports
  `eval_type_backport` from `pydantic._internal._typing_extra`, which current
  pydantic (2.14) no longer exposes, so `import mcp` failed at collection — the
  declared floor did not actually install. 1.22.0 is the lowest release that
  imports with current pydantic and still carries `StreamableHTTPSessionManager`
  (the reason for the 1.8 floor). The suite passes against it (45 tests).

  Surfaced by CI: once nodus-lang 5.15.1 fixed the Python 3.10 import (its #893),
  the previously-masked 3.10 floor cell revealed this. The CI floor cell now pins
  `mcp==1.22.0`.

- **Raised the `nodus-lang` floor to `>=5.15.1`** (was `>=5.15.0`). 5.15.0 has a
  bare `import tomllib` that breaks every Python 3.10 install (nodus-lang #893);
  5.15.1 is the first release that imports on 3.10, so the floor must be 5.15.1
  for this package's own `requires-python = ">=3.10"` to hold.

### Added

- **A CI workflow** (#9): a matrix of OS {ubuntu, windows} × Python {3.10, 3.13}
  × mcp {1.x, 2.x} plus the declared floor, and a package job that builds the
  wheel and completes a real `initialize` handshake against the installed server.

## [0.1.15] — 2026-11-06

### Security

- **This server has no authentication, and the README now says so.** Anyone who can
  reach `--http` can use every tool: read, change and delete memory, and run code in
  the `nodus_exec` sandbox. The ChatGPT setup told readers to expose it through ngrok
  without saying that. If you have a tunnel running, stop it when you are not using it.
- **`nodus_exec` could read and change the server's environment variables** when
  installed with nodus-lang below 5.0.0, which `nodus-lang>=4.0.5` allowed and which
  an older install keeps through an upgrade. `allow_env` was never set, so it took
  that version's default of on. It is now denied explicitly and the floor is raised.
  Files, network and subprocess were already denied.
- **`nodus_resume_workflow` took any string as a `graph_id`**, and nodus-lang builds
  a file path from it, so `../../x` loaded a `.json` file outside the state directory.
  The contents were not returned, but the reply showed whether the file existed. An
  id must now be letters, digits, `_` or `-`.

### Fixed

- **`nodus_resume_workflow` failed on the built-in workflow** with `Undefined variable:
  topic`, from every checkpoint. Params were injected as globals, and a resume reloads
  the workflow with none. `research` now declares `topic` as a workflow parameter,
  which nodus-lang stores with the run and restores on resume.
- **`serverInfo.version` reported the mcp SDK's version**, not this package's.
- **The wheel no longer installs a top-level `tests` package** into `site-packages`.

### Changed

- **Workflow params must now be declared.** `nodus_run_workflow` binds them to the
  workflow's parameters, so a missing or unknown param is an error, and a custom
  workflow must be written `workflow name(param) { ... }`. Goals are unchanged.
  Runs saved by 0.1.14 or earlier cannot be resumed; they never could.
- **Requires `nodus-lang>=5.15.0`**, which keeps a run's bounds across a resume.
- The version now has one source, `nodus_mcp_server.__version__`.

## [0.1.14] — 2026-09-18

### Fixed

- **Works with the mcp SDK 2.x** (#3). The server registered its two handlers
  through the 1.x decorator API (`@app.list_tools()` / `@app.call_tool()`),
  which mcp 2.0 removed, so with mcp ≥ 2.0 installed `import
  nodus_mcp_server.server` failed — both transports. 0.1.13 pinned `mcp<2` as
  the stopgap; the pin is lifted to `mcp>=1.8`.

  One implementation with a branch per SDK major, chosen once at import
  (`_SDK_V2`): on 2.x the handlers go through `add_request_handler`, take
  `(ctx, params)` and return result models; 1.x is unchanged. The same port as
  nodus-mcp 0.1.4 (nodus-mcp#11), so the two servers do not drift. Both
  branches are driven by `tests/test_http_transport.py`'s real `initialize` →
  `tools/list` through the ASGI app: 27/27 under mcp 1.27 and under 2.2.

## [0.1.13] — 2026-09-17

### Fixed

- **`--http` mode answered every request with a 500.** 0.1.11 replaced the
  SSE transport with `StreamableHTTPSessionManager` to fix a `TypeError` on
  every request, and never entered the manager's `run()` — its task group is
  created there, and without it every POST to `/mcp` raised
  `RuntimeError: Task group is not initialized. Make sure to use run()`. The
  process started, printed its URL, and served nothing; ChatGPT-over-ngrok,
  the mode's whole purpose, could not complete `initialize`. The manager now
  runs for the app's lifespan (`build_http_app()`), which is what uvicorn and
  Starlette's `TestClient` both start and stop.

  `tests/test_http_transport.py` completes a real `initialize` → `tools/list`
  through the app, and pins the unrun shape as a 500 so the reading is not
  lore. Nothing drove the HTTP app before; both transport fixes shipped on the
  strength of the process starting. stdio mode (Claude Desktop) was never
  affected.

### Changed

- **`mcp` is pinned `>=1.8,<2`** (was `>=1.0.0`). Found by installing the
  0.1.13 wheel into a clean venv before uploading it: pip resolved `mcp`
  2.2.0, which removed the low-level `@app.list_tools()` / `@app.call_tool()`
  decorator API, and `import nodus_mcp_server.server` failed — in **both**
  modes. Every fresh install since mcp 2.0 shipped has been broken at import;
  an install that resolved 1.x earlier keeps working, which is why nobody saw
  it. 1.8 is where `StreamableHTTPSessionManager` first appeared. The 2.x
  port is issue #3.

## [0.1.12] — 2026-08-17

### Changed

- **Floated the `nodus-lang` dependency to `>=4.0.5`** (was `>=4.0.5,<5.0.0`).
  The upper bound made this package uninstallable alongside nodus-lang 5.0.0
  (`ResolutionImpossible`), while nothing in the code was incompatible — the
  full suite (25 tests) passes against 5.0.0 unchanged.

  The cap was prophylactic rather than earned; no 5.x break was ever recorded
  here. A hard upper bound on a first-party dependency turns every nodus-lang
  major into a two-repo release train with consumers frozen in between. This
  package's own suite is the check that catches a real break; a cap earns its
  place once a break is known.

### Note on nodus-lang 5.0.0 behaviour

nodus-lang 5.0.0 makes embedded runtimes deny subprocess, network and env access
by default. This server constructs two runtimes in `server.py`: `_exec_runtime`
(the arbitrary-code path) already passed `allow_network=False,
allow_subprocess=False` explicitly and is unchanged; `_runtime` used the defaults
and now denies as well. That is a real behaviour change in the safe direction,
and the suite passes, so nothing depended on the old permissiveness. For a server
exposed to Claude Desktop and ChatGPT, model-generated workflows running without
subprocess access is the intended posture.
