#!/usr/bin/env python3
"""
Layer 2 Tests — Gemini tool calling accuracy with state machine architecture.
Sends text scenarios to Gemini, checks it calls the right tools with correct args.
No audio, no pipecat. Direct google.genai SDK.

Usage: cd ~/nexus && source venv/bin/activate && python voice/test_layer2.py
"""

import asyncio
import json
import os
import sys
import time

from dotenv import load_dotenv
from google import genai
from google.genai.types import (
    Content,
    FunctionDeclaration,
    GenerateContentConfig,
    Part,
    Tool,
)

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)


# ── Tool schema (matches jarvis.py) ─────────────────────────────────────────

TOOLS = [
    Tool(
        function_declarations=[
            FunctionDeclaration(
                name="enter_project",
                description=(
                    "Initiate connection to a coding project. Call this tool immediately — "
                    "it will return a confirmation prompt for the user. "
                    "Use when user says 'connect me to X', 'let's work on X', 'open project X'."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Project name (e.g. 'nexus')"}
                    },
                    "required": ["project"],
                },
            ),
            FunctionDeclaration(
                name="confirm_action",
                description=(
                    "Confirm a pending action. Call this tool when user says 'confirm', 'yes', "
                    "'go ahead', 'do it' in response to a confirmation prompt from enter_project or exit_project."
                ),
                parameters={"type": "object", "properties": {}},
            ),
            FunctionDeclaration(
                name="exit_project",
                description=(
                    "Initiate leaving the current project. Call this tool immediately — "
                    "it will return a confirmation prompt for the user. "
                    "Use when user says 'exit', 'disconnect', 'leave project'."
                ),
                parameters={"type": "object", "properties": {}},
            ),
            FunctionDeclaration(
                name="pause_system",
                description=(
                    "Pause the assistant (user is busy, talking to someone). "
                    "Requires 'wake up' to resume. Use when user says 'wait', 'hold on', 'pause'."
                ),
                parameters={"type": "object", "properties": {}},
            ),
            FunctionDeclaration(
                name="wake_up",
                description="Resume from pause. Use when user says 'wake up', 'I'm back', 'continue'.",
                parameters={"type": "object", "properties": {}},
            ),
            FunctionDeclaration(
                name="shut_down",
                description="Shut down the system completely. Use when user says 'sleep', 'shut down', 'goodbye'.",
                parameters={"type": "object", "properties": {}},
            ),
            FunctionDeclaration(
                name="get_status",
                description=(
                    "Get current state: which mode, which project, what task is running. "
                    "Use when user asks 'where am I?', 'what's going on?', 'status'."
                ),
                parameters={"type": "object", "properties": {}},
            ),
            FunctionDeclaration(
                name="do_task",
                description=(
                    "Start a coding task in the active project. ONLY works in Claude mode. "
                    "Runs Claude Code in background with progress tracking. "
                    "Set new_feature=true for a fresh session, or false to continue previous context."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "instruction": {"type": "string", "description": "Detailed task description."},
                        "new_feature": {"type": "boolean", "description": "True=fresh session, False=continue."},
                    },
                    "required": ["instruction"],
                },
            ),
            FunctionDeclaration(
                name="check_progress",
                description=(
                    "Check status of the running coding task. ONLY in Claude mode. "
                    "Use when user asks 'how's it going?', 'what are you doing?', 'is it done?'."
                ),
                parameters={"type": "object", "properties": {}},
            ),
            FunctionDeclaration(
                name="redirect_task",
                description=(
                    "Stop current task and restart with corrected instruction. ONLY in Claude mode. "
                    "Use when user says 'actually', 'wait no', 'change that', 'do X instead'."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "instruction": {"type": "string", "description": "The corrected task instruction."}
                    },
                    "required": ["instruction"],
                },
            ),
            FunctionDeclaration(
                name="search_documents",
                description="Search the user's 14K+ document archive by keywords.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search keywords."}
                    },
                    "required": ["query"],
                },
            ),
            FunctionDeclaration(
                name="run_shell",
                description="Run a shell command. Works in both modes. For quick tasks: git status, open apps.",
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Shell command to execute."}
                    },
                    "required": ["command"],
                },
            ),
        ]
    )
]

