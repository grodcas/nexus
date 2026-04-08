#!/usr/bin/env python3
"""
Jarvis — Personal Voice Assistant

Voice-controlled assistant using Gemini Live for conversation,
with background tools for coding, management, documents, and GitHub.

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

PROJECTS = {"nexus": "~/nexus"}

_projects_file = os.path.expanduser("~/.nexus/projects.json")
if os.path.exists(_projects_file):
    with open(_projects_file) as f:
        PROJECTS.update(json.load(f))

SESSIONS_FILE = os.path.expanduser("~/.nexus/sessions.json")
WORKTREE_ROOT = os.path.expanduser("~/.nexus/documents")
MANAGEMENT_ROOT = os.path.expanduser("~/.nexus/management")
MANAGEMENT_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "scripts", "management")

IDLE_TIMEOUT = 420  # 7 minutes before disconnecting Gemini


# =============================================================================
# State machine — 2 states, no confirmation
# =============================================================================

class AppState(Enum):
    IDLE = "idle"       # General assistant
    CODING = "coding"   # Connected to a project


class App:
    def __init__(self):
        self.state = AppState.IDLE
        self.active_project = None
        self.active_project_path = None
        self.project_context = {}
        self.claude = ClaudeSession()
        self.pipeline_task = None
        self.last_activity = time.time()
        self.sleep_requested = False
        self.pending_claude_result = None
        self.llm = None


# Forward reference — App created after ClaudeSession is defined


# =============================================================================
# Session persistence
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
    return entry


def save_last_result(project_name: str, result: str):
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


def load_project_context(project_path: str) -> dict:
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
# Claude session manager — with kill(), auto-replace, on_complete callback
# =============================================================================

class ClaudeSession:
    def __init__(self):
        self.proc = None
        self.status = "idle"  # idle | working | done | error
        self.instruction = ""
        self.current_action = ""
        self.result_text = ""
        self.started_at = 0
        self.session_id = None
        self._monitor_task = None
        self._on_complete = None
        self._events = []

    @property
    def is_busy(self):
        return self.status == "working"

    def kill(self):
        """Kill subprocess and cancel monitor. Single cleanup entry point."""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._monitor_task = None
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=1)
        self.proc = None
        self.status = "idle"
        self.current_action = ""

    async def start(self, instruction: str, repo_path: str,
                    continue_session: bool = True, on_complete=None):
        """Start a Claude Code task. Kills any existing task first."""
        self.kill()

        if "concise" not in instruction.lower() and "short" not in instruction.lower():
            instruction += "\n\nKeep your final summary to 3-5 sentences."

        self.instruction = instruction
        self.status = "working"
        self._events = []
        self.current_action = "starting..."
        self.result_text = ""
        self.started_at = time.time()
        self._on_complete = on_complete

        cmd = ["claude", "--print", "--verbose", "--output-format", "stream-json",
               "--dangerously-skip-permissions"]

        if continue_session:
            stored = get_last_session(app.active_project) if app.active_project else None
            if stored:
                cmd.extend(["--resume", stored])
            else:
                cmd.append("--continue")

        cmd.extend(["-p", instruction])

        self.proc = subprocess.Popen(
            cmd, cwd=repo_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self._monitor_task = asyncio.create_task(self._monitor())
        self._monitor_task.set_name(f"claude-monitor-{int(time.time())}")
        logger.info(f"Claude started: {instruction[:100]}")

    async def _monitor(self):
        """Read Claude's stream-json output. Calls on_complete when done."""
        try:
            loop = asyncio.get_event_loop()
            while self.proc and self.proc.poll() is None:
                line = await loop.run_in_executor(None, self.proc.stdout.readline)
                if not line:
                    break
                try:
                    event = json.loads(line.decode("utf-8", errors="replace"))
                    self._events.append(event)
                    self._process_event(event)
                except json.JSONDecodeError:
                    pass

            # Drain remaining output
            if self.proc:
                remaining = await loop.run_in_executor(None, self.proc.stdout.read)
                if remaining:
                    for raw_line in remaining.split(b"\n"):
                        if raw_line.strip():
                            try:
                                event = json.loads(raw_line.decode("utf-8", errors="replace"))
                                self._events.append(event)
                                self._process_event(event)
                            except json.JSONDecodeError:
                                pass

            if self.status == "working":
                self.status = "done"
                self.current_action = "finished"

        except asyncio.CancelledError:
            logger.info("Claude monitor cancelled")
            return
        except Exception as e:
            self.status = "error"
            self.current_action = f"error: {e}"
            logger.error(f"Claude monitor error: {e}")
        finally:
            logger.info(f"Monitor finally: status={self.status}, has_callback={self._on_complete is not None}")
            if self._on_complete and self.status in ("done", "error"):
                try:
                    await self._on_complete()
                except Exception as e:
                    logger.error(f"on_complete callback error: {e}")
            elif self._on_complete:
                logger.warning(f"on_complete skipped — status was '{self.status}'")

    def _process_event(self, event):
        etype = event.get("type", "")

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
                        self.current_action = f"thinking: {text[:80]}"
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
                        self.current_action = f"running: {inp.get('command', '?')[:50]}"
                    elif tool == "Grep":
                        self.current_action = f"searching: {inp.get('pattern', '?')}"
                    elif tool == "Glob":
                        self.current_action = f"finding: {inp.get('pattern', '?')}"
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
        # Health check: detect zombie process
        if self.status == "working" and self.proc and self.proc.poll() is not None:
            self.status = "error"
            self.current_action = f"process died (exit {self.proc.returncode})"

        if self.status == "idle":
            return "No task running."

        elapsed = int(time.time() - self.started_at)
        tool_count = sum(
            1 for e in self._events
            if e.get("type") == "assistant"
            and any(b.get("type") == "tool_use" for b in e.get("message", {}).get("content", []))
        )

        if self.status == "working":
            return (
                f"Working on: {self.instruction[:100]}\n"
                f"Current: {self.current_action}\n"
                f"Elapsed: {elapsed}s, {tool_count} operations"
            )
        elif self.status == "done":
            summary = self.result_text[:500] if self.result_text else "Completed."
            return f"Done ({elapsed}s, {tool_count} ops). Result: {summary}"
        else:
            return f"Error: {self.current_action}"


