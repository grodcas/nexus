#!/usr/bin/env python3
"""
Layer 1 Tests — State machine + ClaudeSession + search. No API calls.

Usage: cd ~/nexus && python voice/test_layer1.py
"""

import json
import os
import sys
import tempfile
import time
import types

# ── Stub pipecat imports so jarvis.py can be parsed ─────────────────────────

stub_modules = [
    "pipecat", "pipecat.audio", "pipecat.audio.vad", "pipecat.audio.vad.silero",
    "pipecat.frames", "pipecat.frames.frames", "pipecat.pipeline",
    "pipecat.pipeline.pipeline", "pipecat.pipeline.runner", "pipecat.pipeline.task",
    "pipecat.processors", "pipecat.processors.aggregators",
    "pipecat.processors.aggregators.llm_context",
    "pipecat.processors.aggregators.llm_response_universal",
    "pipecat.services", "pipecat.services.google",
    "pipecat.services.google.gemini_live", "pipecat.services.google.gemini_live.llm",
    "pipecat.services.llm_service",
    "pipecat.transports", "pipecat.transports.local", "pipecat.transports.local.audio",
    "pipecat.turns", "pipecat.turns.user_mute",
    "pipecat.turns.user_mute.always_user_mute_strategy",
]

for mod_name in stub_modules:
    if mod_name not in sys.modules:
        sys.modules[mod_name] = types.ModuleType(mod_name)

dummy_attrs = [
    "SileroVADAnalyzer", "LLMRunFrame", "EndFrame", "Pipeline", "PipelineRunner",
    "PipelineParams", "PipelineTask", "LLMContext",
    "LLMContextAggregatorPair", "LLMUserAggregatorParams",
    "GeminiLiveLLMService", "GeminiVADParams", "FunctionCallParams",
    "LocalAudioTransport", "LocalAudioTransportParams", "AlwaysUserMuteStrategy",
]
for attr in dummy_attrs:
    for mod_name in stub_modules:
        setattr(sys.modules[mod_name], attr, type(attr, (), {}))

sys.path.insert(0, "/Users/gines/nexus/voice")
from jarvis import App, AppState, ClaudeSession, search_worktree
import jarvis


# ── Mock stream-json events ─────────────────────────────────────────────────

EVENTS_SIMPLE_TASK = [
    {"type": "system", "subtype": "init", "session_id": "test-session-123"},
    {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "I'll create the script."}]},
    },
    {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/nexus/README.md"}}]},
    },
    {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Write", "input": {"file_path": "/nexus/hello.py", "content": "print('hello')"}}]},
    },
    {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "python hello.py"}}]},
    },
    {"type": "result", "result": "Created hello.py.", "duration_ms": 8500, "total_cost_usd": 0.004},
]


# ── Helpers ──────────────────────────────────────────────────────────────────

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} — {detail}")


def feed_events(session: ClaudeSession, events: list):
    session.status = "working"
    session.started_at = time.time() - 5
    for event in events:
        session.events.append(event)
        session._update_status(event)


# ── Tests: State machine ────────────────────────────────────────────────────

def test_state_machine():
    print("\n── State machine transitions ──")

    a = App()
    check("starts in GEMINI", a.state == AppState.GEMINI)
    check("no active project", a.active_project is None)

    # Enter confirming
    a.pending_action = "enter:nexus"
    a.state = AppState.CONFIRMING
    check("confirming state", a.state == AppState.CONFIRMING)
    check("pending action set", a.pending_action == "enter:nexus")

    # Confirm → Claude mode
    a.active_project = "nexus"
    a.active_project_path = os.path.expanduser("~/nexus")
    a.state = AppState.CLAUDE
    a.pending_action = None
    check("claude state", a.state == AppState.CLAUDE)
    check("project set", a.active_project == "nexus")

    # Pause
    a.previous_state = a.state
    a.state = AppState.PAUSED
    check("paused", a.state == AppState.PAUSED)
    check("previous state saved", a.previous_state == AppState.CLAUDE)

    # Wake up
    a.state = a.previous_state
    check("resumed to claude", a.state == AppState.CLAUDE)

    # Exit confirming
    a.pending_action = "exit"
    a.state = AppState.CONFIRMING
    check("exit confirming", a.pending_action == "exit")

    # Confirm exit
    a.active_project = None
    a.active_project_path = None
    a.state = AppState.GEMINI
    a.pending_action = None
    check("back to gemini", a.state == AppState.GEMINI)
    check("project cleared", a.active_project is None)


def test_status_summary():
    print("\n── Status summary ──")

    a = App()
    check("gemini summary", "GEMINI" in a.status_summary())

    a.state = AppState.PAUSED
    check("paused summary", "PAUSED" in a.status_summary())

    a.state = AppState.CONFIRMING
    a.pending_action = "enter:nexus"
    check("confirming summary", "CONFIRMING" in a.status_summary() and "enter:nexus" in a.status_summary())

    a.state = AppState.CLAUDE
    a.active_project = "nexus"
    check("claude summary", "CLAUDE" in a.status_summary() and "nexus" in a.status_summary())


# ── Tests: Session persistence ───────────────────────────────────────────────