SYSTEM_PROMPT = """\
Voice assistant with two modes. State is returned in every tool response.

GEMINI MODE (default): Friendly general assistant. Search documents, answer questions, chat.
CLAUDE MODE: Technical coding partner for one project. Concise. Report what Claude is doing.

State keywords — detect these and call the corresponding tool:
- "connect me to [X]" / "let's work on [X]" → enter_project
- "confirm" / "yes" (after confirmation prompt) → confirm_action
- "exit" / "disconnect" → exit_project
- "wait" / "hold on" → pause_system (mute until "wake up")
- "wake up" / "I'm back" → wake_up
- "sleep" / "shut down" → shut_down

Rules:
- When user wants to enter/exit a project, ALWAYS call the tool immediately. The tool handles the confirmation flow — do not ask for confirmation yourself.
- In PAUSED state: only respond to "wake up". Stay silent otherwise.
- In CONFIRMING state: only accept confirm or cancel. Anything else = cancel.
- Never start coding tasks outside Claude mode.
- In Claude mode: be technical, report progress, keep it brief.
- In Gemini mode: be conversational, flexible, handle anything.
- Always speak before calling tools. Never go silent.
- If ambiguous in Claude mode, ask: "Should I change that in the code, or are you thinking out loud?"

Available projects: nexus (~/nexus)
"""


# ── Test scenarios ───────────────────────────────────────────────────────────