app = App()


# =============================================================================
# Document search — single-pass, two-tier matching
# =============================================================================

def search_worktree(query: str) -> str:
    words = [w.lower() for w in query.split() if len(w) > 2]
    if not words:
        return f"No results for '{query}'."

    exact, partial = [], []
    for dirpath, _, filenames in os.walk(WORKTREE_ROOT):
        for fname in filenames:
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(dirpath, fname)
            with open(fpath) as f:
                for line in f:
                    ll = line.lower()
                    if all(w in ll for w in words):
                        exact.append(f"[{fname}] {line.strip()}")
                    elif len(partial) < 15 and any(w in ll for w in words):
                        partial.append(f"[{fname}] {line.strip()}")
            if len(exact) >= 15:
                break

    results = exact[:15] or partial[:15]
    return "\n".join(results) if results else f"Nothing found for '{query}'."


# =============================================================================
# Management sync helper
# =============================================================================

def _sync_management(source: str = "all") -> bool:
    """Run sync scripts. Called via asyncio.to_thread — never blocks event loop."""
    venv_python = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "venv", "bin", "python3")
    )
    cmd = [venv_python, "sync_all.py"]
    if source != "all":
        cmd.append(f"--{source}")
    try:
        r = subprocess.run(cmd, cwd=MANAGEMENT_SCRIPTS, capture_output=True, text=True, timeout=60)
        logger.info(f"Sync ({source}): {r.stdout.strip()[-100:]}")
        return r.returncode == 0
    except Exception as e:
        logger.error(f"Sync failed: {e}")
        return False


def _read_management_file(name: str) -> str:
    path = os.path.join(MANAGEMENT_ROOT, name)
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return f"File {name} not found. Run a sync first."


# =============================================================================
# Activity tracking
# =============================================================================

def track_activity(handler):
    async def wrapper(params):
        app.last_activity = time.time()
        await handler(params)
    wrapper.__name__ = handler.__name__
    return wrapper


