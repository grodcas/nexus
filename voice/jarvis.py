#!/usr/bin/env python3
"""
Nexus Voice — Claudia

Two-mode voice assistant:
  GEMINI MODE: casual companion, documents, general questions
  CLAUDE MODE: coding partner, locked to one project, session-aware

Usage:
    cd ~/nexus && source venv/bin/activate
    python voice/jarvis.py
"""

import asyncio
import json
import math
import os
import struct
import subprocess
import sys
import time
from enum import Enum

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import EndFrame, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.google.gemini_live.llm import (
    GeminiLiveLLMService,
    GeminiVADParams,
)
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.local.audio import (
    LocalAudioTransport,
    LocalAudioTransportParams,
)
from pipecat.turns.user_mute.always_user_mute_strategy import AlwaysUserMuteStrategy

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)

logger.remove(0)
logger.add(sys.stderr, level="INFO")


# =============================================================================
# Configuration
# =============================================================================

PROJECTS = {
    "nexus": "~/nexus",
    # Add more projects here or load from ~/.nexus/projects.json
}

_projects_file = os.path.expanduser("~/.nexus/projects.json")
if os.path.exists(_projects_file):
    with open(_projects_file) as f:
        PROJECTS.update(json.load(f))

SESSIONS_FILE = os.path.expanduser("~/.nexus/sessions.json")
WORKTREE_ROOT = os.path.expanduser("~/.nexus/documents")


# =============================================================================
# State machine
# =============================================================================

class AppState(Enum):
    GEMINI = "gemini"
    CONFIRMING = "confirming"
    CLAUDE = "claude"
    PAUSED = "paused"


# App class defined after ClaudeSession (forward reference)


# =============================================================================
# Session persistence (session_id per project)
# =============================================================================

def load_sessions() -> dict:
    if os.path.exists(SESSIONS_FILE):
        with open(SESSIONS_FILE) as f:
            return json.load(f)
    return {}


def save_session(project_name: str, session_id: str):
    sessions = load_sessions()
    sessions[project_name] = session_id
    os.makedirs(os.path.dirname(SESSIONS_FILE), exist_ok=True)
    with open(SESSIONS_FILE, "w") as f:
        json.dump(sessions, f, indent=2)


def get_last_session(project_name: str) -> str | None:
    return load_sessions().get(project_name)


# =============================================================================
# Project context
# =============================================================================

def load_project_context(project_path: str) -> dict:
    """Gather repo context for Claude mode entry."""
    ctx = {}
    try:
        r = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=project_path, capture_output=True, text=True, timeout=5,
        )
        ctx["branch"] = r.stdout.strip() if r.returncode == 0 else "unknown"

        r = subprocess.run(
            ["git", "status", "--short"],
            cwd=project_path, capture_output=True, text=True, timeout=5,
        )
        ctx["git_status"] = r.stdout.strip()[:500] or "clean"

        r = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            cwd=project_path, capture_output=True, text=True, timeout=5,
        )
        ctx["recent_commits"] = r.stdout.strip()[:500]
    except Exception as e:
        ctx["error"] = str(e)

    claude_md = os.path.join(project_path, "CLAUDE.md")
    if os.path.exists(claude_md):
        with open(claude_md) as f:
            ctx["claude_md"] = f.read()[:1500]

    return ctx


# =============================================================================
# Claude session manager
# =============================================================================

