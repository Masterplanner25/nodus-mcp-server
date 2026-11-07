"""Tests for goal/workflow/exec runner against live nodus-lang."""
import pytest
from nodus.runtime.embedding import NodusRuntime
from nodus_mcp_server import runner


@pytest.fixture(scope="module")
def rt():
    return NodusRuntime(timeout_ms=None, max_steps=None, allowed_paths=[])


# ── exec_code ─────────────────────────────────────────────────────────────────

def test_exec_print_int(rt):
    result = runner.exec_code(rt, "print(1 + 1)")
    assert result["ok"] is True
    assert result["stdout"] == "2.0"  # Nodus numbers print as floats


def test_exec_print_string(rt):
    result = runner.exec_code(rt, 'print("hello")')
    assert result["ok"] is True
    assert result["stdout"] == "hello"


def test_exec_print_captured(rt):
    result = runner.exec_code(rt, 'print("hi")')
    assert result["ok"] is True
    assert result["stdout"] == "hi"


def test_exec_empty_code_succeeds(rt):
    # Empty program is valid Nodus — returns ok=True with no output
    result = runner.exec_code(rt, "")
    assert result["ok"] is True
    assert result["stdout"] == ""


def test_exec_let_binding(rt):
    result = runner.exec_code(rt, 'let x = 42\nprint(x)')
    assert result["ok"] is True
    assert result["stdout"] == "42.0"  # Nodus numbers print as floats


def test_exec_syntax_error(rt):
    result = runner.exec_code(rt, "let x = ")
    assert result["ok"] is False
    assert "error" in result


def test_exec_throw(rt):
    result = runner.exec_code(rt, 'throw "boom"')
    assert result["ok"] is False
    assert "boom" in result.get("error", "")


# ── run_goal ──────────────────────────────────────────────────────────────────

def test_run_goal_summarize_short(rt):
    result = runner.run_goal(rt, "summarize", {"text": "hi"})
    assert result["ok"] is True
    classify = result["steps"]["classify"]
    assert classify["size"] == "short"
    assert classify["chars"] == 2
    assert classify["empty"] is False


def test_run_goal_summarize_empty(rt):
    result = runner.run_goal(rt, "summarize", {"text": ""})
    assert result["ok"] is True
    classify = result["steps"]["classify"]
    assert classify["empty"] is True
    assert classify["chars"] == 0


def test_run_goal_summarize_long(rt):
    result = runner.run_goal(rt, "summarize", {"text": "x" * 2001})
    assert result["ok"] is True
    assert result["steps"]["classify"]["size"] == "long"


def test_run_goal_pipeline(rt):
    result = runner.run_goal(rt, "pipeline", {"items": [1, 2, 3], "label": "batch"})
    assert result["ok"] is True
    report = result["steps"]["report"]
    assert report["label"] == "batch"
    assert report["item_count"] == 3
    assert report["has_items"] is True
    assert report["status"] == "complete"


def test_run_goal_pipeline_empty_items(rt):
    result = runner.run_goal(rt, "pipeline", {"items": [], "label": "empty"})
    assert result["ok"] is True
    assert result["steps"]["report"]["has_items"] is False
    assert result["steps"]["report"]["item_count"] == 0


def test_run_goal_not_found(rt):
    result = runner.run_goal(rt, "nonexistent_goal_xyz", {})
    assert result["ok"] is False
    assert "not found" in result["error"].lower()


def test_run_goal_path_traversal_blocked(rt):
    result = runner.run_goal(rt, "../../../etc/passwd", {})
    assert result["ok"] is False


# ── run_workflow ───────────────────────────────────────────────────────────────

def test_run_workflow_research(rt):
    result = runner.run_workflow(rt, "research", {"topic": "LLM context windows"})
    assert result["ok"] is True
    plan = result["steps"]["plan"]
    assert plan["query"] == "LLM context windows"
    assert plan["strategy"] == "step-by-step"
    execute = result["steps"]["execute"]
    assert execute["topic"] == "LLM context windows"
    assert execute["status"] == "complete"


def test_run_workflow_not_found(rt):
    result = runner.run_workflow(rt, "nonexistent_workflow_xyz", {})
    assert result["ok"] is False
    assert "not found" in result["error"].lower()


def test_run_workflow_missing_param_is_refused(rt, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.run_workflow(rt, "research", {})
    assert result["ok"] is False
    assert "topic" in result["error"]


def test_run_workflow_unknown_param_is_refused(rt, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.run_workflow(rt, "research", {"topic": "x", "nope": 1})
    assert result["ok"] is False
    assert "nope" in result["error"]


# ── resume_workflow_tool ───────────────────────────────────────────────────────

@pytest.mark.parametrize("checkpoint", ["planned", "executed", None])
def test_resume_keeps_the_params_the_run_started_with(rt, tmp_path, monkeypatch, checkpoint):
    """Resume reloads the workflow in a fresh VM and has no params to pass.

    Through 0.1.14 `topic` was an injected global, so every resume of the
    built-in workflow failed with `Undefined variable: topic`.
    """
    monkeypatch.chdir(tmp_path)
    started = runner.run_workflow(rt, "research", {"topic": "LLM context windows"})
    assert started["ok"] is True

    resumed = runner.resume_workflow_tool(started["graph_id"], checkpoint)

    assert resumed["ok"] is True, resumed
    assert resumed["graph_id"] == started["graph_id"]
    assert resumed["steps"]["plan"]["query"] == "LLM context windows"
    assert resumed["steps"]["execute"]["topic"] == "LLM context windows"


def test_resume_unknown_graph_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.resume_workflow_tool("g_0000000000", None)
    assert result["ok"] is False
    assert "no saved state" in result["error"]


@pytest.mark.parametrize("graph_id", [
    "../../../outside",
    "..\\..\\..\\outside",
    "/etc/outside",
    "C:/outside",
    "g_abc/../../outside",
    "",
    "x" * 129,
])
def test_resume_refuses_a_graph_id_that_is_not_an_id(tmp_path, monkeypatch, graph_id):
    """The id comes from the client and nodus-lang builds a path from it."""
    cwd = tmp_path / "a" / "b" / "c"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    (tmp_path / "outside.json").write_text('{"metadata": {"workflow_name": "research"}}')

    assert runner.resume_workflow_tool(graph_id, None) == {
        "ok": False,
        "error": "invalid graph_id",
    }