# =============================================================================
# Tool handlers — 8 tools, all async-safe
# =============================================================================

# ── 1. connect_project ──

async def handle_connect_project(params: FunctionCallParams):
    project = params.arguments.get("project", "").lower().strip()

    if app.state == AppState.CODING:
        await params.result_callback({
            "result": f"Already connected to '{app.active_project}'. Disconnect first."
        })
        return

    if project not in PROJECTS:
        available = ", ".join(PROJECTS.keys())
        await params.result_callback({
            "result": f"Unknown project '{project}'. Available: {available}"
        })
        return

    path = os.path.expanduser(PROJECTS[project])
    if not os.path.isdir(path):
        await params.result_callback({"result": f"Path '{path}' not found."})
        return

    ctx = await asyncio.to_thread(load_project_context, path)

    app.active_project = project
    app.active_project_path = path
    app.project_context = ctx
    app.state = AppState.CODING

    last_result = get_last_result(project)

    summary = f"Connected to '{project}'. Branch: {ctx.get('branch', '?')}."
    if ctx.get("recent_commits"):
        summary += f"\nRecent commits:\n{ctx['recent_commits']}"
    if last_result:
        summary += f"\nLast session: {last_result}"
    if ctx.get("claude_md"):
        summary += f"\nProject: {ctx['claude_md'][:300]}"

    await params.result_callback({"result": summary})


# ── 2. disconnect_project ──

async def handle_disconnect_project(params: FunctionCallParams):
    if app.state != AppState.CODING:
        await params.result_callback({"result": "Not connected to a project."})
        return

    old = app.active_project
    app.claude.kill()
    app.active_project = None
    app.active_project_path = None
    app.project_context = {}
    app.claude = ClaudeSession()
    app.state = AppState.IDLE

    await params.result_callback({"result": f"Disconnected from '{old}'."})


# ── 3. coding_task ──

async def handle_coding_task(params: FunctionCallParams):
    if app.state != AppState.CODING:
        await params.result_callback({
            "result": "Not connected to a project. Say 'connect to <project>' first."
        })
        return

    instruction = params.arguments.get("instruction", "")
    fresh = params.arguments.get("fresh_session", False)

    async def on_complete():
        result = app.claude.get_progress()
        if app.active_project:
            await asyncio.to_thread(save_last_result, app.active_project, result)
        app.pending_claude_result = result
        logger.info("Claude done — result saved, Gemini will reconnect to deliver")

    await app.claude.start(
        instruction, app.active_project_path,
        continue_session=not fresh, on_complete=on_complete,
    )

    await asyncio.sleep(1.5)
    progress = app.claude.get_progress()
    await params.result_callback({
        "result": f"Task started. I'll disconnect now and come back when it's done. {progress}"
    })

    # Disconnect Gemini — Claude runs in background.
    # Main loop will reconnect when pending_claude_result appears.
    await asyncio.sleep(3)  # Let Gemini speak the response first
    if app.pipeline_task:
        await app.pipeline_task.queue_frames([EndFrame()])


# ── 4. check_progress ──

async def handle_check_progress(params: FunctionCallParams):
    if app.state != AppState.CODING:
        available = ", ".join(PROJECTS.keys())
        await params.result_callback({
            "result": f"Not connected to a project. Available: {available}"
        })
        return

    parts = [f"Project: {app.active_project} ({app.active_project_path})"]
    parts.append(f"Branch: {app.project_context.get('branch', '?')}")
    parts.append(app.claude.get_progress())

    await params.result_callback({"result": "\n".join(parts)})


# ── 5. management ──

async def handle_management(params: FunctionCallParams):
    source = params.arguments.get("source", "all")
    query = params.arguments.get("query", "")

    await asyncio.to_thread(_sync_management, source)

    if source == "all":
        data = await asyncio.to_thread(_read_management_file, "root.md")
        prefix = (
            "Summarize as a spoken briefing. Prioritize: today's schedule, "
            "urgent reminders, emails needing reply. Skip newsletters.\n\n"
        )
    else:
        filename = {"calendar": "calendar.md", "reminders": "reminders.md", "email": "email.md"}
        data = await asyncio.to_thread(_read_management_file, filename.get(source, "root.md"))
        prefix = f"User asked: '{query}'\nAnswer concisely based on this data.\n\n"

    await params.result_callback({"result": prefix + data})