class ClaudeSession:
    """Manages Claude Code subprocess with streaming output and session continuity."""

    def __init__(self):
        self.proc = None
        self.status = "idle"     # idle | working | done | error
        self.instruction = ""
        self.events = []
        self.current_action = ""
        self.result_text = ""
        self.started_at = 0
        self.session_id = None
        self._monitor_task = None

    @property
    def is_busy(self):
        return self.status == "working"

    def start(self, instruction: str, repo_path: str, continue_session: bool = True):
        """Start a Claude Code task with streaming output."""
        if self.is_busy:
            return "Already working. Check progress or redirect first."

        self.instruction = instruction
        self.status = "working"
        self.events = []
        self.current_action = "starting..."
        self.result_text = ""
        self.started_at = time.time()

        cmd = ["claude", "--print", "--verbose", "--output-format", "stream-json",
               "--dangerously-skip-permissions"]

        # Session continuity: --continue resumes last conversation in this directory
        if continue_session:
            # Try --resume with stored session_id, fall back to --continue
            project_name = app.active_project
            stored_session = get_last_session(project_name) if project_name else None
            if stored_session:
                cmd.extend(["--resume", stored_session])
            else:
                cmd.append("--continue")

        cmd.extend(["-p", instruction])

        self.proc = subprocess.Popen(
            cmd,
            cwd=repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._monitor_task = asyncio.get_event_loop().create_task(self._monitor())
        logger.info(f"Claude started: {instruction}")
        return None

    async def _monitor(self):
        """Background task: reads Claude's stream-json output."""
        try:
            loop = asyncio.get_event_loop()
            while self.proc and self.proc.poll() is None:
                line = await loop.run_in_executor(None, self.proc.stdout.readline)
                if not line:
                    break
                try:
                    event = json.loads(line.decode("utf-8", errors="replace"))
                    self.events.append(event)
                    self._update_status(event)
                except json.JSONDecodeError:
                    pass

            # Process remaining output
            if self.proc:
                remaining = self.proc.stdout.read()
                if remaining:
                    for raw_line in remaining.split(b"\n"):
                        if raw_line.strip():
                            try:
                                event = json.loads(raw_line.decode("utf-8", errors="replace"))
                                self.events.append(event)
                                self._update_status(event)
                            except json.JSONDecodeError:
                                pass

            if self.status == "working":
                self.status = "done"
                self.current_action = "finished"

        except Exception as e:
            self.status = "error"
            self.current_action = f"error: {e}"
            logger.error(f"Claude monitor error: {e}")

    def _update_status(self, event):
        """Extract human-readable status from stream-json events."""
        etype = event.get("type", "")

        # Capture session_id from init event
        if etype == "system" and event.get("subtype") == "init":
            sid = event.get("session_id")
            if sid:
                self.session_id = sid
                if app.active_project:
                    save_session(app.active_project, sid)
                logger.info(f"Claude session: {sid}")

        elif etype == "assistant":
            msg = event.get("message", {})
            for block in msg.get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    self.result_text = text
                    if text.strip():
                        self.current_action = f"thinking: {text[:100]}"
                elif block.get("type") == "tool_use":
                    tool = block.get("name", "?")
                    inp = block.get("input", {})
                    if tool == "Read":
                        self.current_action = f"reading {inp.get('file_path', '?')}"
                    elif tool == "Edit":
                        self.current_action = f"editing {inp.get('file_path', '?')}"
                    elif tool == "Write":
                        self.current_action = f"writing {inp.get('file_path', '?')}"
                    elif tool == "Bash":
                        cmd = inp.get("command", "?")[:60]
                        self.current_action = f"running: {cmd}"
                    elif tool == "Grep":
                        self.current_action = f"searching for '{inp.get('pattern', '?')}'"
                    elif tool == "Glob":
                        self.current_action = f"finding files: {inp.get('pattern', '?')}"
                    else:
                        self.current_action = f"using {tool}"

        elif etype == "result":
            self.status = "done"
            self.result_text = event.get("result", "")
            self.current_action = "finished"
            duration = event.get("duration_ms", 0)
            cost = event.get("total_cost_usd", 0)
            logger.info(f"Claude done in {duration / 1000:.1f}s, cost ${cost:.4f}")

    def get_progress(self) -> str:
        if self.status == "idle":
            return "No task running. Ready for work."

        elapsed = int(time.time() - self.started_at)
        tool_uses = sum(
            1 for e in self.events
            if e.get("type") == "assistant"
            and any(b.get("type") == "tool_use" for b in e.get("message", {}).get("content", []))
        )

        if self.status == "working":
            return (
                f"Working on: {self.instruction}\n"
                f"Current: {self.current_action}\n"
                f"Elapsed: {elapsed}s, {tool_uses} operations so far"
            )
        elif self.status == "done":
            summary = self.result_text[:500] if self.result_text else "Completed (no text output)"
            return (
                f"Done: {self.instruction}\n"
                f"Took {elapsed}s, {tool_uses} operations\n"
                f"Result: {summary}"
            )
        else:
            return f"Error: {self.current_action}"

    def redirect(self, new_instruction: str) -> str:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.status = "idle"
        err = self.start(new_instruction, app.active_project_path)
        if err:
            return err
        return f"Redirected. Now working on: {new_instruction}"


# =============================================================================
# Application state
# =============================================================================

class App:
    """Central application state."""

    def __init__(self):
        self.state = AppState.GEMINI
        self.previous_state = AppState.GEMINI
        self.pending_action = None       # "enter:project_name" or "exit"
        self.active_project = None       # project name
        self.active_project_path = None  # expanded path
        self.project_context = {}        # git info, CLAUDE.md loaded on enter
        self.claude = ClaudeSession()
        self.pipeline_task = None        # for shutdown
        self.last_activity = time.time() # tracks user interaction
        self.shutdown_requested = False  # set by shut_down tool

    def status_summary(self) -> str:
        """One-line state description for tool responses."""
        if self.state == AppState.PAUSED:
            return "[PAUSED] Say 'wake up' to resume."
        if self.state == AppState.CONFIRMING:
            return f"[CONFIRMING] Pending: {self.pending_action}. Say 'confirm' or 'cancel'."
        if self.state == AppState.CLAUDE:
            task_status = ""
            if self.claude.is_busy:
                task_status = f" Task running: {self.claude.current_action}"
            elif self.claude.status == "done":
                task_status = " Last task completed."
            return f"[CLAUDE MODE] Project: {self.active_project}.{task_status}"
        return "[GEMINI MODE] General assistant. Say 'connect me to <project>' to code."


app = App()


# =============================================================================
# Document search
# =============================================================================

def load_worktree_summary():
    root_path = os.path.join(WORKTREE_ROOT, "root.md")
    if os.path.exists(root_path):
        with open(root_path) as f:
            return f.read()
    return "No worktree loaded."


def search_worktree(query: str) -> str:
    words = [w.lower() for w in query.split() if len(w) > 2]
    if not words:
        return f"No results for '{query}'."

    results = []
    for dirpath, _, filenames in os.walk(WORKTREE_ROOT):
        for fname in filenames:
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(dirpath, fname)
            with open(fpath) as f:
                content = f.read()
            for line in content.split("\n"):
                ll = line.lower()
                if all(w in ll for w in words):
                    results.append(f"[{fname}] {line.strip()}")
            if len(results) >= 15:
                break

    if not results:
        for dirpath, _, filenames in os.walk(WORKTREE_ROOT):
            for fname in filenames:
                if not fname.endswith(".md"):
                    continue
                fpath = os.path.join(dirpath, fname)
                with open(fpath) as f:
                    content = f.read()
                for line in content.split("\n"):
                    ll = line.lower()
                    if any(w in ll for w in words):
                        results.append(f"[{fname}] {line.strip()}")
                if len(results) >= 15:
                    break

    return "\n".join(results[:15]) if results else f"Nothing found for '{query}'."


# =============================================================================
# Activity tracking — wraps tool handlers to detect user presence
# =============================================================================

def track_activity(handler):
    """Decorator: update last_activity whenever a tool is called."""
    async def wrapper(params):
        app.last_activity = time.time()
        await handler(params)
    wrapper.__name__ = handler.__name__
    return wrapper


# =============================================================================
# Tool handlers — project management
# =============================================================================

async def handle_create_project(params: FunctionCallParams):
    name = params.arguments.get("name", "").lower().strip().replace(" ", "-")
    description = params.arguments.get("description", "")

    if not name:
        await params.result_callback({"result": "Project name is required."})
        return

    if name in PROJECTS:
        await params.result_callback({
            "result": f"Project '{name}' already exists at {PROJECTS[name]}. Use 'connect me to {name}' instead.",
            "state": app.status_summary(),
        })
        return

    # Create project directory and init git
    project_path = os.path.expanduser(f"~/{name}")
    try:
        os.makedirs(project_path, exist_ok=True)
        subprocess.run(["git", "init"], cwd=project_path, capture_output=True, timeout=5)

        # Write a minimal CLAUDE.md with the description
        if description:
            claude_md = os.path.join(project_path, "CLAUDE.md")
            with open(claude_md, "w") as f:
                f.write(f"# {name}\n\n{description}\n")
            subprocess.run(["git", "add", "CLAUDE.md"], cwd=project_path, capture_output=True, timeout=5)
            subprocess.run(["git", "commit", "-m", "Initial commit: add project description"],
                           cwd=project_path, capture_output=True, timeout=5)

        # Register in projects
        PROJECTS[name] = f"~/{name}"

        # Persist to projects.json
        os.makedirs(os.path.dirname(_projects_file), exist_ok=True)
        with open(_projects_file, "w") as f:
            json.dump(PROJECTS, f, indent=2)

        await params.result_callback({
            "result": (
                f"Created project '{name}' at {project_path}. "
                f"Git initialized. Say 'connect me to {name}' to start working."
            ),
            "state": app.status_summary(),
        })
    except Exception as e:
        await params.result_callback({"result": f"Error creating project: {e}"})


# =============================================================================
# Tool handlers — state transitions
# =============================================================================

async def handle_enter_project(params: FunctionCallParams):
    project = params.arguments.get("project", "").lower().strip()

    if app.state == AppState.CLAUDE:
        await params.result_callback({
            "result": f"Already in project '{app.active_project}'. Exit first, then connect to another.",
            "state": app.status_summary(),
        })
        return

    if app.state == AppState.PAUSED:
        await params.result_callback({"result": "[PAUSED] Say 'wake up' first."})
        return

    # Find project
    if project not in PROJECTS:
        available = ", ".join(PROJECTS.keys())
        await params.result_callback({
            "result": f"Unknown project '{project}'. Available: {available}",
            "state": app.status_summary(),
        })
        return

    path = os.path.expanduser(PROJECTS[project])
    if not os.path.isdir(path):
        await params.result_callback({
            "result": f"Project path '{path}' not found.",
            "state": app.status_summary(),
        })
        return

    app.pending_action = f"enter:{project}"
    app.state = AppState.CONFIRMING
    await params.result_callback({
        "result": f"Connect to project '{project}'? Say confirm to proceed.",
        "state": app.status_summary(),
    })


async def handle_confirm_action(params: FunctionCallParams):
    if app.state != AppState.CONFIRMING or not app.pending_action:
        await params.result_callback({
            "result": "Nothing to confirm.",
            "state": app.status_summary(),
        })
        return

    action = app.pending_action
    app.pending_action = None

    if action.startswith("enter:"):
        project_name = action.split(":", 1)[1]
        path = os.path.expanduser(PROJECTS[project_name])

        app.active_project = project_name
        app.active_project_path = path
        app.project_context = load_project_context(path)
        app.state = AppState.CLAUDE

        # Build context summary for Gemini
        ctx = app.project_context
        last_session = get_last_session(project_name)
        session_info = "Previous session available — can continue." if last_session else "Fresh start — no previous session."

        summary = (
            f"Connected to '{project_name}'.\n"
            f"Branch: {ctx.get('branch', '?')}\n"
            f"Git status: {ctx.get('git_status', '?')}\n"
            f"Recent commits:\n{ctx.get('recent_commits', 'none')}\n"
            f"Session: {session_info}\n"
        )
        if ctx.get("claude_md"):
            summary += f"\nProject notes:\n{ctx['claude_md'][:500]}\n"

        summary += "\nAsk the user: What are we working on? Continue last feature or start new?"

        await params.result_callback({
            "result": summary,
            "state": app.status_summary(),
        })

    elif action == "exit":
        # Kill any running task
        if app.claude.is_busy and app.claude.proc:
            app.claude.proc.terminate()
            try:
                app.claude.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                app.claude.proc.kill()
            app.claude.status = "idle"

        old_project = app.active_project
        app.active_project = None
        app.active_project_path = None
        app.project_context = {}
        app.claude = ClaudeSession()
        app.state = AppState.GEMINI

        await params.result_callback({
            "result": f"Disconnected from '{old_project}'. Back to Gemini mode.",
            "state": app.status_summary(),
        })


async def handle_exit_project(params: FunctionCallParams):
    if app.state != AppState.CLAUDE:
        await params.result_callback({
            "result": "Not in a project.",
            "state": app.status_summary(),
        })
        return

    warning = ""
    if app.claude.is_busy:
        warning = f" Warning: task still running ({app.claude.current_action})."

    app.pending_action = "exit"
    app.state = AppState.CONFIRMING
    await params.result_callback({
        "result": f"Exit project '{app.active_project}'?{warning} Say confirm.",
        "state": app.status_summary(),
    })


async def handle_pause(params: FunctionCallParams):
    if app.state == AppState.PAUSED:
        await params.result_callback({"result": "Already paused."})
        return

    app.previous_state = app.state
    app.state = AppState.PAUSED
    await params.result_callback({
        "result": "Paused. Say 'wake up' when you're back.",
        "state": app.status_summary(),
    })


async def handle_wake_up(params: FunctionCallParams):
    if app.state != AppState.PAUSED:
        await params.result_callback({
            "result": "Not paused. Already active.",
            "state": app.status_summary(),
        })
        return

    app.state = app.previous_state
    status = ""
    if app.state == AppState.CLAUDE and app.claude.is_busy:
        status = f" Task still running: {app.claude.current_action}"
    elif app.state == AppState.CLAUDE:
        status = f" In project '{app.active_project}'."

    await params.result_callback({
        "result": f"Resumed.{status}",
        "state": app.status_summary(),
    })


async def handle_shut_down(params: FunctionCallParams):
    # Kill Claude if running
    if app.claude.proc and app.claude.proc.poll() is None:
        app.claude.proc.terminate()

    app.shutdown_requested = True
    await params.result_callback({"result": "Shutting down. Goodbye."})

    # Give Gemini time to speak the goodbye, then stop pipeline
    await asyncio.sleep(3)
    if app.pipeline_task:
        await app.pipeline_task.queue_frames([EndFrame()])


# =============================================================================
# Tool handlers — status
# =============================================================================

async def handle_get_status(params: FunctionCallParams):
    parts = [app.status_summary()]

    if app.state == AppState.CLAUDE:
        parts.append(f"Project: {app.active_project} ({app.active_project_path})")
        parts.append(f"Branch: {app.project_context.get('branch', '?')}")
        if app.claude.status != "idle":
            parts.append(app.claude.get_progress())

    available = ", ".join(PROJECTS.keys())
    parts.append(f"Available projects: {available}")

    await params.result_callback({"result": "\n".join(parts)})


# =============================================================================
# Tool handlers — Claude mode (coding)
# =============================================================================

async def handle_do_task(params: FunctionCallParams):
    if app.state != AppState.CLAUDE:
        await params.result_callback({
            "result": "Not in Claude mode. Connect to a project first.",
            "state": app.status_summary(),
        })
        return

    instruction = params.arguments.get("instruction", "")
    new_feature = params.arguments.get("new_feature", False)

    err = app.claude.start(
        instruction,
        app.active_project_path,
        continue_session=not new_feature,
    )
    if err:
        await params.result_callback({"result": err, "state": app.status_summary()})
        return

    await asyncio.sleep(2)
    progress = app.claude.get_progress()
    await params.result_callback({"result": f"Started. {progress}", "state": app.status_summary()})


async def handle_check_progress(params: FunctionCallParams):
    if app.state != AppState.CLAUDE:
        await params.result_callback({
            "result": "Not in Claude mode.",
            "state": app.status_summary(),
        })
        return

    progress = app.claude.get_progress()
    await params.result_callback({"result": progress, "state": app.status_summary()})


async def handle_redirect_task(params: FunctionCallParams):
    if app.state != AppState.CLAUDE:
        await params.result_callback({
            "result": "Not in Claude mode.",
            "state": app.status_summary(),
        })
        return

    instruction = params.arguments.get("instruction", "")
    result = app.claude.redirect(instruction)
    await asyncio.sleep(2)
    progress = app.claude.get_progress()
    await params.result_callback({
        "result": f"{result}\n{progress}",
        "state": app.status_summary(),
    })


# =============================================================================
# Tool handlers — general
# =============================================================================

async def handle_search_documents(params: FunctionCallParams):
    query = params.arguments.get("query", "")
    logger.info(f"Searching: {query}")
    result = search_worktree(query)
    await params.result_callback({"result": result, "state": app.status_summary()})


async def handle_run_shell(params: FunctionCallParams):
    command = params.arguments.get("command", "")
    logger.info(f"Shell: {command}")

    # Run in project dir if in Claude mode, else home
    cwd = app.active_project_path if app.state == AppState.CLAUDE else os.path.expanduser("~")

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=10, cwd=cwd,
        )
        output = result.stdout.strip() or result.stderr.strip()
        output = output[:500] if output else "(no output)"
    except subprocess.TimeoutExpired:
        output = "Command timed out."
    except Exception as e:
        output = f"Error: {e}"

    await params.result_callback({"result": output, "state": app.status_summary()})


