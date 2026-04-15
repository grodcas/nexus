#!/usr/bin/env python3
"""
score.py — scorer for Plan 2 eval cases.

Pure Python, no external deps. Given a case (from cases.yaml) and a
result (from run.py), returns per-axis booleans:

    {
        "routing":       bool,  # did Gemini call the expected tool with
                                # the expected args?
        "task_success":  bool,  # did the handler output satisfy the
                                # case-level predicate (or was no tool
                                # expected and none was called)?
        "latency":       bool,  # did we land under the class budget?
    }

Four success modes, documented in cases.yaml:

    no_tool   — passes iff no tool call was made (or the trigger gate
                blocked it). Knowledge + smalltalk buckets.
    contains  — predicate must appear (case-insensitive) somewhere in
                the concatenated handler_result + assistant_text.
    judge     — Haiku grader call; delegated.
    exact     — reserved; currently always True. Use for future
                deterministic per-case checks.

Routing is scored independently of success: a case can get routing
right but fail success (handler was wrong), or get routing wrong
while the trigger gate accidentally saves it. Both facts matter and
both are reported.

Run this module directly to unit-test the scorer against 8 hand-
authored case/result pairs.
"""

from __future__ import annotations

from typing import Any


def _nested(d: dict, *keys: str, default: Any = None) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def score_routing(case: dict, result: dict) -> bool:
    """Did Gemini dispatch the right tool with the right args?"""
    expected = case.get("expected") or {}
    expected_action = expected.get("action")
    tool_called = result.get("tool_called")
    tool_args = result.get("tool_args") or {}

    # Case expects NO tool call.
    if expected_action is None:
        return tool_called is None

    # Case expects a specific tool call.
    if tool_called is None:
        return False

    actual_action = (tool_args.get("action") or "").lower().strip()
    if actual_action != str(expected_action).lower().strip():
        return False

    # All listed substrings must be in the query arg (lowercase).
    for sub in expected.get("query_contains") or []:
        if sub.lower() not in (tool_args.get("query") or "").lower():
            return False

    # Session arg must match if the case specifies one.
    if expected.get("session"):
        if (tool_args.get("session") or "") != expected["session"]:
            return False

    return True


def score_task_success(case: dict, result: dict, judge=None) -> bool:
    """
    Did the handler output satisfy the case-level predicate?
    `judge` is an instance with a .grade(case, result) -> bool method;
    only used when success.mode == 'judge'.
    """
    success = case.get("success") or {}
    mode = success.get("mode", "exact")

    if mode == "no_tool":
        # Passes if either (a) no tool was called, or (b) the gate
        # blocked the call (which means slim recognised the case as
        # trigger-less and did the right thing).
        return result.get("tool_called") is None or bool(result.get("gate_blocked"))

    if mode == "contains":
        predicate = (success.get("predicate") or "").lower()
        if not predicate:
            return True
        hay = (
            (result.get("assistant_text") or "")
            + " "
            + (result.get("handler_result") or "")
        ).lower()
        return predicate in hay

    if mode == "judge":
        if judge is None:
            return False  # judge required but not provided
        try:
            return bool(judge.grade(case, result))
        except Exception:
            return False

    if mode == "exact":
        # Reserved for future deterministic checks.
        return True

    return False


def score_latency(case: dict, result: dict) -> bool:
    """Was the Python-side latency under the class budget?"""
    budget = float(case.get("latency_budget_ms", 2000))
    actual = float(result.get("latency_ms", 0.0))
    return actual <= budget


def score_case(case: dict, result: dict, judge=None) -> dict:
    """Top-level: return per-axis booleans for one case.

    If the runner reported an error, every axis fails — a case that
    crashed the session cannot be scored as "routing passed because
    no tool call was made."
    """
    if result.get("error"):
        return {"routing": False, "task_success": False, "latency": False}
    return {
        "routing": score_routing(case, result),
        "task_success": score_task_success(case, result, judge),
        "latency": score_latency(case, result),
    }


# =============================================================================
# Unit tests — run `python eval/score.py` to exercise
# =============================================================================