# ── 6. search_documents ──

async def handle_search_documents(params: FunctionCallParams):
    query = params.arguments.get("query", "")
    logger.info(f"Doc search: {query}")
    result = await asyncio.to_thread(search_worktree, query)
    await params.result_callback({"result": result})


# ── 7. github ──

async def handle_github(params: FunctionCallParams):
    query = params.arguments.get("query", "")
    logger.info(f"GitHub: {query}")

    def _fetch():
        parts = []
        try:
            r = subprocess.run(
                ["gh", "api", "/user/repos?sort=pushed&per_page=5",
                 "--jq", r'.[] | "\(.name) — \(.pushed_at) — \(.description // "no desc")"'],
                capture_output=True, text=True, timeout=15,
            )
            if r.stdout.strip():
                parts.append(f"Recent repos:\n{r.stdout.strip()}")
        except Exception as e:
            parts.append(f"Error: {e}")

        # Get commits for specific project if mentioned
        for proj_name, proj_path in PROJECTS.items():
            if proj_name in query.lower():
                path = os.path.expanduser(proj_path)
                try:
                    r = subprocess.run(
                        ["git", "log", "--oneline", "-5"],
                        cwd=path, capture_output=True, text=True, timeout=5,
                    )
                    if r.stdout.strip():
                        parts.append(f"Last commits in {proj_name}:\n{r.stdout.strip()}")
                except Exception:
                    pass
                break

        return "\n\n".join(parts) if parts else "No data."

    data = await asyncio.to_thread(_fetch)
    await params.result_callback({
        "result": f"User asked: '{query}'\nAnswer concisely.\n\n{data}"
    })


# ── 8. sleep ──

async def handle_sleep(params: FunctionCallParams):
    await params.result_callback({"result": "Going to sleep. Say 'hey jarvis' when you need me."})
    app.sleep_requested = True
    await asyncio.sleep(3)
    if app.pipeline_task:
        await app.pipeline_task.queue_frames([EndFrame()])


# =============================================================================
# Tool schema — 8 tools
# =============================================================================

TOOLS = [
    {
        "function_declarations": [
            {
                "name": "connect_project",
                "description": (
                    "Connect to a coding project. Use when user says "
                    "'connect to X', 'let's work on X', 'open X'. Connects immediately."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Project name"},
                    },
                    "required": ["project"],
                },
            },
            {
                "name": "disconnect_project",
                "description": (
                    "Disconnect from the current coding project. "
                    "Use when user says 'exit', 'disconnect', 'leave project', 'close project'."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "coding_task",
                "description": (
                    "Send a task to the background coding agent. Only works when connected to a project. "
                    "If a task is already running, it will be stopped and replaced. "
                    "Write a clear, detailed instruction. Set fresh_session=true only for unrelated new work."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "instruction": {
                            "type": "string",
                            "description": "Detailed task instruction for the coding agent.",
                        },
                        "fresh_session": {
                            "type": "boolean",
                            "description": "True = new session. False = continue previous context (default).",
                        },
                    },
                    "required": ["instruction"],
                },
            },
            {
                "name": "check_progress",
                "description": (
                    "Check the coding agent's status and project info. "
                    "Use when user asks 'how's it going', 'is it done', 'what's the status'."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "management",
                "description": (
                    "Access calendar, reminders, or email. Syncs live data (takes 5-15s). "
                    "Use source='all' for a full daily briefing. "
                    "Use for 'morning briefing', 'any meetings', 'what's due', 'any emails'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "enum": ["all", "calendar", "reminders", "email"],
                            "description": "Which source to check.",
                        },
                        "query": {
                            "type": "string",
                            "description": "What the user asked.",
                        },
                    },
                    "required": ["source"],
                },
            },
            {
                "name": "search_documents",
                "description": (
                    "Search the user's 14K+ document archive by keywords. "
                    "Use for 'find my document about X', 'do I have notes on X'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search keywords."},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "github",
                "description": (
                    "Check GitHub activity: recent repos, commits. "
                    "Use for 'what did I last commit', 'recent activity', 'repo status'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The GitHub question."},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "sleep",
                "description": (
                    "Go to sleep — disconnect and wait for 'hey jarvis' wake word. "
                    "Use when user says 'sleep', 'goodbye', 'go to sleep', 'that's all', "
                    "'hold on', 'wait', 'pause'."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        ]
    }
]