SCENARIOS = [
    # ═══ State transitions ═══

    # Entering a project
    (
        "enter_project_direct",
        [{"role": "user", "content": "Connect me to the nexus project"}],
        {"tool": "enter_project", "args_contain": {"project": "nexus"}},
    ),
    (
        "enter_project_casual",
        [{"role": "user", "content": "Let's work on nexus"}],
        {"tool": "enter_project", "args_contain": {"project": "nexus"}},
    ),

    # Confirmation flow
    (
        "confirm_after_enter",
        [
            {"role": "user", "content": "Connect me to nexus"},
            {"role": "model", "content": "I'll connect you to the nexus project. Confirm to proceed."},
            {"role": "user", "content": "Confirm"},
        ],
        {"tool": "confirm_action"},
    ),
    (
        "confirm_yes_variant",
        [
            {"role": "user", "content": "Let's work on nexus"},
            {"role": "model", "content": "Connecting to nexus. Say confirm to proceed."},
            {"role": "user", "content": "Yes, go ahead"},
        ],
        {"tool": "confirm_action"},
    ),

    # Exit project
    (
        "exit_project",
        [
            {"role": "user", "content": "Connect me to nexus"},
            {"role": "model", "content": "Connected to nexus. Branch: master. What are we working on?"},
            {"role": "user", "content": "Exit"},
        ],
        {"tool": "exit_project"},
    ),
    (
        "exit_disconnect",
        [
            {"role": "user", "content": "Connect me to nexus"},
            {"role": "model", "content": "Connected to nexus. What should we work on?"},
            {"role": "user", "content": "Disconnect from the project"},
        ],
        {"tool": "exit_project"},
    ),

    # Pause / wake up
    (
        "pause_wait",
        [{"role": "user", "content": "Wait, hold on"}],
        {"tool": "pause_system"},
    ),
    (
        "wake_up_after_pause",
        [
            {"role": "user", "content": "Wait"},
            {"role": "model", "content": "Paused. Say wake up when you're back."},
            {"role": "user", "content": "Wake up"},
        ],
        {"tool": "wake_up"},
    ),
    (
        "wake_up_im_back",
        [
            {"role": "user", "content": "Hold on a second"},
            {"role": "model", "content": "Paused. Say wake up when you're back."},
            {"role": "user", "content": "I'm back"},
        ],
        {"tool": "wake_up"},
    ),

    # Shutdown
    (
        "sleep_shutdown",
        [{"role": "user", "content": "Sleep"}],
        {"tool": "shut_down"},
    ),
    (
        "goodbye_shutdown",
        [{"role": "user", "content": "Goodbye, shut down"}],
        {"tool": "shut_down"},
    ),

    # Status
    (
        "status_check",
        [{"role": "user", "content": "Where am I? What's the status?"}],
        {"tool": "get_status"},
    ),

    # ═══ Claude mode: coding ═══

    (
        "coding_task",
        [
            {"role": "user", "content": "Connect me to nexus"},
            {"role": "model", "content": "Connected to nexus. Branch: master. Git status: clean. What are we working on?"},
            {"role": "user", "content": "Add input validation to the API endpoints"},
        ],
        {"tool": "do_task", "args_contain": {"instruction": "validation"}},
    ),
    (
        "coding_new_feature",
        [
            {"role": "user", "content": "Connect me to nexus"},
            {"role": "model", "content": "Connected to nexus. Previous session available. Continue or new feature?"},
            {"role": "user", "content": "New feature. I want to add a websocket layer for real-time updates."},
        ],
        {"tool": "do_task", "args_contain": {"instruction": "websocket"}},
    ),
    (
        "coding_continue_feature",
        [
            {"role": "user", "content": "Connect me to nexus"},
            {"role": "model", "content": "Connected to nexus. Branch: master. You were working on the voice assistant. What should we do?"},
            {"role": "user", "content": "Continue the voice assistant work. Add error handling to the pipeline."},
        ],
        {"tool": "do_task", "args_contain": {"instruction": "error"}},
    ),

    # Progress
    (
        "check_progress",
        [
            {"role": "user", "content": "Connect me to nexus"},
            {"role": "model", "content": "Connected. Starting task: add validation. Claude is reading files."},
            {"role": "user", "content": "How's it going?"},
        ],
        {"tool": "check_progress"},
    ),

    # Redirect
    (
        "redirect_actually",
        [
            {"role": "user", "content": "Connect me to nexus"},
            {"role": "model", "content": "Connected. Working on: add validation to API."},
            {"role": "user", "content": "Actually, focus on the database layer instead"},
        ],
        {"tool": "redirect_task", "args_contain": {"instruction": "database"}},
    ),
    (
        "redirect_no_change",
        [
            {"role": "user", "content": "Connect me to nexus"},
            {"role": "model", "content": "Started writing tests for the parser module."},
            {"role": "user", "content": "No wait, don't write tests. Just fix the bug in the parser."},
        ],
        {"tool": "redirect_task"},
    ),

    # ═══ Gemini mode: general ═══

    (
        "document_search",
        [{"role": "user", "content": "Find my thesis about aerodynamics"}],
        {"tool": "search_documents", "args_contain": {"query": "aerodynamics"}},
    ),
    (
        "shell_command",
        [{"role": "user", "content": "What's the git status of the nexus repo?"}],
        {"tool": "run_shell", "args_contain": {"command": "git"}},
    ),

    # ═══ No tool expected ═══

    (
        "greeting",
        [{"role": "user", "content": "Hey good morning!"}],
        {"no_tool": True},
    ),
    (
        "casual_question",
        [{"role": "user", "content": "What time is it?"}],
        {"no_tool": True, "allow_tool": True},  # run_shell is also acceptable
    ),
    (
        "thanks_after_task",
        [
            {"role": "user", "content": "Fix the typo in the readme"},
            {"role": "model", "content": "Done. Fixed the typo on line 5."},
            {"role": "user", "content": "Thanks!"},
        ],
        {"no_tool": True},
    ),

    # ═══ Edge: won't code outside Claude mode ═══
    (
        "refuse_code_in_gemini",
        [{"role": "user", "content": "Write a Python script that sorts a list"}],
        # Should NOT call do_task (not in Claude mode). Should either suggest connecting or just answer.
        {"tool": ["enter_project"], "allow_no_tool": True},
    ),
]


