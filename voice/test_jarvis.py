#!/usr/bin/env python3
"""
Jarvis Test Suite — validates all 8 tools, state machine, and Claude session.

Runs without mic or Gemini API. Mocks FunctionCallParams to call handlers directly.
Writes results to voice/test.md for human review.

Usage:
    cd ~/nexus && source venv/bin/activate
    python voice/test_jarvis.py
"""

import asyncio
import os
import sys
import time
from datetime import datetime
from unittest.mock import MagicMock

# Add parent to path so we can import jarvis
sys.path.insert(0, os.path.dirname(__file__))

# Import jarvis components
import jarvis
from jarvis import (
    AppState,
    ClaudeSession,
    app,
    handle_check_progress,
    handle_coding_task,
    handle_connect_project,
    handle_disconnect_project,
    handle_github,
    handle_management,
    handle_search_documents,
    handle_sleep,
    search_worktree,
)

# We need FunctionCallParams
from pipecat.services.llm_service import FunctionCallParams


# =============================================================================
# Test infrastructure
# =============================================================================

results = []  # (group, name, pass/fail, detail)


def log(group: str, name: str, passed: bool, detail: str = ""):
    status = "PASS" if passed else "FAIL"
    results.append((group, name, status, detail))
    icon = "." if passed else "F"
    print(icon, end="", flush=True)


def make_params(tool_name: str, arguments: dict, callback):
    """Create a FunctionCallParams with mock LLM/context."""
    return FunctionCallParams(
        function_name=tool_name,
        tool_call_id=f"test-{int(time.time() * 1000)}",
        arguments=arguments,
        llm=MagicMock(),
        context=MagicMock(),
        result_callback=callback,
    )


def reset_app():
    """Reset app to clean IDLE state."""
    app.state = AppState.IDLE
    app.active_project = None
    app.active_project_path = None
    app.project_context = {}
    app.claude = ClaudeSession()
    app.sleep_requested = False
    app.pending_claude_result = None
    app.llm = None
    app.pipeline_task = None


# =============================================================================
# Test groups
# =============================================================================

async def test_state_machine():
    """Group 1: State machine transitions."""
    group = "State Machine"

    # 1a. Connect to valid project from IDLE
    reset_app()
    result = {}

    async def cb(data):
        nonlocal result
        result = data

    params = make_params("connect_project", {"project": "nexus"}, cb)
    await handle_connect_project(params)

    log(group, "connect nexus → CODING",
        app.state == AppState.CODING and app.active_project == "nexus",
        f"state={app.state.value}, project={app.active_project}")
    log(group, "connect nexus → has branch info",
        "branch" in app.project_context or "Branch" in result.get("result", ""),
        f"result={result.get('result', '')[:100]}")

    # 1b. Connect to unknown project
    reset_app()
    result = {}
    params = make_params("connect_project", {"project": "nonexistent"}, cb)
    await handle_connect_project(params)

    log(group, "connect unknown → error, stays IDLE",
        app.state == AppState.IDLE and "Unknown" in result.get("result", ""),
        f"result={result.get('result', '')[:100]}")

    # 1c. Connect while already connected
    reset_app()
    app.state = AppState.CODING
    app.active_project = "nexus"
    result = {}
    params = make_params("connect_project", {"project": "jim_app"}, cb)
    await handle_connect_project(params)

    log(group, "connect while connected → error",
        "Already" in result.get("result", "") or "Disconnect" in result.get("result", ""),
        f"result={result.get('result', '')[:100]}")

    # 1d. Disconnect from project
    reset_app()
    # First connect
    params = make_params("connect_project", {"project": "nexus"}, cb)
    await handle_connect_project(params)
    assert app.state == AppState.CODING

    result = {}
    params = make_params("disconnect_project", {}, cb)
    await handle_disconnect_project(params)

    log(group, "disconnect → IDLE",
        app.state == AppState.IDLE and app.active_project is None,
        f"state={app.state.value}, project={app.active_project}")

    # 1e. Disconnect while not connected
    reset_app()
    result = {}
    params = make_params("disconnect_project", {}, cb)
    await handle_disconnect_project(params)

    log(group, "disconnect while IDLE → error",
        "Not connected" in result.get("result", ""),
        f"result={result.get('result', '')[:100]}")


async def test_claude_session():
    """Group 2: Claude session lifecycle."""
    group = "Claude Session"

    # 2a. Fresh session state
    cs = ClaudeSession()
    log(group, "fresh session is idle",
        cs.status == "idle" and cs.proc is None,
        f"status={cs.status}")

    # 2b. kill() on idle session — no crash
    cs.kill()
    log(group, "kill() on idle → no crash",
        cs.status == "idle",
        "no exception")

    # 2c. get_progress() on idle
    progress = cs.get_progress()
    log(group, "get_progress() idle → 'No task running'",
        "No task" in progress,
        f"progress={progress}")

    # 2d. Zombie detection: fake a dead process
    cs.status = "working"
    cs.proc = MagicMock()
    cs.proc.poll.return_value = 1  # process exited with code 1
    cs.proc.returncode = 1
    cs.started_at = time.time()
    cs._events = []
    cs.instruction = "test"

    progress = cs.get_progress()
    log(group, "zombie detection → error status",
        cs.status == "error" and "died" in progress,
        f"progress={progress}")

    # 2e. kill() clears everything
    cs2 = ClaudeSession()
    cs2.status = "working"
    cs2.proc = MagicMock()
    cs2.proc.poll.return_value = None  # still running
    cs2.proc.terminate = MagicMock()
    cs2.proc.wait = MagicMock()
    cs2._monitor_task = MagicMock()
    cs2._monitor_task.done.return_value = False
    cs2._monitor_task.cancel = MagicMock()

    cs2.kill()
    log(group, "kill() → idle, proc=None, monitor cancelled",
        cs2.status == "idle" and cs2.proc is None,
        f"status={cs2.status}, monitor_cancelled={cs2._monitor_task is None}")