# =============================================================================
# System prompt — identity-first, concise
# =============================================================================

SYSTEM_PROMPT = """\
You are Jarvis, a personal assistant. Sharp, friendly, concise. You speak naturally.

Tools: coding (connect to project + delegate), management (calendar/reminders/email), \
documents (search 14K files), github (repo info).

Rules:
- ALWAYS say something before calling a tool. "Let me check..." Never go silent.
- When a tool returns data, READ IT BACK to the user almost verbatim. Do not rephrase, \
  interpret, or summarize unless the user asks. You are a voice layer — pass the information \
  through with natural speech flow. Add a brief intro ("Here's what I found", "Done") and \
  read the content.
- For coding tasks: pass the user's request to the coding agent. When results come back, \
  read them directly. Do not add your own interpretation of code.
- When translating a user request into a coding_task instruction, ENHANCE it: add specificity \
  and context. But the result comes back raw — just read it.
- You can answer other questions while a coding task runs.

Available projects: {projects}
"""


# =============================================================================
# Wake word detection — openwakeword "hey jarvis"
# =============================================================================

WAKEWORD_THRESHOLD = 0.7
_oww_model = None


def _get_oww_model():
    global _oww_model
    if _oww_model is None:
        from openwakeword.model import Model
        _oww_model = Model(
            wakeword_models=["alexa"],
            inference_framework="onnx",
        )
        logger.info("OpenWakeWord loaded (alexa)")
    return _oww_model


def wait_for_wakeword(timeout: float = 0):
    """Block until 'hey jarvis' detected. timeout=0 waits forever."""
    import numpy as np
    import pyaudio

    CHUNK = 1280  # 80ms @ 16kHz
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
            for name, score in prediction.items():
                if score >= WAKEWORD_THRESHOLD:
                    logger.info(f"Wake word: '{name}' ({score:.3f})")
                    oww.reset()
                    return True
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()


# =============================================================================
# Pipeline session
# =============================================================================

