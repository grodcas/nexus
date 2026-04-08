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
import os
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
MANAGEMENT_ROOT = os.path.expanduser("~/.nexus/management")
MANAGEMENT_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "scripts", "management")


# =============================================================================
# State machine
# =============================================================================

class AppState(Enum):
    GEMINI = "gemini"
    CONFIRMING = "confirming"
    CLAUDE = "claude"


# App class defined after ClaudeSession (forward reference)


# =============================================================================
# Session persistence (session_id per project)
# =============================================================================

def load_sessions() -> dict:
    if os.path.exists(SESSIONS_FILE):
        with open(SESSIONS_FILE) as f:
            return json.load(f)
    return {}


def _save_sessions(sessions: dict):
    os.makedirs(os.path.dirname(SESSIONS_FILE), exist_ok=True)
    with open(SESSIONS_FILE, "w") as f:
        json.dump(sessions, f, indent=2)


def save_session(project_name: str, session_id: str):
    sessions = load_sessions()
    if not isinstance(sessions.get(project_name), dict):
        sessions[project_name] = {}
    sessions[project_name]["session_id"] = session_id
    _save_sessions(sessions)


def get_last_session(project_name: str) -> str | None:
    entry = load_sessions().get(project_name, {})
    if isinstance(entry, dict):
        return entry.get("session_id")
    return entry  # backward compat: old format was just a string


def save_last_result(project_name: str, result: str):
    """Save what Claude did last, so we can tell the user on re-entry."""
    sessions = load_sessions()
    if not isinstance(sessions.get(project_name), dict):
        sessions[project_name] = {}
    sessions[project_name]["last_result"] = result[:1000]
    sessions[project_name]["last_result_time"] = time.strftime("%Y-%m-%d %H:%M")
    _save_sessions(sessions)


def get_last_result(project_name: str) -> str | None:
    entry = load_sessions().get(project_name, {})
    if isinstance(entry, dict):
        result = entry.get("last_result")
        when = entry.get("last_result_time", "")
        if result:
            return f"[{when}] {result}"
    return None


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

        # Keep final summary short — the text response is relayed to the user
        if "concise" not in instruction.lower() and "short" not in instruction.lower() and "brief" not in instruction.lower():
            instruction += "\n\nKeep your final summary to 3-5 sentences."
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
        self.pending_action = None       # "enter:project_name" or "exit"
        self.active_project = None       # project name
        self.active_project_path = None  # expanded path
        self.project_context = {}        # git info, CLAUDE.md loaded on enter
        self.claude = ClaudeSession()
        self.pipeline_task = None        # for ending session
        self.last_activity = time.time() # tracks user interaction
        self.sleep_requested = False     # set by go_to_sleep tool → disconnect Gemini
        self.pending_claude_result = None  # stored when Claude finishes while Gemini is disconnected
        self.llm = None  # reference to GeminiLiveLLMService for direct session access

    @property
    def project_brief(self) -> str:
        """Domain context for Gemini while in Claude mode.
        Loaded from CLAUDE.md + last result. Helps Gemini understand
        what Claude is talking about — the translator's domain knowledge."""
        if self.state != AppState.CLAUDE or not self.active_project:
            return ""
        parts = []
        # Project description from CLAUDE.md
        claude_md = self.project_context.get("claude_md", "")
        if claude_md:
            parts.append(f"Project brief: {claude_md[:400]}")
        # Last work done
        last = get_last_result(self.active_project)
        if last:
            parts.append(f"Last work: {last[:300]}")
        return "\n".join(parts)

    def status_summary(self) -> str:
        """State description + project brief for tool responses."""
        if self.state == AppState.CONFIRMING:
            return f"[CONFIRMING] Pending: {self.pending_action}. Say 'confirm' or 'cancel'."
        if self.state == AppState.CLAUDE:
            task_status = ""
            if self.claude.is_busy:
                task_status = f" Task running: {self.claude.current_action}"
            elif self.claude.status == "done":
                task_status = " Last task completed."
            brief = self.project_brief
            context = f"\n{brief}" if brief else ""
            return f"[CLAUDE MODE] Project: {self.active_project}.{task_status}{context}"
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
        last_result = get_last_result(project_name)

        summary = (
            f"Connected to '{project_name}'.\n"
            f"Branch: {ctx.get('branch', '?')}\n"
            f"Git status: {ctx.get('git_status', '?')}\n"
            f"Recent commits:\n{ctx.get('recent_commits', 'none')}\n"
        )

        if last_result:
            summary += f"\nLast session work:\n{last_result}\n"
        elif last_session:
            summary += "\nPrevious session exists but no saved summary.\n"
        else:
            summary += "\nFresh start — no previous session.\n"

        if ctx.get("claude_md"):
            summary += f"\nProject notes:\n{ctx['claude_md'][:500]}\n"

        summary += (
            "\nTell the user what was done last time based on the info above. "
            "Then WAIT for their instruction. Do NOT start any task automatically. "
            "The user decides what to do next — you just report the current state and listen."
        )

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


