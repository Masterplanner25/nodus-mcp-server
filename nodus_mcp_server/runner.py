"""Goal and workflow execution helpers."""
from __future__ import annotations

import os
from typing import Any

from nodus.vm.vm import VM
from nodus.runtime.module_loader import ModuleLoader
from nodus.tooling.sandbox import capture_output, configure_vm_limits
from nodus.tooling.runner import _resolve_goal_from_vm, _resolve_workflow_from_vm
from nodus.support.config import MAX_STEPS, MAX_STDOUT_CHARS

_HERE = os.path.dirname(os.path.abspath(__file__))
GOALS_DIR = os.path.join(_HERE, "goals")
WORKFLOWS_DIR = os.path.join(_HERE, "workflows")


def _load(directory: str, name: str, kind: str) -> tuple[str, str]:
    safe = os.path.basename(name)  # prevent path traversal
    path = os.path.join(directory, f"{safe}.nd")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{kind} '{safe}' not found (looked in {directory})")
    with open(path, encoding="utf-8") as fh:
        return fh.read(), path


def _load_into_vm(code: str, path: str, params: dict, timeout_ms: int) -> tuple[VM, str]:
    """Compile and execute module-level code (goal/workflow definition) into a sandboxed VM.

    host_globals must be passed to ModuleLoader — passing them only to the VM constructor
    is not enough because _execute_module overwrites vm.host_globals via reset_program.
    """
    vm = VM([], {}, code_locs=[], source_path=path, allowed_paths=[])
    configure_vm_limits(vm, max_steps=MAX_STEPS, timeout_ms=timeout_ms)
    loader = ModuleLoader(vm=vm, host_globals=params)
    module_name = os.path.abspath(path)
    base_dir = os.path.dirname(module_name)
    with capture_output(max_stdout_chars=MAX_STDOUT_CHARS) as (stdout, _):
        loader.load_module_from_source(code, module_name=module_name, base_dir=base_dir)
    return vm, stdout.getvalue()


def run_goal(runtime: Any, name: str, params: dict) -> dict:
    code, path = _load(GOALS_DIR, name, "goal")
    try:
        vm, def_stdout = _load_into_vm(code, path, params, timeout_ms=30_000)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    try:
        goal = _resolve_goal_from_vm(vm, name)
        with capture_output(max_stdout_chars=MAX_STDOUT_CHARS) as (run_out, _):
            goal_result = vm.builtin_run_goal(goal)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    combined_stdout = (def_stdout + run_out.getvalue()).strip()
    goal_data = goal_result if isinstance(goal_result, dict) else {}
    steps = goal_data.get("steps", {})
    out: dict = {"ok": True, "goal": name, "steps": steps}
    if combined_stdout:
        out["stdout"] = combined_stdout
    return out


def run_workflow(runtime: Any, name: str, params: dict) -> dict:
    code, path = _load(WORKFLOWS_DIR, name, "workflow")
    try:
        vm, def_stdout = _load_into_vm(code, path, params, timeout_ms=60_000)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    try:
        workflow = _resolve_workflow_from_vm(vm, name)
        with capture_output(max_stdout_chars=MAX_STDOUT_CHARS) as (run_out, _):
            wf_result = vm.builtin_run_workflow(workflow)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    combined_stdout = (def_stdout + run_out.getvalue()).strip()
    wf_data = wf_result if isinstance(wf_result, dict) else {}
    steps = wf_data.get("steps", {})
    out: dict = {"ok": True, "workflow": name, "steps": steps}
    if combined_stdout:
        out["stdout"] = combined_stdout
    return out


def exec_code(runtime: Any, code: str) -> dict:
    result = runtime.run_source(code, timeout_ms=10_000)
    if not result["ok"]:
        err = result.get("error") or {}
        return {"ok": False, "error": err.get("message", "execution failed")}
    return {
        "ok": True,
        "result": result.get("result"),
        "stdout": result.get("stdout", "").strip(),
    }