def test_session_persistence():
    print("\n── Session persistence ──")

    with tempfile.TemporaryDirectory() as tmpdir:
        old_file = jarvis.SESSIONS_FILE
        jarvis.SESSIONS_FILE = os.path.join(tmpdir, "sessions.json")

        jarvis.save_session("nexus", "abc-123")
        check("save session", os.path.exists(jarvis.SESSIONS_FILE))

        sid = jarvis.get_last_session("nexus")
        check("load session", sid == "abc-123", f"got: {sid}")

        check("no session for unknown", jarvis.get_last_session("unknown") is None)

        jarvis.save_session("nexus", "def-456")
        sid = jarvis.get_last_session("nexus")
        check("overwrite session", sid == "def-456", f"got: {sid}")

        jarvis.SESSIONS_FILE = old_file


# ── Tests: ClaudeSession ────────────────────────────────────────────────────

def test_claude_session_id_capture():
    print("\n── Session ID capture ──")

    s = ClaudeSession()
    feed_events(s, EVENTS_SIMPLE_TASK)
    check("captures session_id", s.session_id == "test-session-123", f"got: {s.session_id}")


def test_status_tracking():
    print("\n── Status tracking ──")

    s = ClaudeSession()
    feed_events(s, EVENTS_SIMPLE_TASK)
    check("final status done", s.status == "done")
    check("result text", "hello.py" in s.result_text)
    check("current_action finished", s.current_action == "finished")
    check("all events stored", len(s.events) == len(EVENTS_SIMPLE_TASK))


def test_action_labels():
    print("\n── Action labels ──")

    s = ClaudeSession()
    s.status = "working"
    s.started_at = time.time()

    s._update_status(EVENTS_SIMPLE_TASK[1])  # text
    check("thinking label", "thinking:" in s.current_action)

    s._update_status(EVENTS_SIMPLE_TASK[2])  # Read
    check("read label", "reading" in s.current_action)

    s._update_status(EVENTS_SIMPLE_TASK[3])  # Write
    check("write label", "writing" in s.current_action)

    s._update_status(EVENTS_SIMPLE_TASK[4])  # Bash
    check("bash label", "running:" in s.current_action)


def test_progress_messages():
    print("\n── Progress messages ──")

    s = ClaudeSession()
    check("idle message", "Ready" in s.get_progress() or "No task" in s.get_progress())

    s.status = "working"
    s.instruction = "create script"
    s.current_action = "writing hello.py"
    s.started_at = time.time() - 10
    s.events = EVENTS_SIMPLE_TASK[:4]
    progress = s.get_progress()
    check("working shows instruction", "create script" in progress)
    check("working shows action", "writing hello.py" in progress)

    s2 = ClaudeSession()
    feed_events(s2, EVENTS_SIMPLE_TASK)
    s2.instruction = "create script"
    progress = s2.get_progress()
    check("done shows result", "hello.py" in progress)

    s3 = ClaudeSession()
    s3.status = "error"
    s3.current_action = "error: timeout"
    check("error message", "timeout" in s3.get_progress())


def test_is_busy():
    print("\n── Busy state ──")

    s = ClaudeSession()
    check("idle not busy", not s.is_busy)
    s.status = "working"
    check("working is busy", s.is_busy)
    s.status = "done"
    check("done not busy", not s.is_busy)


# ── Tests: Document search ───────────────────────────────────────────────────

def test_search_worktree():
    print("\n── Document search ──")

    with tempfile.TemporaryDirectory() as tmpdir:
        old_root = jarvis.WORKTREE_ROOT
        jarvis.WORKTREE_ROOT = tmpdir

        with open(os.path.join(tmpdir, "physics.md"), "w") as f:
            f.write("- Mechanics lecture notes on PID control theory\n")
            f.write("- Thermodynamics final exam 2023\n")

        with open(os.path.join(tmpdir, "projects.md"), "w") as f:
            f.write("- Drone autopilot PID tuning Arduino\n")
            f.write("- Thermal camera Python OpenCV project\n")

        result = search_worktree("PID control")
        check("all-words match", "PID" in result and "control" in result)

        result = search_worktree("thermal")
        check("single word", "thermal" in result.lower())

        result = search_worktree("PID")
        lines = [l for l in result.split("\n") if l.strip()]
        check("multiple results", len(lines) == 2, f"got {len(lines)}")

        result = search_worktree("quantum entanglement")
        check("no results", "Nothing found" in result or "No results" in result)

        result = search_worktree("quantum thermodynamics")
        check("fallback any-word", "thermodynamics" in result.lower())

        jarvis.WORKTREE_ROOT = old_root


# ── Tests: Project context ───────────────────────────────────────────────────

def test_load_project_context():
    print("\n── Project context ──")

    ctx = jarvis.load_project_context(os.path.expanduser("~/nexus"))
    check("branch loaded", len(ctx.get("branch", "")) > 0, f"got: {ctx.get('branch')}")
    check("git_status loaded", "git_status" in ctx)
    check("recent_commits loaded", "recent_commits" in ctx)
    # CLAUDE.md may or may not exist
    check("context is dict", isinstance(ctx, dict))


# ── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("LAYER 1 — State Machine + ClaudeSession + Search")
    print("=" * 60)

    test_state_machine()
    test_status_summary()
    test_session_persistence()
    test_claude_session_id_capture()
    test_status_tracking()
    test_action_labels()
    test_progress_messages()
    test_is_busy()
    test_search_worktree()
    test_load_project_context()

    print(f"\n{'=' * 60}")
    print(f"Results: {PASS} passed, {FAIL} failed")
    print(f"{'=' * 60}")
    sys.exit(1 if FAIL > 0 else 0)
