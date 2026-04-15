#!/usr/bin/env python3
"""
run.py — Plan 2 text-mode eval harness for Nexus.

Loads eval/cases.yaml, runs each case through a Gemini Live text
session using slim's exact SYSTEM_PROMPT + TOOL_DECLARATIONS, invokes
handle_tool in-process for the function_calls Gemini emits, scores
each case with eval/score.py + eval/judge.py, and writes:

    eval/plan2_run.jsonl      — one line per case (raw results)
    eval/plan2_baseline.md    — human-readable scorecard

Usage:
    python eval/run.py                       # default: no browse, N=1
    python eval/run.py --with-browse         # include browse cases
    python eval/run.py --repeats 5           # stability sweep
    python eval/run.py --only knowledge,morning  # run specific buckets
    python eval/run.py --dry-run             # mock handler, no side effects
    python eval/run.py --no-judge            # skip Haiku judge (judge cases → FAIL)

Invariants:
    • eval/run.py does NOT modify any slim code.
    • SYSTEM_PROMPT, TOOL_DECLARATIONS, handle_tool, ACTION_GATE, and
      _transcript_has_trigger are imported from jarvis_slim — so
      what we test is exactly what slim dispatches in production.
    • Each case uses a fresh Gemini Live session (no state bleed).
    • The trigger-word gate is applied in-harness, same logic as
      slim's receive loop, so gated false-positives are caught.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Load .env BEFORE importing anything that needs keys.
_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env", override=True)

# Make slim's modules importable.
sys.path.insert(0, str(_ROOT / "voice"))
sys.path.insert(0, str(_ROOT / "scripts"))

from google import genai
from google.genai import types
from loguru import logger

import jarvis_slim  # noqa: E402
from jarvis_slim import (  # noqa: E402
    SYSTEM_PROMPT,
    TOOL_DECLARATIONS,
    ACTION_GATE,
    _transcript_has_trigger,
    handle_tool,
)

from score import score_case  # noqa: E402


# =============================================================================
# Gemini config — non-Live text mode, same schema + prompt as slim
# =============================================================================

# Slim's production path uses gemini-2.5-flash-native-audio-preview-12-2025
# over the Live bidi API with audio modality. That model is audio-only and
# rejects text-mode Live connects ("Cannot extract voices from a non-audio
# request"). For Plan 2 we skip Live entirely and use
# `client.aio.models.generate_content` with the same tool schema and
# system prompt. Advantages:
#
#   - deterministic request/response (no draining a session stream)
#   - works offline-friendly (no WebSocket, no audio preamble)
#   - same tool-calling surface as Live — function_call parts in
#     the response, same Content/Part shape for multi-turn history
#   - simpler multi-turn context: just prepend prior turns to the
#     `contents` list, no send_client_content sequencing
#
# Trade-off: the model is gemini-2.5-flash (text-capable), not slim's
# native-audio preview. The routing schema is identical, so tool
# decisions should transfer; Plan 3 real-audio will measure any
# delta that matters.

_MODEL_ID = "gemini-2.5-flash"

_GENAI_CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    tools=[types.Tool(function_declarations=TOOL_DECLARATIONS)],
)


# =============================================================================
# Per-case runner
# =============================================================================

_BROWSE_ACTIONS = {"browse", "search", "navigate"}


def _build_history(case: dict) -> list:
    """Return the list of Content entries to prepend to the scored turn."""
    history = []
    for turn in case.get("context") or []:
        if "user" in turn:
            history.append(types.Content(
                role="user",
                parts=[types.Part(text=turn["user"])],
            ))
        if "assistant" in turn:
            history.append(types.Content(
                role="model",
                parts=[types.Part(text=turn["assistant"])],
            ))
    return history


async def run_case(client, case: dict, dry_run: bool = False) -> dict:
    """
    Run one case through a fresh generate_content call. Returns a
    result dict with routing, latency, text, and any error.

    Flow:
      1. Build contents = [history..., scored_utterance].
      2. Call generate_content; inspect response for function_call.
      3. If present: apply trigger gate, invoke handler, capture
         result. (One round — we don't loop on follow-up tool calls
         in Plan 2; a case is scored on its first dispatch.)
      4. Capture any text parts as assistant_text.
      5. Stop the clock.
    """
    result: dict = {
        "id": case["id"],
        "bucket": case["bucket"],
        "utterance": case["utterance"],
        "latency_ms": 0.0,
        "tool_called": None,
        "tool_args": {},
        "assistant_text": "",
        "handler_result": "",
        "gate_blocked": False,
        "error": None,
    }

    contents = _build_history(case) + [
        types.Content(
            role="user",
            parts=[types.Part(text=case["utterance"])],
        ),
    ]

    dispatch_start = time.perf_counter()
    try:
        resp = await client.aio.models.generate_content(
            model=_MODEL_ID,
            contents=contents,
            config=_GENAI_CONFIG,
        )
    except Exception as e:
        result["error"] = f"genai: {e}"
        result["latency_ms"] = round(
            (time.perf_counter() - dispatch_start) * 1000.0, 1
        )
        return result

    # Walk the candidates for function_calls + text
    for cand in resp.candidates or []:
        if not cand.content or not cand.content.parts:
            continue
        for part in cand.content.parts:
            fc = getattr(part, "function_call", None)
            if fc and fc.name:
                result["tool_called"] = fc.name
                args = dict(fc.args) if fc.args else {}
                result["tool_args"] = args
                action = (args.get("action") or "").lower().strip()

                # Trigger-word gate — same logic slim applies in its
                # receive loop.
                gated = action in ACTION_GATE
                has_trigger = _transcript_has_trigger(case["utterance"])
                if gated and case["utterance"] and not has_trigger:
                    result["gate_blocked"] = True
                    result["handler_result"] = (
                        "No trigger word heard. Say jarvis or nexus first."
                    )
                elif dry_run:
                    result["handler_result"] = f"[dry-run] would call {action}"
                else:
                    try:
                        r, _is_long = await asyncio.to_thread(
                            handle_tool,
                            action,
                            args.get("query", ""),
                            args.get("session", ""),
                        )
                        result["handler_result"] = r
                    except Exception as e:
                        result["error"] = f"handler: {e}"
                        result["handler_result"] = f"ERROR: {e}"
                break  # score on first function_call
            elif getattr(part, "text", None):
                result["assistant_text"] += part.text

    result["latency_ms"] = round(
        (time.perf_counter() - dispatch_start) * 1000.0, 1
    )
    return result


# =============================================================================
# Full sweep
# =============================================================================

async def sweep(cases: list[dict], *, with_browse: bool, dry_run: bool,
                repeats: int, only: set[str] | None, judge) -> list[dict]:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    filtered = []
    for case in cases:
        if only and case["bucket"] not in only:
            continue
        if not with_browse:
            expected_action = (case.get("expected") or {}).get("action") or ""
            if expected_action.lower() in _BROWSE_ACTIONS:
                continue
        filtered.append(case)

    print(f"\n  Running {len(filtered)} cases "
          f"(repeats={repeats}, with_browse={with_browse}, dry_run={dry_run})\n")

    all_results: list[dict] = []

    for case in filtered:
        case_results = []
        for rep in range(repeats):
            try:
                r = await run_case(client, case, dry_run=dry_run)
            except Exception as e:
                r = {
                    "id": case["id"],
                    "bucket": case["bucket"],
                    "utterance": case["utterance"],
                    "error": f"runner: {e}\n{traceback.format_exc()}",
                    "latency_ms": 0.0,
                    "tool_called": None,
                    "tool_args": {},
                    "assistant_text": "",
                    "handler_result": "",
                    "gate_blocked": False,
                }
            r["rep"] = rep
            r["scoring"] = score_case(case, r, judge=judge)
            case_results.append(r)

            scoring = r["scoring"]
            flag = "✓" if all(scoring.values()) else "✗"
            err = f"  err={r['error']}" if r.get("error") else ""
            print(
                f"  {flag} {case['id']:14s} rep{rep}  "
                f"route={'Y' if scoring['routing'] else 'N'}  "
                f"succ={'Y' if scoring['task_success'] else 'N'}  "
                f"lat={'Y' if scoring['latency'] else 'N'}  "
                f"{r['latency_ms']:>8.1f}ms  "
                f"call={(r['tool_args'].get('action') if r['tool_args'] else '-') or '-'}"
                f"{err}"
            )

        all_results.extend(case_results)

    return all_results


# =============================================================================
# Scorecard report
# =============================================================================

def write_scorecard(cases: list[dict], results: list[dict], out_path: Path) -> dict:
    from collections import defaultdict

    # Group by case id for per-case aggregates; cases may have N reps.
    by_case: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_case[r["id"]].append(r)

    case_index = {c["id"]: c for c in cases}

    totals = {"routing": 0, "task_success": 0, "latency": 0, "all": 0, "n": 0}
    buckets: dict[str, dict] = defaultdict(lambda: {"n": 0, "routing": 0, "task_success": 0, "latency": 0, "all": 0})
    failures: list[tuple] = []

    for cid, rs in by_case.items():
        case = case_index.get(cid)
        if case is None:
            continue
        n_reps = len(rs)
        # Use majority vote across reps for the aggregate per-case view.
        def maj(key: str) -> bool:
            ones = sum(1 for r in rs if r["scoring"].get(key))
            return ones * 2 >= n_reps
        routing = maj("routing")
        task = maj("task_success")
        latency = maj("latency")
        all_pass = routing and task and latency

        bucket = case["bucket"]
        buckets[bucket]["n"] += 1
        buckets[bucket]["routing"] += int(routing)
        buckets[bucket]["task_success"] += int(task)
        buckets[bucket]["latency"] += int(latency)
        buckets[bucket]["all"] += int(all_pass)

        totals["n"] += 1
        totals["routing"] += int(routing)
        totals["task_success"] += int(task)
        totals["latency"] += int(latency)
        totals["all"] += int(all_pass)

        if not all_pass:
            # Pick the first representative result for the failure
            # report.
            example = rs[0]
            failures.append((bucket, cid, case, example, {
                "routing": routing, "task_success": task, "latency": latency,
            }))

    def pct(num: int, den: int) -> str:
        return f"{(100.0*num/den):.0f}%" if den else "—"

    lines = [
        "# Plan 2 — Baseline scorecard",
        "",
        f"- Date: {time.strftime('%Y-%m-%d %H:%M')}",
        f"- Cases (unique): **{totals['n']}**",
        f"- Repetitions per case: {len(next(iter(by_case.values()), []))}",
        "",
        "## Headline",
        "",
        "| Axis | Pass rate |",
        "|---|---:|",
        f"| Routing | **{pct(totals['routing'], totals['n'])}** ({totals['routing']}/{totals['n']}) |",
        f"| Task success | **{pct(totals['task_success'], totals['n'])}** ({totals['task_success']}/{totals['n']}) |",
        f"| Latency | **{pct(totals['latency'], totals['n'])}** ({totals['latency']}/{totals['n']}) |",
        f"| All three | **{pct(totals['all'], totals['n'])}** ({totals['all']}/{totals['n']}) |",
        "",
        "## By bucket",
        "",
        "| Bucket | n | Routing | Success | Latency | All |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for bucket, s in sorted(buckets.items()):
        lines.append(
            f"| {bucket} | {s['n']} | "
            f"{pct(s['routing'], s['n'])} | "
            f"{pct(s['task_success'], s['n'])} | "
            f"{pct(s['latency'], s['n'])} | "
            f"{pct(s['all'], s['n'])} |"
        )

    # Failures grouped by bucket
    if failures:
        lines.append("")
        lines.append("## Failing cases")
        lines.append("")
        failures.sort(key=lambda x: (x[0], x[1]))
        for bucket, cid, case, example, axes in failures:
            fail_axes = [a for a, ok in axes.items() if not ok]
            tag = ",".join(fail_axes)
            lines.append(f"### `{cid}` ({bucket}) — {tag}")
            lines.append("")
            lines.append(f"- Utterance: `{case['utterance']}`")
            exp = case.get("expected") or {}
            lines.append(
                f"- Expected: action=`{exp.get('action')}`, "
                f"query_contains=`{exp.get('query_contains')}`, "
                f"session=`{exp.get('session')}`"
            )
            args = example.get("tool_args") or {}
            lines.append(
                f"- Actual: action=`{args.get('action')}`, "
                f"query=`{args.get('query')}`, "
                f"session=`{args.get('session')}`, "
                f"latency=`{example.get('latency_ms')}ms`, "
                f"gate_blocked=`{example.get('gate_blocked')}`"
            )
            if example.get("assistant_text"):
                lines.append(f"- Text: `{example['assistant_text'][:200]}`")
            if example.get("error"):
                lines.append(f"- Error: `{example['error']}`")
            lines.append("")

    out_path.write_text("\n".join(lines))
    return {
        "totals": totals,
        "buckets": dict(buckets),
        "failures": len(failures),
    }


# =============================================================================
# Main
# =============================================================================

async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-browse", action="store_true",
                    help="Include browse/search/navigate cases (slow; spawns Claude)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Mock handle_tool with stub results — no side effects")
    ap.add_argument("--repeats", type=int, default=1,
                    help="Repetitions per case (for stability)")
    ap.add_argument("--only", type=str, default=None,
                    help="Comma-separated list of bucket IDs to run")
    ap.add_argument("--no-judge", action="store_true",
                    help="Skip Haiku judge (judge cases fail)")
    args = ap.parse_args()

    # Load cases
    with open(_ROOT / "eval" / "cases.yaml") as f:
        data = yaml.safe_load(f)
    cases = data["cases"]

    only: set[str] | None = None
    if args.only:
        only = {b.strip() for b in args.only.split(",") if b.strip()}

    # Judge setup
    judge = None
    if not args.no_judge:
        try:
            from judge import Judge
            judge = Judge()
            print("  Judge: Haiku enabled")
        except Exception as e:
            print(f"  Judge: disabled ({e})")

    results = await sweep(
        cases,
        with_browse=args.with_browse,
        dry_run=args.dry_run,
        repeats=args.repeats,
        only=only,
        judge=judge,
    )

    # Write JSONL
    jsonl_path = _ROOT / "eval" / "plan2_run.jsonl"
    with open(jsonl_path, "w") as f:
        for r in results:
            f.write(json.dumps(r, default=str) + "\n")
    print(f"\n  JSONL: {jsonl_path}")

    # Scorecard
    md_path = _ROOT / "eval" / "plan2_baseline.md"
    summary = write_scorecard(cases, results, md_path)
    print(f"  Scorecard: {md_path}")

    t = summary["totals"]
    print(f"\n  {'SUMMARY':-^60}")
    print(f"  Routing:      {t['routing']}/{t['n']}")
    print(f"  Task success: {t['task_success']}/{t['n']}")
    print(f"  Latency:      {t['latency']}/{t['n']}")
    print(f"  All three:    {t['all']}/{t['n']}")
    if judge:
        print(f"  Judge calls:  {judge.calls}")


if __name__ == "__main__":
    # Quiet slim's loguru output during eval — we print our own.
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    asyncio.run(main())