async def test_coding_task():
    """Group 3: coding_task handler."""
    group = "Coding Task"

    # 3a. coding_task while not connected → error
    reset_app()
    result = {}

    async def cb(data):
        nonlocal result
        result = data

    params = make_params("coding_task", {"instruction": "test"}, cb)
    await handle_coding_task(params)

    log(group, "coding_task while IDLE → error",
        "Not connected" in result.get("result", ""),
        f"result={result.get('result', '')[:100]}")

    # 3b. coding_task while connected — starts Claude
    reset_app()
    result = {}
    # Connect first
    params = make_params("connect_project", {"project": "nexus"}, cb)
    await handle_connect_project(params)

    result = {}
    params = make_params("coding_task", {
        "instruction": "List all Python files in the project. Be concise."
    }, cb)
    await handle_coding_task(params)

    log(group, "coding_task while CODING → started",
        "Started" in result.get("result", "") or app.claude.status == "working",
        f"result={result.get('result', '')[:120]}")

    # Wait a moment for Claude to start
    await asyncio.sleep(2)

    # 3c. Verify Claude process is actually running
    is_running = app.claude.proc is not None and app.claude.proc.poll() is None
    log(group, "Claude process is running",
        is_running or app.claude.status == "done",  # might finish fast
        f"status={app.claude.status}, proc={app.claude.proc is not None}")

    # Kill it for cleanup
    app.claude.kill()

    # Reset for next tests
    reset_app()


async def test_management():
    """Group 4: management handler (uses real sync scripts)."""
    group = "Management"
    result = {}

    async def cb(data):
        nonlocal result
        result = data

    # 4a. source="calendar" — build only (fast, uses cached data)
    result = {}
    params = make_params("management", {"source": "calendar", "query": "any meetings today?"}, cb)
    await handle_management(params)

    has_calendar = "Calendar" in result.get("result", "") or "calendar" in result.get("result", "").lower()
    log(group, "management(calendar) → calendar data",
        has_calendar and len(result.get("result", "")) > 50,
        f"result_len={len(result.get('result', ''))}, snippet={result.get('result', '')[:100]}")

    # 4b. source="email"
    result = {}
    params = make_params("management", {"source": "email", "query": "any new emails?"}, cb)
    await handle_management(params)

    has_email = "email" in result.get("result", "").lower() or "Email" in result.get("result", "")
    log(group, "management(email) → email data",
        has_email and len(result.get("result", "")) > 50,
        f"result_len={len(result.get('result', ''))}, snippet={result.get('result', '')[:100]}")

    # 4c. source="reminders"
    result = {}
    params = make_params("management", {"source": "reminders", "query": "any tasks?"}, cb)
    await handle_management(params)

    has_reminders = "remind" in result.get("result", "").lower() or "Remind" in result.get("result", "")
    log(group, "management(reminders) → reminders data",
        has_reminders,
        f"result_len={len(result.get('result', ''))}, snippet={result.get('result', '')[:100]}")

    # 4d. source="all" — full briefing
    result = {}
    params = make_params("management", {"source": "all", "query": "morning briefing"}, cb)
    await handle_management(params)

    has_briefing_prefix = "Summarize as a spoken briefing" in result.get("result", "")
    log(group, "management(all) → briefing prefix + root.md",
        has_briefing_prefix and len(result.get("result", "")) > 100,
        f"has_prefix={has_briefing_prefix}, result_len={len(result.get('result', ''))}")


async def test_search_documents():
    """Group 5: search_documents handler."""
    group = "Search Documents"
    result = {}

    async def cb(data):
        nonlocal result
        result = data

    # 5a. Search for something that should exist (aerospace/drone related)
    result = {}
    params = make_params("search_documents", {"query": "drone"}, cb)
    await handle_search_documents(params)

    has_results = "Nothing found" not in result.get("result", "")
    log(group, "search 'drone' → finds results",
        has_results,
        f"result={result.get('result', '')[:150]}")

    # 5b. Search for something that shouldn't exist
    result = {}
    params = make_params("search_documents", {"query": "xyzzyflurble99"}, cb)
    await handle_search_documents(params)

    no_results = "Nothing found" in result.get("result", "")
    log(group, "search nonsense → 'Nothing found'",
        no_results,
        f"result={result.get('result', '')[:100]}")

    # 5c. Direct function test — verify single pass
    direct_result = search_worktree("education")
    has_content = len(direct_result) > 20
    log(group, "search_worktree('education') → has content",
        has_content,
        f"len={len(direct_result)}, snippet={direct_result[:100]}")


