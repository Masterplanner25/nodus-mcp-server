"""`nodus_exec` runs code an MCP client chose, under an explicit denial of each capability.

Through 0.1.14 `allow_env` was not set, and `nodus-lang>=4.0.5` allowed an install
where it defaults to on, so a client could read and change the server's environment.
"""
import ast
import os
from pathlib import Path

import pytest
from nodus.runtime.embedding import NodusRuntime

from nodus_mcp_server import server

_SERVER_SOURCE = Path(server.__file__).read_text(encoding="utf-8")
_ENV_READ = 'import "std:env" as env\nprint(env.get("NODUS_MCP_TEST_SENTINEL", "absent"))'


def test_every_capability_is_denied_by_name_in_the_source():
    """Asserted on the source: on nodus-lang 5.x a missing flag still behaves as denied."""
    for node in ast.walk(ast.parse(_SERVER_SOURCE)):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "_exec_runtime":
            flags = {k.arg: k.value.value for k in node.value.keywords if k.arg.startswith("allow_")}
            assert flags == {"allow_network": False, "allow_subprocess": False, "allow_env": False}
            return
    pytest.fail("_exec_runtime assignment not found in server.py")


def test_the_probe_reads_the_variable_when_allowed(monkeypatch):
    """The control: without this the denial below could pass on a broken probe."""
    monkeypatch.setenv("NODUS_MCP_TEST_SENTINEL", "visible")
    runtime = NodusRuntime(timeout_ms=None, max_steps=None, allowed_paths=[], allow_env=True)
    assert runtime.run_source(_ENV_READ, timeout_ms=10_000)["stdout"].strip() == "visible"


@pytest.mark.parametrize("source", [
    _ENV_READ,
    'import "std:env" as env\nprint(env.list())',
    'import "std:env" as env\nenv.set("NODUS_MCP_TEST_WRITTEN", "1")',
])
def test_exec_cannot_touch_the_environment(monkeypatch, source):
    monkeypatch.setenv("NODUS_MCP_TEST_SENTINEL", "visible")
    monkeypatch.delenv("NODUS_MCP_TEST_WRITTEN", raising=False)

    result = server._exec({"code": source})

    assert result["ok"] is False
    assert "environment variable access" in result["error"]
    assert "NODUS_MCP_TEST_WRITTEN" not in os.environ