def _test() -> None:
    def _mk_case(**k):
        base = {
            "id": "t",
            "expected": {"action": None, "query_contains": [], "session": None},
            "success": {"mode": "no_tool", "predicate": None},
            "latency_budget_ms": 2000,
        }
        for key, val in k.items():
            if key in base and isinstance(base[key], dict):
                base[key] = {**base[key], **val}
            else:
                base[key] = val
        return base

    def _mk_result(**k):
        base = {
            "tool_called": None,
            "tool_args": {},
            "assistant_text": "",
            "handler_result": "",
            "gate_blocked": False,
            "latency_ms": 100.0,
        }
        base.update(k)
        return base

    fails = 0
    def check(label, case, result, expected):
        nonlocal fails
        got = score_case(case, result)
        ok = got == expected
        status = "PASS" if ok else "FAIL"
        if not ok:
            fails += 1
        print(f"  {status}  {label:42s}  got={got}  expected={expected}")

    # 1. no_tool, correct — no call happened
    check("no_tool / no call",
          _mk_case(), _mk_result(),
          {"routing": True, "task_success": True, "latency": True})

    # 2. no_tool, wrong — a tool was called
    check("no_tool / tool fired (bad)",
          _mk_case(),
          _mk_result(tool_called="do", tool_args={"action": "search", "query": "x"}),
          {"routing": False, "task_success": False, "latency": True})

    # 3. expected action, correct
    check("action=browse / correct",
          _mk_case(expected={"action": "browse", "query_contains": ["drone"]},
                   success={"mode": "exact"}),
          _mk_result(tool_called="do", tool_args={"action": "browse", "query": "find drone stuff"}),
          {"routing": True, "task_success": True, "latency": True})

    # 4. expected action, wrong action
    check("action=browse / got search",
          _mk_case(expected={"action": "browse", "query_contains": ["drone"]},
                   success={"mode": "exact"}),
          _mk_result(tool_called="do", tool_args={"action": "search", "query": "find drone stuff"}),
          {"routing": False, "task_success": True, "latency": True})

    # 5. expected action, missing substring
    check("action=browse / query missing sub",
          _mk_case(expected={"action": "browse", "query_contains": ["drone", "lidar"]},
                   success={"mode": "exact"}),
          _mk_result(tool_called="do", tool_args={"action": "browse", "query": "drone only"}),
          {"routing": False, "task_success": True, "latency": True})

    # 6. contains predicate met
    check("contains / predicate in result",
          _mk_case(expected={"action": "calendar"},
                   success={"mode": "contains", "predicate": "meeting"}),
          _mk_result(tool_called="do", tool_args={"action": "calendar"},
                     handler_result="you have a meeting at 10"),
          {"routing": True, "task_success": True, "latency": True})

    # 7. contains predicate missing
    check("contains / predicate absent",
          _mk_case(expected={"action": "calendar"},
                   success={"mode": "contains", "predicate": "meeting"}),
          _mk_result(tool_called="do", tool_args={"action": "calendar"},
                     handler_result="nothing today"),
          {"routing": True, "task_success": False, "latency": True})

    # 8. latency over budget
    check("latency / over budget",
          _mk_case(latency_budget_ms=500),
          _mk_result(latency_ms=1500),
          {"routing": True, "task_success": True, "latency": False})

    # 9. no_tool / gate blocked
    check("no_tool / gate blocked (also passes)",
          _mk_case(),
          _mk_result(tool_called="do", tool_args={"action": "search"}, gate_blocked=True),
          {"routing": False, "task_success": True, "latency": True})

    # 10. session arg match
    check("session arg match",
          _mk_case(expected={"action": "code", "query_contains": ["nexus"], "session": "last"},
                   success={"mode": "exact"}),
          _mk_result(tool_called="do", tool_args={"action": "code", "query": "nexus", "session": "last"}),
          {"routing": True, "task_success": True, "latency": True})

    # 11. session arg mismatch
    check("session arg mismatch",
          _mk_case(expected={"action": "code", "query_contains": ["nexus"], "session": "last"},
                   success={"mode": "exact"}),
          _mk_result(tool_called="do", tool_args={"action": "code", "query": "nexus", "session": "new"}),
          {"routing": False, "task_success": True, "latency": True})

    print()
    print(f"  {'PASS' if fails == 0 else 'FAIL'}: {fails} failures")
    if fails:
        raise SystemExit(1)


if __name__ == "__main__":
    _test()