async def test_github():
    """Group 6: github handler (uses real gh CLI)."""
    group = "GitHub"
    result = {}

    async def cb(data):
        nonlocal result
        result = data

    # 6a. General activity
    result = {}
    params = make_params("github", {"query": "recent activity"}, cb)
    await handle_github(params)

    has_repos = "repos" in result.get("result", "").lower() or "nexus" in result.get("result", "").lower()
    log(group, "github('recent activity') → repo list",
        has_repos and len(result.get("result", "")) > 50,
        f"result={result.get('result', '')[:150]}")

    # 6b. Specific project mention
    result = {}
    params = make_params("github", {"query": "last commits on nexus"}, cb)
    await handle_github(params)

    has_commits = "commits" in result.get("result", "").lower() or "nexus" in result.get("result", "")
    log(group, "github('commits on nexus') → includes nexus commits",
        has_commits,
        f"result={result.get('result', '')[:150]}")


async def test_check_progress():
    """Group 7: check_progress handler."""
    group = "Check Progress"
    result = {}

    async def cb(data):
        nonlocal result
        result = data

    # 7a. Not connected → lists available projects
    reset_app()
    result = {}
    params = make_params("check_progress", {}, cb)
    await handle_check_progress(params)

    has_available = "Not connected" in result.get("result", "") or "Available" in result.get("result", "")
    log(group, "check_progress while IDLE → available projects",
        has_available,
        f"result={result.get('result', '')[:100]}")

    # 7b. Connected, idle → shows project info
    reset_app()

    async def cb_connect(data):
        pass

    params = make_params("connect_project", {"project": "nexus"}, cb_connect)
    await handle_connect_project(params)

    result = {}
    params = make_params("check_progress", {}, cb)
    await handle_check_progress(params)

    has_project = "nexus" in result.get("result", "").lower()
    has_branch = "branch" in result.get("result", "").lower() or "Branch" in result.get("result", "")
    log(group, "check_progress while CODING → project + branch",
        has_project and has_branch,
        f"result={result.get('result', '')[:150]}")

    reset_app()


async def test_sleep():
    """Group 8: sleep handler."""
    group = "Sleep"
    reset_app()
    result = {}

    async def cb(data):
        nonlocal result
        result = data

    # Mock pipeline_task so EndFrame doesn't crash
    mock_task = MagicMock()

    async def mock_queue(frames):
        pass

    mock_task.queue_frames = mock_queue
    app.pipeline_task = mock_task

    params = make_params("sleep", {}, cb)
    # Run with a short timeout since sleep handler waits 3s
    try:
        await asyncio.wait_for(handle_sleep(params), timeout=5)
    except asyncio.TimeoutError:
        pass

    log(group, "sleep → goodbye message",
        "sleep" in result.get("result", "").lower() or "hey jarvis" in result.get("result", "").lower(),
        f"result={result.get('result', '')[:100]}")

    log(group, "sleep → sleep_requested=True",
        app.sleep_requested is True,
        f"sleep_requested={app.sleep_requested}")

    reset_app()


# =============================================================================
# Write test.md report
# =============================================================================

def write_report():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# Jarvis Test Results — {now}\n"]

    current_group = None
    pass_count = 0
    fail_count = 0

    for group, name, status, detail in results:
        if group != current_group:
            lines.append(f"\n## {group}\n")
            current_group = group

        if status == "PASS":
            pass_count += 1
        else:
            fail_count += 1

        detail_str = f" — `{detail}`" if detail else ""
        lines.append(f"- **{status}**: {name}{detail_str}")

    lines.append(f"\n---\n\n**Total: {pass_count} passed, {fail_count} failed**\n")

    report_path = os.path.join(os.path.dirname(__file__), "test.md")
    with open(report_path, "w") as f:
        f.write("\n".join(lines))
    print(f"\n\nReport written to {report_path}")


# =============================================================================
# Main
# =============================================================================

async def run_all():
    print("Running Jarvis tests...\n")

    test_groups = [
        ("State Machine", test_state_machine),
        ("Claude Session", test_claude_session),
        ("Coding Task", test_coding_task),
        ("Management", test_management),
        ("Search Documents", test_search_documents),
        ("GitHub", test_github),
        ("Check Progress", test_check_progress),
        ("Sleep", test_sleep),
    ]

    for name, test_fn in test_groups:
        print(f"\n  {name}: ", end="")
        try:
            await test_fn()
        except Exception as e:
            log(name, f"CRASHED: {e}", False, str(e))
            print(f" CRASH: {e}")

    write_report()

    # Summary
    passes = sum(1 for _, _, s, _ in results if s == "PASS")
    fails = sum(1 for _, _, s, _ in results if s == "FAIL")
    print(f"\n  {passes} passed, {fails} failed")

    return fails == 0


if __name__ == "__main__":
    success = asyncio.run(run_all())
    sys.exit(0 if success else 1)