# =============================================================================
# Tool schema
# =============================================================================

TOOLS = [
    {
        "function_declarations": [
            # ── Project management ──
            {
                "name": "create_project",
                "description": (
                    "Create a new coding project. Initializes a git repo and registers it. "
                    "Use when user says 'create a new project', 'start a new project called X', 'new project X'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Project name (lowercase, no spaces, e.g. 'my-app')",
                        },
                        "description": {
                            "type": "string",
                            "description": "Brief description of what the project is about. Written to CLAUDE.md.",
                        },
                    },
                    "required": ["name"],
                },
            },
            # ── State transitions ──
            {
                "name": "enter_project",
                "description": (
                    "Initiate connection to a coding project. Call this tool immediately — "
                    "it will return a confirmation prompt for the user. "
                    "Use when user says 'connect me to X', 'let's work on X', 'open project X'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "project": {
                            "type": "string",
                            "description": "Project name (e.g. 'nexus')",
                        }
                    },
                    "required": ["project"],
                },
            },
            {
                "name": "confirm_action",
                "description": (
                    "Confirm a pending action. Call this tool when user says 'confirm', 'yes', "
                    "'go ahead', 'do it' in response to a confirmation prompt from enter_project or exit_project."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "exit_project",
                "description": (
                    "Initiate leaving the current project. Call this tool immediately — "
                    "it will return a confirmation prompt for the user. "
                    "Use when user says 'exit', 'disconnect', 'leave project'."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "pause_system",
                "description": (
                    "Pause the assistant (user is busy, talking to someone, etc). "
                    "Requires 'wake up' to resume. Use when user says 'wait', 'hold on', 'pause'."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "wake_up",
                "description": (
                    "Resume from pause. Use when user says 'wake up', 'I'm back', 'continue'."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "shut_down",
                "description": (
                    "Shut down the system completely. Use when user says 'sleep', 'shut down', 'goodbye'."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            # ── Status ──
            {
                "name": "get_status",
                "description": (
                    "Get current state: which mode, which project, what task is running. "
                    "Use when user asks 'where am I?', 'what's going on?', 'status'."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            # ── Claude mode: coding ──
            {
                "name": "do_task",
                "description": (
                    "Start a coding task in the active project. ONLY works in Claude mode. "
                    "Runs Claude Code in background with progress tracking. "
                    "Set new_feature=true for a fresh session, or false to continue previous context."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "instruction": {
                            "type": "string",
                            "description": "Detailed task description. Be specific.",
                        },
                        "new_feature": {
                            "type": "boolean",
                            "description": "True = fresh session. False = continue previous session context.",
                        },
                    },
                    "required": ["instruction"],
                },
            },
            {
                "name": "check_progress",
                "description": (
                    "Check status of the running coding task. ONLY in Claude mode. "
                    "Use when user asks 'how's it going?', 'what are you doing?', 'is it done?'."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "redirect_task",
                "description": (
                    "Stop current task and restart with corrected instruction. ONLY in Claude mode. "
                    "Use when user says 'actually', 'wait no', 'change that', 'do X instead'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "instruction": {
                            "type": "string",
                            "description": "The corrected task instruction.",
                        }
                    },
                    "required": ["instruction"],
                },
            },
            # ── General ──
            {
                "name": "search_documents",
                "description": (
                    "Search the user's 14K+ document archive by keywords. "
                    "Returns matching file entries with descriptions."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search keywords (e.g. 'PID control', 'thermal camera')",
                        }
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "run_shell",
                "description": (
                    "Run a shell command. Works in both modes. "
                    "For quick tasks: git status, open apps, check files."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Shell command to execute.",
                        }
                    },
                    "required": ["command"],
                },
            },
        ]
    }
]


# =============================================================================
# System prompt
# =============================================================================

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

Available projects: {projects}

{context}
"""


# =============================================================================
# Local wake detection — no API, just PyAudio + RMS threshold
# =============================================================================

IDLE_TIMEOUT = 420  # 7 minutes before disconnecting Gemini
SPEECH_RMS_THRESHOLD = 400  # RMS threshold for speech detection
SPEECH_CONFIRM_FRAMES = 3  # consecutive frames above threshold to confirm speech


def wait_for_speech():
    """Block until speech is detected locally. No API calls.
    Uses PyAudio to read mic input and checks RMS energy level."""
    import pyaudio

    CHUNK = 1024
    RATE = 16000
    p = pyaudio.PyAudio()
    stream = p.open(
        format=pyaudio.paInt16, channels=1, rate=RATE,
        input=True, frames_per_buffer=CHUNK,
    )

    consecutive = 0
    try:
        while True:
            data = stream.read(CHUNK, exception_on_overflow=False)
            samples = struct.unpack(f"{CHUNK}h", data)
            rms = math.sqrt(sum(s * s for s in samples) / CHUNK)
            if rms > SPEECH_RMS_THRESHOLD:
                consecutive += 1
                if consecutive >= SPEECH_CONFIRM_FRAMES:
                    return
            else:
                consecutive = 0
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()


# =============================================================================
# Main — session lifecycle
# =============================================================================

async def run_pipeline_session(is_first: bool = False):
    """Run one Gemini session. Returns when idle timeout or shutdown."""
    worktree_summary = load_worktree_summary()[:1500]
    projects_list = ", ".join(f"{k} ({v})" for k, v in PROJECTS.items())

    system = SYSTEM_PROMPT.format(
        projects=projects_list,
        context=f"Document archive: {worktree_summary}" if worktree_summary else "",
    )

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        )
    )

    llm = GeminiLiveLLMService(
        api_key=os.getenv("GEMINI_API_KEY"),
        system_instruction=system,
        tools=TOOLS,
        settings=GeminiLiveLLMService.Settings(
            model="gemini-2.5-flash-native-audio-preview-12-2025",
            voice="Aoede",
            vad=GeminiVADParams(disabled=True),
        ),
    )

    # Register all tools (wrapped with activity tracker)
    llm.register_function("create_project", track_activity(handle_create_project))
    llm.register_function("enter_project", track_activity(handle_enter_project))
    llm.register_function("confirm_action", track_activity(handle_confirm_action))
    llm.register_function("exit_project", track_activity(handle_exit_project))
    llm.register_function("pause_system", track_activity(handle_pause))
    llm.register_function("wake_up", track_activity(handle_wake_up))
    llm.register_function("shut_down", track_activity(handle_shut_down), cancel_on_interruption=False)
    llm.register_function("get_status", track_activity(handle_get_status))
    llm.register_function("do_task", track_activity(handle_do_task), cancel_on_interruption=False, timeout_secs=30)
    llm.register_function("check_progress", track_activity(handle_check_progress))
    llm.register_function("redirect_task", track_activity(handle_redirect_task), cancel_on_interruption=False, timeout_secs=30)
    llm.register_function("search_documents", track_activity(handle_search_documents))
    llm.register_function("run_shell", track_activity(handle_run_shell), cancel_on_interruption=False, timeout_secs=15)

    # Build initial context — re-inject state on reconnection
    if is_first:
        initial_msg = "Greet the user briefly. You're in Gemini mode."
    else:
        state = app.status_summary()
        if app.state == AppState.CLAUDE:
            ctx = app.project_context
            initial_msg = (
                f"User just came back after being idle. Current state: {state} "
                f"Branch: {ctx.get('branch', '?')}. "
                f"Acknowledge briefly — say you're back and ready."
            )
        elif app.state == AppState.PAUSED:
            initial_msg = (
                f"User spoke after being away. State was PAUSED. "
                f"Resume and acknowledge briefly."
            )
        else:
            initial_msg = (
                f"User came back after being idle. State: {state} "
                f"Acknowledge briefly — say you're listening."
            )

    context = LLMContext(
        [{"role": "user", "content": initial_msg}],
    )
    user_params = LLMUserAggregatorParams(
        user_mute_strategies=[AlwaysUserMuteStrategy()],
        vad_analyzer=SileroVADAnalyzer(),
    )
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context, user_params=user_params
    )

    pipeline = Pipeline(
        [
            transport.input(),
            user_aggregator,
            llm,
            transport.output(),
            assistant_aggregator,
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    app.pipeline_task = task
    app.last_activity = time.time()

    async def start_conversation():
        await asyncio.sleep(1)
        await task.queue_frames([LLMRunFrame()])

    async def keepalive():
        """Send silent audio every 90s to prevent Gemini websocket drop.
        The websocket dies after ~2-5 min of silence. We keep it alive
        up to the IDLE_TIMEOUT (7 min), then let idle_monitor disconnect."""
        from google.genai.types import Blob
        silent_frame = Blob(data=b"\x00" * 160, mime_type="audio/pcm;rate=16000")
        while True:
            await asyncio.sleep(90)
            try:
                if llm._session and not llm._disconnecting:
                    await llm._session.send_realtime_input(audio=silent_frame)
                    logger.debug("Keepalive ping")
            except Exception:
                pass

    async def idle_monitor():
        """Disconnect Gemini after 7 min of no user activity."""
        while True:
            await asyncio.sleep(10)
            idle_secs = time.time() - app.last_activity

            # Don't timeout if Claude is actively working
            if app.claude.is_busy:
                app.last_activity = time.time()
                continue

            # Don't timeout if paused (user explicitly paused)
            if app.state == AppState.PAUSED:
                app.last_activity = time.time()
                continue

            if idle_secs > IDLE_TIMEOUT:
                logger.info(f"Idle for {idle_secs:.0f}s — disconnecting Gemini")
                await task.queue_frames([EndFrame()])
                return

    runner = PipelineRunner(handle_sigint=True)
    await asyncio.gather(runner.run(task), start_conversation(), keepalive(), idle_monitor())


async def main():
    print("\n  Claudia starting. Speak into your mic. Ctrl+C to stop.\n")

    is_first = True
    while not app.shutdown_requested:
        # Phase 1: Active session — Gemini connected, full pipeline
        logger.info("Gemini session starting...")
        try:
            await run_pipeline_session(is_first=is_first)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Pipeline error: {e}")

        if app.shutdown_requested:
            break

        is_first = False

        # Phase 2: Idle — Gemini disconnected, local-only speech detection
        print("  💤 Idle — Gemini disconnected. Listening locally for speech...")
        logger.info("Waiting for local speech detection...")

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, wait_for_speech)
        except KeyboardInterrupt:
            break

        print("  🎤 Speech detected — reconnecting Gemini...")
        logger.info("Speech detected — reconnecting Gemini")

    print("\n  Claudia stopped.\n")


if __name__ == "__main__":
    asyncio.run(main())
