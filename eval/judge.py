#!/usr/bin/env python3
"""
judge.py — Haiku-backed grader for open-ended eval cases.

Used by score.py when case.success.mode == 'judge'. Sends a short
prompt containing the case id, utterance, rubric, and observed
behavior, and asks for one of PASS / FAIL.

Cost: ~200 input tokens + 4 output tokens per call. A full Plan 2
sweep uses the judge on ~40 cases, so under $0.01 per run at
Haiku's current pricing. Cheap enough to fire on every iteration.

Environment:
    ANTHROPIC_API_KEY — required. Loaded from .env at the repo root
    via python-dotenv (already in Nexus's venv).

CLI self-test:
    ANTHROPIC_API_KEY=... python eval/judge.py
"""

from __future__ import annotations

import os
import sys
from typing import Any

from dotenv import load_dotenv

# Load .env from repo root so `ANTHROPIC_API_KEY` is available.
_ENV_PATH = os.path.join(os.path.dirname(__file__), "..", ".env")
load_dotenv(_ENV_PATH, override=True)


_DEFAULT_MODEL = "claude-haiku-4-5-20251001"


class Judge:
    """
    Wraps the Anthropic SDK with a fixed single-word rubric prompt.

    .grade(case, result) returns True on PASS, False on FAIL or any
    SDK error. Judge failures never silently pass a case.
    """

    def __init__(self, model: str = _DEFAULT_MODEL, verbose: bool = False):
        from anthropic import Anthropic
        self._client = Anthropic()
        self._model = model
        self._verbose = verbose
        self.calls = 0

    def grade(self, case: dict, result: dict) -> bool:
        rubric = (case.get("success") or {}).get("predicate") or ""
        utterance = case.get("utterance", "")
        tool_args = result.get("tool_args") or {}
        # Nexus has a single function named `do`; the semantically
        # meaningful "tool" is the action argument.
        action = (tool_args.get("action") or "").lower().strip() or "(none)"
        query = tool_args.get("query") or ""
        sess = tool_args.get("session") or ""
        assistant_text = (result.get("assistant_text") or "")[:500]
        handler_result = (result.get("handler_result") or "")[:500]
        gate_blocked = bool(result.get("gate_blocked"))
        no_call = result.get("tool_called") is None

        prompt = (
            "You are grading one case from a voice-agent eval suite.\n"
            "Reply with exactly one word: PASS or FAIL.\n\n"
            f"CASE ID: {case.get('id', '?')}\n"
            f"USER UTTERANCE: {utterance!r}\n\n"
            f"RUBRIC: {rubric}\n\n"
            "ACTUAL BEHAVIOR:\n"
            f"  action called: {'(none — answered conversationally)' if no_call else action}\n"
            f"  query arg:     {query!r}\n"
            f"  session arg:   {sess!r}\n"
            f"  gate blocked:  {gate_blocked}\n"
            f"  handler result: {handler_result!r}\n"
            f"  assistant text: {assistant_text!r}\n\n"
            "Note: when the rubric mentions a tool by name (e.g. 'Action "
            "is calendar'), it refers to the `action called` field above — "
            "Nexus has a single function, 'do', with an `action` argument.\n\n"
            "If the rubric is satisfied by the actual behavior, reply PASS.\n"
            "Otherwise reply FAIL.\n"
        )

        try:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=8,
                messages=[{"role": "user", "content": prompt}],
            )
            self.calls += 1
            # Extract the first text block
            raw = ""
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    raw += block.text
            verdict = raw.strip().upper()
            if self._verbose:
                print(f"  [judge] {case.get('id')}: {verdict!r}")
            return verdict.startswith("PASS")
        except Exception as e:
            # Never silently pass on a judge failure.
            sys.stderr.write(f"[judge] {case.get('id')} error: {e}\n")
            return False


# =============================================================================
# CLI self-test
# =============================================================================

def _test() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set — skipping live tests")
        return

    judge = Judge(verbose=True)
    fails = 0

    cases_results = [
        (
            # Clear PASS — rubric matches behavior
            {
                "id": "test-pass",
                "utterance": "jarvis what's on my calendar",
                "success": {
                    "mode": "judge",
                    "predicate": "Tool call is `calendar`. Spoken summary covers the day.",
                },
            },
            {
                "tool_called": "do",
                "tool_args": {"action": "calendar", "query": ""},
                "handler_result": "10am design review, 2pm 1:1 with Marta.",
                "assistant_text": "You have two meetings today.",
                "gate_blocked": False,
            },
            True,
        ),
        (
            # Clear FAIL — tool call is wrong
            {
                "id": "test-fail",
                "utterance": "jarvis what's on my calendar",
                "success": {
                    "mode": "judge",
                    "predicate": "Tool call is `calendar`. Spoken summary covers the day.",
                },
            },
            {
                "tool_called": "do",
                "tool_args": {"action": "search", "query": "calendar"},
                "handler_result": "opened google.com",
                "assistant_text": "Searching.",
                "gate_blocked": False,
            },
            False,
        ),
        (
            # Knowledge no-tool case with judge grading
            {
                "id": "test-knowledge",
                "utterance": "how does TDLAS work",
                "success": {
                    "mode": "judge",
                    "predicate": "Answer explains tunable diode laser absorption. No tool call.",
                },
            },
            {
                "tool_called": None,
                "tool_args": {},
                "handler_result": "",
                "assistant_text": "TDLAS uses a tunable diode laser tuned to a gas absorption line; the amount of light absorbed reveals concentration.",
                "gate_blocked": False,
            },
            True,
        ),
    ]

    for case, result, expected in cases_results:
        got = judge.grade(case, result)
        ok = got == expected
        status = "PASS" if ok else "FAIL"
        if not ok:
            fails += 1
        print(f"  {status}  {case['id']:20s}  expected={expected}  got={got}")

    print(f"\n  {judge.calls} judge calls, {fails} failures")
    if fails:
        raise SystemExit(1)


if __name__ == "__main__":
    _test()