# ── Test runner ──────────────────────────────────────────────────────────────

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

PASS = 0
FAIL = 0
ERRORS = []


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"✓ {name}")
    else:
        FAIL += 1
        ERRORS.append((name, detail))
        print(f"✗ {name} — {detail}")


async def call_gemini(contents: list, max_retries: int = 2) -> object:
    for attempt in range(max_retries + 1):
        try:
            return await client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=contents,
                config=GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    tools=TOOLS,
                    temperature=0.0,
                ),
            )
        except Exception as e:
            if "429" in str(e) and attempt < max_retries:
                wait = 30 + attempt * 15
                print(f"  rate limited, waiting {wait}s...")
                await asyncio.sleep(wait)
            else:
                raise


async def run_scenario(name: str, messages: list, expected: dict):
    contents = [
        Content(role=msg["role"], parts=[Part(text=msg["content"])])
        for msg in messages
    ]

    try:
        response = await call_gemini(contents)
    except Exception as e:
        check(name, False, f"API error: {e}")
        return

    # Extract tool calls and text
    tool_calls = []
    text_parts = []

    if response.candidates and response.candidates[0].content:
        for part in response.candidates[0].content.parts:
            if part.function_call:
                tool_calls.append({
                    "name": part.function_call.name,
                    "args": dict(part.function_call.args) if part.function_call.args else {},
                })
            if part.text:
                text_parts.append(part.text)

    text_response = " ".join(text_parts)

    # ── Evaluate ──

    if expected.get("no_tool"):
        if not tool_calls:
            check(name, True)
        elif expected.get("allow_tool"):
            check(name, True)
        else:
            check(name, False, f"expected no tool, got: {tool_calls[0]['name']}")
        return

    if expected.get("allow_no_tool") and not tool_calls:
        check(name, True)
        return

    if not tool_calls:
        check(name, False, f"expected tool, got text: {text_response[:120]}")
        return

    call = tool_calls[0]
    expected_tool = expected["tool"]

    if isinstance(expected_tool, list):
        tool_ok = call["name"] in expected_tool
        tool_desc = f"one of {expected_tool}"
    else:
        tool_ok = call["name"] == expected_tool
        tool_desc = expected_tool

    if not tool_ok:
        check(name, False, f"expected {tool_desc}, got: {call['name']}({json.dumps(call['args'], ensure_ascii=False)[:100]})")
        return

    args_contain = expected.get("args_contain", {})
    for key, substr in args_contain.items():
        val = str(call["args"].get(key, ""))
        if substr.lower() not in val.lower():
            check(name, False, f"args[{key}] should contain '{substr}', got: '{val[:100]}'")
            return

    check(name, True)


async def main():
    print("=" * 60)
    print("LAYER 2 — Gemini Tool Calling (12-tool state machine)")
    print(f"Model: gemini-2.5-flash | Scenarios: {len(SCENARIOS)}")
    print("=" * 60)

    # Run sequentially with small delay (paid tier is generous but be safe)
    for i, (name, msgs, exp) in enumerate(SCENARIOS):
        sys.stdout.write(f"[{i + 1}/{len(SCENARIOS)}] ")
        sys.stdout.flush()
        await run_scenario(name, msgs, exp)
        if i < len(SCENARIOS) - 1:
            await asyncio.sleep(1)

    # Summary
    print(f"\n{'=' * 60}")
    total = PASS + FAIL
    pct = 100 * PASS / total if total else 0
    print(f"Results: {PASS}/{total} passed ({pct:.0f}%)")

    if ERRORS:
        print(f"\nFailures:")
        for name, detail in ERRORS:
            print(f"  {name}: {detail}")

    print(f"{'=' * 60}")
    return FAIL == 0


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
