# Changelog

Format: [Keep a Changelog](https://keepachangelog.com)
Versioning: [Semantic Versioning](https://semver.org)

This file starts at 0.1.12. Releases 0.1.0–0.1.11 predate it; their history is in
the git log and the GitHub releases.

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