async def handle_go_to_sleep(params: FunctionCallParams):
    """Disconnect Gemini and go back to wake word listening."""
    await params.result_callback({"result": "Going to sleep. Say 'hey jarvis' when you need me."})

    app.sleep_requested = True

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

    # Background watcher: notify Gemini when Claude finishes
    async def _watch_claude():
        while app.claude.is_busy:
            await asyncio.sleep(5)
        if app.claude.status == "done":
            result = app.claude.get_progress()
            app.pending_claude_result = result
            app.last_activity = time.time()
            # Persist result so next session knows what we did
            if app.active_project:
                save_last_result(app.active_project, result)
            # If Gemini is still connected, notify via send_client_content
            if app.llm and app.llm._session and not app.llm._disconnecting:
                try:
                    from google.genai.types import Content, Part
                    # Truncate result for voice — Gemini can't speak long texts
                    short_result = result[:400]
                    logger.info("Notifying Gemini of Claude completion via send_client_content...")
                    msg = Content(
                        role="user",
                        parts=[Part(text=f"[SYSTEM] Claude task completed. Tell the user concisely what was done:\n{short_result}")]
                    )
                    await app.llm._session.send_client_content(
                        turns=[msg], turn_complete=True
                    )
                    app.pending_claude_result = None
                    logger.info("Gemini notified successfully")
                except Exception as e:
                    logger.error(f"Failed to notify Gemini: {e}")
                    # Result stays pending for reconnect

    asyncio.create_task(_watch_claude())


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
# Tool handlers — GitHub
# =============================================================================

async def handle_check_github(params: FunctionCallParams):
    """Query GitHub repos via gh CLI."""
    query = params.arguments.get("query", "")
    logger.info(f"GitHub query: {query}")

    parts = []
    try:
        # Recent repo activity
        r = subprocess.run(
            "gh api /user/repos?sort=pushed&per_page=5 --jq '.[] | \"\\(.name) — last push \\(.pushed_at) — \\(.description // \"no desc\")\"'",
            shell=True, capture_output=True, text=True, timeout=15,
        )
        if r.stdout.strip():
            parts.append(f"Recent repos:\n{r.stdout.strip()}")

        # If asking about a specific repo, get last commits
        for project in PROJECTS:
            if project in query.lower():
                path = os.path.expanduser(PROJECTS[project])
                r = subprocess.run(
                    ["git", "log", "--oneline", "-5"],
                    cwd=path, capture_output=True, text=True, timeout=5,
                )
                if r.stdout.strip():
                    parts.append(f"Last commits in {project}:\n{r.stdout.strip()}")
                break

    except Exception as e:
        parts.append(f"Error: {e}")

    data = "\n\n".join(parts) if parts else "No data retrieved."

    await params.result_callback({
        "result": (
            f"User asked about GitHub: '{query}'\n"
            f"Answer based on this data. Be concise and conversational.\n\n"
            f"{data}"
        ),
        "state": app.status_summary(),
    })


# =============================================================================
# Tool handlers — management (briefing, calendar, reminders, email)
# =============================================================================

