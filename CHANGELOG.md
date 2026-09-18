# Changelog

Format: [Keep a Changelog](https://keepachangelog.com)
Versioning: [Semantic Versioning](https://semver.org)

This file starts at 0.1.12. Releases 0.1.0–0.1.11 predate it; their history is in
the git log and the GitHub releases.

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