async def run_pipeline_session(is_first: bool = False):
    projects_list = ", ".join(f"{k} ({v})" for k, v in PROJECTS.items())
    system = SYSTEM_PROMPT.format(projects=projects_list)

    transport = LocalAudioTransport(
        LocalAudioTransportParams(audio_in_enabled=True, audio_out_enabled=True)
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

    # Register 8 tools
    llm.register_function("connect_project", track_activity(handle_connect_project))
    llm.register_function("disconnect_project", track_activity(handle_disconnect_project))
    llm.register_function("coding_task", track_activity(handle_coding_task),
                          cancel_on_interruption=False, timeout_secs=30)
    llm.register_function("check_progress", track_activity(handle_check_progress))
    llm.register_function("management", track_activity(handle_management),
                          cancel_on_interruption=False, timeout_secs=60)
    llm.register_function("search_documents", track_activity(handle_search_documents),
                          timeout_secs=15)
    llm.register_function("github", track_activity(handle_github),
                          cancel_on_interruption=False, timeout_secs=30)
    llm.register_function("sleep", track_activity(handle_sleep),
                          cancel_on_interruption=False)

    # Build initial context
    if is_first:
        initial_msg = "Greet the user. You just started up. Be brief — one sentence."
    elif app.pending_claude_result:
        result = app.pending_claude_result
        app.pending_claude_result = None
        # Extract just the result text, skip metadata (instruction, elapsed, ops)
        lines = result.split("\n")
        # Find "Result:" line if present, else take last meaningful line
        short = result[:200]
        for line in lines:
            if line.startswith("Result:"):
                short = line[7:].strip()[:200]
                break
        initial_msg = f"Task done. Read this to the user: {short}"
    elif app.state == AppState.CODING:
        initial_msg = (
            f"The user is back. Connected to project '{app.active_project}'. "
            "Acknowledge briefly."
        )
    else:
        initial_msg = "The user just said 'hey jarvis'. Acknowledge briefly — you're ready."

    context = LLMContext([{"role": "user", "content": initial_msg}])
    user_params = LLMUserAggregatorParams(
        user_mute_strategies=[],
        vad_analyzer=SileroVADAnalyzer(),
    )
    user_agg, assistant_agg = LLMContextAggregatorPair(context, user_params=user_params)

    pipeline = Pipeline([
        transport.input(), user_agg, llm, transport.output(), assistant_agg,
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            enable_usage_metrics=True,
            idle_timeout_secs=None,
        ),
    )

    app.pipeline_task = task
    app.llm = llm
    app.last_activity = time.time()

    session_stop = asyncio.Event()

    has_pending_result = app.pending_claude_result is not None or (
        not is_first and "Coding task done" in initial_msg
    )

    async def start_conversation():
        await asyncio.sleep(1)
        logger.info(f"Triggering conversation. Initial msg: {initial_msg[:80]}")
        await task.queue_frames([LLMRunFrame()])

        # For pending results, also inject via send_client_content as backup
        # in case LLMContext initial message doesn't trigger speech
        if has_pending_result:
            await asyncio.sleep(3)
            try:
                if llm._session and not llm._disconnecting:
                    from google.genai.types import Content, Part
                    msg = Content(
                        role="user",
                        parts=[Part(text=initial_msg)]
                    )
                    await llm._session.send_client_content(
                        turns=[msg], turn_complete=True
                    )
                    logger.info("Backup result delivery via send_client_content")
            except Exception as e:
                logger.error(f"Backup delivery failed: {e}")

    async def keepalive():
        from google.genai.types import Blob
        silent = Blob(data=b"\x00" * 320, mime_type="audio/pcm;rate=16000")
        while not session_stop.is_set():
            try:
                await asyncio.wait_for(session_stop.wait(), timeout=20)
                break
            except asyncio.TimeoutError:
                pass
            try:
                if llm._session and not llm._disconnecting:
                    await llm._session.send_realtime_input(audio=silent)
            except Exception:
                pass

    async def idle_monitor():
        while not session_stop.is_set():
            try:
                await asyncio.wait_for(session_stop.wait(), timeout=10)
                break
            except asyncio.TimeoutError:
                pass

            if app.claude.is_busy:
                app.last_activity = time.time()
                continue

            if time.time() - app.last_activity > IDLE_TIMEOUT:
                logger.info("Idle timeout — disconnecting")
                await task.queue_frames([EndFrame()])
                return

    async def run_pipeline():
        runner = PipelineRunner(handle_sigint=False)
        await runner.run(task)
        session_stop.set()

    try:
        await asyncio.gather(run_pipeline(), start_conversation(), keepalive(), idle_monitor())
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning(f"Session ended: {e}")
    finally:
        session_stop.set()
        app.pipeline_task = None
        app.llm = None


# =============================================================================
# Main loop + signal handling
# =============================================================================

async def main():
    print("\n  Jarvis starting. Say 'hey jarvis' to wake. Ctrl+C to stop.\n")

    is_first = True
    while True:
        app.sleep_requested = False
        logger.info("Gemini session starting...")
        await run_pipeline_session(is_first=is_first)
        logger.info("Session ended, entering idle")

        is_first = False

        if app.pending_claude_result:
            logger.info("Claude result pending — reconnecting immediately")
            continue

        print("  Idle — listening for 'alexa'...")
        loop = asyncio.get_event_loop()
        while True:
            detected = await loop.run_in_executor(None, wait_for_wakeword, 5.0)
            if detected:
                logger.info("Wake word — reconnecting")
                break
            if app.pending_claude_result:
                logger.info("Claude result pending — reconnecting")
                break


def _force_exit(signum, frame):
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