def _sync_management(sources: str = "all"):
    """Run sync scripts to refresh management worktree. Returns True on success."""
    venv_python = os.path.join(os.path.dirname(__file__), "..", "venv", "bin", "python3")
    venv_python = os.path.abspath(venv_python)
    cmd = [venv_python, "sync_all.py"]
    if sources != "all":
        cmd.append(f"--{sources}")
    try:
        result = subprocess.run(
            cmd, cwd=MANAGEMENT_SCRIPTS,
            capture_output=True, text=True, timeout=60,
        )
        logger.info(f"Management sync: {result.stdout.strip()}")
        return result.returncode == 0
    except Exception as e:
        logger.error(f"Management sync failed: {e}")
        return False


def _read_management_file(name: str) -> str:
    """Read a management worktree markdown file."""
    path = os.path.join(MANAGEMENT_ROOT, name)
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return f"File {name} not found. Run a sync first."


async def handle_briefing(params: FunctionCallParams):
    """Daily briefing: sync all sources, read root.md, return summary for Gemini to speak."""
    refresh = params.arguments.get("refresh", True)

    if refresh:
        _sync_management("all")

    root = _read_management_file("root.md")

    await params.result_callback({
        "result": (
            "Here is the user's current management data. Summarize it as a natural spoken briefing. "
            "Prioritize: today's schedule, urgent reminders, emails that need a reply (skip newsletters/promos). "
            "Be concise — this is spoken, not read.\n\n"
            f"{root}"
        ),
        "state": app.status_summary(),
    })


async def handle_check_calendar(params: FunctionCallParams):
    """Answer calendar questions: today, tomorrow, this week, specific date."""
    query = params.arguments.get("query", "")

    _sync_management("calendar")
    calendar = _read_management_file("calendar.md")

    await params.result_callback({
        "result": (
            f"User asked about their calendar: '{query}'\n"
            f"Answer based on this data. Be concise and conversational.\n\n"
            f"{calendar}"
        ),
        "state": app.status_summary(),
    })


async def handle_check_reminders(params: FunctionCallParams):
    """Answer reminder/task questions."""
    query = params.arguments.get("query", "")

    _sync_management("reminders")
    reminders = _read_management_file("reminders.md")

    await params.result_callback({
        "result": (
            f"User asked about their reminders/tasks: '{query}'\n"
            f"Answer based on this data. Be concise.\n\n"
            f"{reminders}"
        ),
        "state": app.status_summary(),
    })


async def handle_check_email(params: FunctionCallParams):
    """Answer email questions: unread, from someone, about a topic."""
    query = params.arguments.get("query", "")

    _sync_management("email")
    email = _read_management_file("email.md")

    await params.result_callback({
        "result": (
            f"User asked about their email: '{query}'\n"
            f"Answer based on this data. Skip obvious newsletters/promos unless asked. Be concise.\n\n"
            f"{email}"
        ),
        "state": app.status_summary(),
    })


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
                "name": "go_to_sleep",
                "description": (
                    "Go to sleep — disconnect and wait for wake word. "
                    "Use when user says 'sleep', 'wait', 'hold on', 'go to sleep', 'goodbye', 'shut down', 'pause'."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            # ── Status ──
            {
                "name": "get_status",
                "description": (
                    "Get SYSTEM state only: which mode (Gemini/Claude), which project is connected, "
                    "is a task running. NOT for project content — use do_task for anything about the code."
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
                    "Check status of a RUNNING coding task. ONLY when a task is actively in progress. "
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
            # ── GitHub ──
            {
                "name": "check_github",
                "description": (
                    "Check the user's GitHub activity. Use for any GitHub/git question: "
                    "'what did I last commit', 'what repos have I been working on', "
                    "'what's the status of my repos', 'last push', 'recent activity'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The user's GitHub question, e.g. 'last commits on nexus', 'any open PRs', 'recent activity'",
                        },
                    },
                    "required": ["query"],
                },
            },
            # ── Management (briefing, calendar, reminders, email) ──
            {
                "name": "briefing",
                "description": (
                    "Daily briefing: calendar, reminders, and email summary. "
                    "Use when user says 'morning briefing', 'what's on my plate', 'brief me', "
                    "'what do I have today', 'give me an overview', 'daily summary'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "refresh": {
                            "type": "boolean",
                            "description": "Whether to sync fresh data from sources (default true). Set false to use cached data.",
                        },
                    },
                },
            },
            {
                "name": "check_calendar",
                "description": (
                    "Check the user's calendar. Use for any schedule question: "
                    "'do I have meetings today', 'what's tomorrow', 'when is my next meeting', "
                    "'am I free on Thursday', 'what's my week look like'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The user's calendar question, e.g. 'meetings tomorrow', 'am I free Friday afternoon'",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "check_reminders",
                "description": (
                    "Check the user's reminders/tasks. Use for any task question: "
                    "'what do I need to do', 'any pending tasks', 'what's on my list', "
                    "'do I have any reminders', 'what's due today'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The user's reminder question, e.g. 'what's pending', 'anything due today'",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "check_email",
                "description": (
                    "Check the user's email. Use for any email question: "
                    "'any new emails', 'did Maria reply', 'what emails do I need to answer', "
                    "'anything important in my inbox', 'emails from Amazon'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The user's email question, e.g. 'unread emails', 'anything from Google'",
                        },
                    },
                    "required": ["query"],
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
You have two modes. State is returned in every tool response.

GEMINI MODE (default): General assistant. Search documents, answer questions, chat freely.
You also have access to the user's MANAGEMENT data: calendar, reminders, and email.

CLAUDE MODE: You ARE Claude. The user is talking directly to you as their coding partner. \
You never say "should I ask Claude?" or "should I forward this to Claude?" — that makes no sense, \
you are Claude. When the user says anything about the code, you act on it immediately via do_task. \
Every instruction, question, or comment about the project goes through do_task — that is your hands and eyes.

How do_task works — you write the instruction for it:
- User says "add a login screen" → do_task: "Add a login screen with email/password fields and validation"
- User says "what's the status?" → do_task: "Summarize the current state: what's built, what works, what's next. Be concise."
- User says "does the database work?" → do_task: "Check if the database layer is functional. Report issues. Short answer."
- User says "fix that bug" → do_task: "Fix the bug discussed in the previous session. Read the recent changes for context."
You ENHANCE the user's words into a clear, actionable instruction. Add specificity. Add context from the project brief.

Effort calibration — match the response to the ask:
- Quick question / "short" / "very short" → add "Be concise, 1-2 sentences." to the do_task instruction
- Opinion / feedback request → add "Give your assessment briefly. Don't change any code." to instruction
- Big task / new feature → write a detailed instruction with clear requirements
- "What do you think?" → add "Reason about this, propose an approach. Don't execute yet."

GitHub tool — use for any GitHub/git questions (works in BOTH modes):
- "what did I last commit?" / "any open PRs?" / "github status" → check_github

Management tools — use these for daily life questions (work in BOTH modes):
- "morning briefing" / "what's my day" / "overview" → briefing (syncs all sources, gives full summary)
- "do I have meetings?" / "when is..." / "am I free..." → check_calendar
- "what tasks do I have?" / "reminders" / "what's due" → check_reminders
- "any emails?" / "did X reply?" / "inbox" → check_email
These tools sync live data from Apple Calendar, Apple Reminders, and Gmail.
IMPORTANT: These tools take 5-15 seconds to sync. You MUST say something BEFORE calling them — \
e.g. "Let me check your calendar", "One moment, pulling your emails", "Let me get your briefing ready". \
NEVER go silent — always speak first, then call the tool.

State keywords — call the corresponding tool immediately:
- "connect me to [X]" / "let's work on [X]" → enter_project
- "confirm" / "yes" (after confirmation prompt) → confirm_action
- "exit" / "disconnect" → exit_project
- "wait" / "sleep" / "hold on" / "go to sleep" / "goodbye" → go_to_sleep (disconnects, listens for "hey jarvis")

Rules:
- In Claude mode: everything about the project goes through do_task. No exceptions. You don't know the code — do_task is how you see and act.
- NEVER start a do_task unless the user explicitly asked you to do something. Report status, then wait. The user drives — you execute.
- Management tools (briefing, check_calendar, check_reminders, check_email) work in BOTH modes — the user can ask about their schedule even while coding.
- When user wants to enter/exit a project, call the tool immediately. The tool handles confirmation.
- Never start coding tasks outside Claude mode.
- Speak before calling tools. Never go silent.

Available projects: {projects}

{context}
"""


# =============================================================================
# Local wake detection — openwakeword "hey jarvis" detector
# =============================================================================

IDLE_TIMEOUT = 420  # 7 minutes before disconnecting Gemini
WAKEWORD_THRESHOLD = 0.5  # openwakeword confidence threshold

# Lazy-loaded singleton — model loads once, reused across idle cycles
_oww_model = None


def _get_oww_model():
    global _oww_model
    if _oww_model is None:
        from openwakeword.model import Model
        _oww_model = Model(
            wakeword_models=["hey_jarvis"],
            inference_framework="onnx",
        )
        logger.info("OpenWakeWord model loaded (hey_jarvis)")
    return _oww_model


def wait_for_wakeword(timeout: float = 0):
    """Block until 'hey jarvis' wake word is detected.
    Uses openwakeword for on-device wake word detection.
    timeout=0 means wait forever. Returns True if detected, False if timed out."""
    import numpy as np
    import pyaudio

    CHUNK = 1280  # 80ms @ 16kHz — openwakeword's recommended chunk size
    RATE = 16000

    oww = _get_oww_model()
    oww.reset()

    p = pyaudio.PyAudio()
    stream = p.open(
        format=pyaudio.paInt16, channels=1, rate=RATE,
        input=True, frames_per_buffer=CHUNK,
    )

    start = time.time()
    try:
        while True:
            if timeout > 0 and (time.time() - start) > timeout:
                return False
            data = stream.read(CHUNK, exception_on_overflow=False)
            audio = np.frombuffer(data, dtype=np.int16)
            prediction = oww.predict(audio)

            for model_name, score in prediction.items():
                if score >= WAKEWORD_THRESHOLD:
                    logger.info(f"Wake word detected: '{model_name}' (score={score:.3f})")
                    oww.reset()
                    return True
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
    llm.register_function("go_to_sleep", track_activity(handle_go_to_sleep), cancel_on_interruption=False)
    llm.register_function("get_status", track_activity(handle_get_status))
    llm.register_function("do_task", track_activity(handle_do_task), cancel_on_interruption=False, timeout_secs=30)
    llm.register_function("check_progress", track_activity(handle_check_progress))
    llm.register_function("redirect_task", track_activity(handle_redirect_task), cancel_on_interruption=False, timeout_secs=30)
    llm.register_function("search_documents", track_activity(handle_search_documents))
    llm.register_function("check_github", track_activity(handle_check_github), cancel_on_interruption=False, timeout_secs=30)
    llm.register_function("run_shell", track_activity(handle_run_shell), cancel_on_interruption=False, timeout_secs=15)
    llm.register_function("briefing", track_activity(handle_briefing), cancel_on_interruption=False, timeout_secs=60)
    llm.register_function("check_calendar", track_activity(handle_check_calendar), cancel_on_interruption=False, timeout_secs=30)
    llm.register_function("check_reminders", track_activity(handle_check_reminders), cancel_on_interruption=False, timeout_secs=30)
    llm.register_function("check_email", track_activity(handle_check_email), cancel_on_interruption=False, timeout_secs=30)

    # Build initial context — re-inject state on reconnection
    if is_first:
        initial_msg = "Greet the user briefly. You're in Gemini mode."
    elif app.pending_claude_result:
        # Claude finished while Gemini was disconnected — deliver the result
        result = app.pending_claude_result
        app.pending_claude_result = None
        initial_msg = (
            f"[SYSTEM] Claude task just completed while you were disconnected. "
            f"Report this to the user concisely:\n{result}"
        )
    else:
        state = app.status_summary()
        if app.state == AppState.CLAUDE:
            ctx = app.project_context
            initial_msg = (
                f"User just came back after being idle. Current state: {state} "
                f"Branch: {ctx.get('branch', '?')}. "
                f"Acknowledge briefly — say you're back and ready."
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
            idle_timeout_secs=None,  # disable pipecat's idle timeout — we manage our own
        ),
    )

    app.pipeline_task = task
    app.llm = llm
    app.last_activity = time.time()

    session_stop = asyncio.Event()

    async def start_conversation():
        await asyncio.sleep(1)
        await task.queue_frames([LLMRunFrame()])

    async def keepalive():
        """Send silent audio every 45s to prevent Gemini websocket drop."""
        from google.genai.types import Blob
        silent_frame = Blob(data=b"\x00" * 320, mime_type="audio/pcm;rate=16000")
        while not session_stop.is_set():
            try:
                await asyncio.wait_for(session_stop.wait(), timeout=45)
                break  # stop event was set
            except asyncio.TimeoutError:
                pass  # 45s elapsed, send keepalive
            try:
                if llm._session and not llm._disconnecting:
                    await llm._session.send_realtime_input(audio=silent_frame)
                    logger.debug("Keepalive ping")
            except Exception:
                pass

    async def idle_monitor():
        """Disconnect Gemini after 7 min of no user activity."""
        while not session_stop.is_set():
            try:
                await asyncio.wait_for(session_stop.wait(), timeout=10)
                break
            except asyncio.TimeoutError:
                pass

            idle_secs = time.time() - app.last_activity

            if app.claude.is_busy:
                app.last_activity = time.time()
                continue

            if app.claude.status == "done" and (time.time() - app.claude.started_at) < IDLE_TIMEOUT:
                app.last_activity = time.time()
                continue

            if idle_secs > IDLE_TIMEOUT:
                logger.info(f"Idle for {idle_secs:.0f}s — disconnecting Gemini")
                await task.queue_frames([EndFrame()])
                return

    async def run_pipeline():
        """Run the pipeline and signal stop when it ends."""
        runner = PipelineRunner(handle_sigint=False)
        await runner.run(task)
        session_stop.set()
        logger.info("Pipeline runner finished — signaling stop")

    try:
        await asyncio.gather(run_pipeline(), start_conversation(), keepalive(), idle_monitor())
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning(f"Pipeline session ended: {e}")
    finally:
        session_stop.set()
        app.pipeline_task = None
        app.llm = None
        logger.info("Pipeline session cleanup done — returning to main loop")


async def main():
    print("\n  Jarvis starting. Say 'hey jarvis' to wake. Ctrl+C to stop.\n")

    is_first = True
    while True:
        # Phase 1: Active session — Gemini connected, full pipeline
        logger.info("Gemini session starting...")
        app.sleep_requested = False
        await run_pipeline_session(is_first=is_first)
        logger.info("Pipeline session ended, entering idle phase")

        is_first = False

        # Phase 2: Idle — Gemini disconnected
        # If Claude has a pending result, reconnect immediately to deliver it
        if app.pending_claude_result:
            logger.info("Claude result pending — reconnecting Gemini immediately")
            continue

        # Otherwise wait for wake word, checking for Claude results every 5s
        print("  Idle — listening for 'hey jarvis'...")
        logger.info("Entering wake word detection loop")

        loop = asyncio.get_event_loop()
        while True:
            detected = await loop.run_in_executor(None, wait_for_wakeword, 5.0)
            if detected:
                logger.info("Wake word detected — reconnecting Gemini")
                break
            if app.pending_claude_result:
                logger.info("Claude result pending — reconnecting Gemini to deliver")
                break


def _force_exit(signum, frame):
    """Hard exit on Ctrl+C — kill Claude subprocess if running, then exit."""
    if app.claude.proc and app.claude.proc.poll() is None:
        app.claude.proc.kill()
    print("\n  Jarvis stopped.\n")
    os._exit(0)


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGINT, _force_exit)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _force_exit(None, None)
