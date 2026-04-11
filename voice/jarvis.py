#!/usr/bin/env python3
"""
Jarvis — Personal Voice Assistant (Gemini mode)

Voice-controlled assistant using Gemini Live for conversation.
Hands off to Claude mode for coding via voice/claude_mode.py.

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audio import wait_for_wakeword, init_ack_cache, start_hotkey_listener
from session_manager import (
    load_projects, format_sessions_for_display, format_all_sessions,
    close_session as close_project_session,
)
from claude_mode import (
    run_claude_mode, get_all_session_statuses, kill_session, check_notifications,
)

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)

logger.remove(0)
logger.add(sys.stderr, level="INFO")


# =============================================================================
# Configuration
# =============================================================================

PROJECTS = load_projects()

WORKTREE_ROOT = os.path.expanduser("~/.nexus/documents")
MANAGEMENT_ROOT = os.path.expanduser("~/.nexus/management")
MANAGEMENT_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "scripts", "management")

IDLE_TIMEOUT = 420  # 7 minutes


# =============================================================================
# Context summarizer — keeps Gemini's context compact
# =============================================================================

_haiku_client = None


def _get_haiku():
    global _haiku_client
    if _haiku_client is None:
        from anthropic import Anthropic
        _haiku_client = Anthropic()
    return _haiku_client


def _summarize_context(conversation_text: str) -> str:
    """Summarize old conversation into a tight status line via Haiku."""
    try:
        response = _get_haiku().messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=(
                "Summarize this conversation into one short status line. "
                "Format: keyword tags of what happened. "
                "Only include what matters for continuity — active tasks, "
                "open documents, current browsing, pending requests. "
                "Drop greetings, completed actions, small talk. "
                "Example: 'browsed Gmail spam | searched Google Images for drone lidar | document report.docx open' "
                "Max 100 words. No filler."
            ),
            messages=[{
                "role": "user",
                "content": conversation_text[:3000],
            }],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error(f"Context summarization failed: {e}")
        return "previous conversation (summary unavailable)"


# =============================================================================
# App state — simpler now (coding state is in claude_mode)
# =============================================================================

class App:
    def __init__(self):
        self.pipeline_task = None
        self.llm = None
        self.last_activity = time.time()
        self.sleep_requested = False
        # Handoff to Claude mode
        self.enter_claude_mode = False
        self.claude_mode_project = None
        self.claude_mode_session = None
        self.claude_mode_path = None
        # Context for Gemini after returning from Claude mode
        self.returned_from_claude = False
        self.returned_project = None


app = App()


# =============================================================================
# Document search
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
# Management sync
# =============================================================================

def _sync_management(source: str = "all") -> bool:
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
# Tool handlers — 8 tools
# =============================================================================

# ── 1. connect_project ──

async def handle_connect_project(params: FunctionCallParams):
    """Show sessions or enter Claude coding mode."""
    project = params.arguments.get("project", "").lower().strip()
    session_choice = params.arguments.get("session_choice", "")

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

    # If no session_choice, show available sessions
    if not session_choice:
        info = await asyncio.to_thread(format_sessions_for_display, project)
        active = get_all_session_statuses()
        if project in active:
            info += f"\nNote: Claude is currently {active[project]} on this project."
        await params.result_callback({"result": info})
        return

    # With session_choice — trigger handoff to Claude mode
    app.enter_claude_mode = True
    app.claude_mode_project = project
    app.claude_mode_session = session_choice
    app.claude_mode_path = path

    await params.result_callback({
        "result": f"Switching to Claude coding mode for '{project}'. Goodbye for now."
    })

    # End Gemini pipeline after response plays
    await asyncio.sleep(3)
    if app.pipeline_task:
        await app.pipeline_task.queue_frames([EndFrame()])


# ── 2. list_sessions ──

async def handle_list_sessions(params: FunctionCallParams):
    """List all sessions across projects."""
    info = await asyncio.to_thread(format_all_sessions)
    active = get_all_session_statuses()
    if active:
        active_str = ", ".join(f"{p}: {s}" for p, s in active.items())
        info += f"\nActive tasks: {active_str}"
    await params.result_callback({"result": info})


# ── 3. close_session ──

async def handle_close_session(params: FunctionCallParams):
    """Close a project's coding session."""
    project = params.arguments.get("project", "").lower().strip()

    if project not in PROJECTS:
        available = ", ".join(PROJECTS.keys())
        await params.result_callback({
            "result": f"Unknown project '{project}'. Available: {available}"
        })
        return

    kill_session(project)
    await asyncio.to_thread(close_project_session, project)
    await params.result_callback({"result": f"Session closed for '{project}'."})


# ── 4. management ──

MAX_TOOL_RESULT = 4000  # Failsafe — Gemini chokes on very large tool results


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

    result = prefix + data
    if len(result) > MAX_TOOL_RESULT:
        result = result[:MAX_TOOL_RESULT] + "\n\n[Truncated — data too long for voice]"
        logger.info(f"Management result truncated to {MAX_TOOL_RESULT} chars")

    await params.result_callback({"result": result})


# ── 5. search_documents ──

async def handle_search_documents(params: FunctionCallParams):
    query = params.arguments.get("query", "")
    result = await asyncio.to_thread(search_worktree, query)
    if len(result) > MAX_TOOL_RESULT:
        result = result[:MAX_TOOL_RESULT] + "\n[Truncated]"
    await params.result_callback({"result": result})


# ── 6. github ──

async def handle_github(params: FunctionCallParams):
    query = params.arguments.get("query", "")

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


# ── 7. sleep ──

async def handle_sleep(params: FunctionCallParams):
    await params.result_callback({"result": "Going to sleep. Say 'hey jarvis' when you need me."})
    app.sleep_requested = True
    await asyncio.sleep(3)
    if app.pipeline_task:
        await app.pipeline_task.queue_frames([EndFrame()])


# ── 8. navigate_browser ──

NAV_RESULT_CAP = 3000  # Hard cap — Gemini crashes above this

# Persistent browser process — started once, reused across navigate calls
_browser_started = False


def _ensure_nav_browser():
    """Start the persistent browser if not running. Called in thread."""
    global _browser_started
    if _browser_started:
        from browser import is_running
        if is_running():
            return
    from browser import ensure_browser
    ensure_browser()
    _browser_started = True


def _run_nav_claude(destination: str, goal: str) -> str:
    """Run a Claude Code session for browser navigation. Returns concise result."""
    nav_script = os.path.join(os.path.dirname(__file__), "nav.py")
    venv_python = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "venv", "bin", "python3")
    )

    prompt = (
        f"Navigate the browser to: {destination}\n"
        f"Goal: {goal}\n\n"
        f"Use this CLI to control the browser:\n"
        f"  {venv_python} {nav_script} state        — see current page\n"
        f"  {venv_python} {nav_script} goto <url>   — go to URL\n"
        f"  {venv_python} {nav_script} click \"text\" — click link/button\n"
        f"  {venv_python} {nav_script} type \"field\" \"value\" — type into input\n"
        f"  {venv_python} {nav_script} press Enter  — press key\n"
        f"  {venv_python} {nav_script} scroll down  — scroll page\n\n"
        "RULES:\n"
        "- Start with 'state' to see current page, then navigate step by step.\n"
        "- Your ENTIRE final response must be under 150 chars.\n"
        "- Examples of good responses:\n"
        "    'Done. Gmail spam folder is open. 14 messages.'\n"
        "    'Login required. Opened login page in browser.'\n"
        "    'Error: page not found. Check the URL.'\n"
        "- NEVER explain what you did step by step. Just the end state.\n"
        "- If login is needed, say 'Login required' and stop.\n"
        "- If something fails after 3 attempts, say what went wrong in one sentence.\n"
    )

    cmd = [
        "claude", "--print", "--verbose",
        "--output-format", "stream-json",
        "--dangerously-skip-permissions",
        "-p", prompt,
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=os.path.dirname(__file__),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        result_text = ""
        start_time = time.time()
        nav_timeout = 90  # seconds

        # Read stream-json events, extract final result
        while proc.poll() is None:
            if time.time() - start_time > nav_timeout:
                proc.kill()
                return "Navigation timed out."

            line = proc.stdout.readline()
            if not line:
                break
            try:
                event = json.loads(line.decode("utf-8", errors="replace"))
                etype = event.get("type", "")

                if etype == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            text = block.get("text", "").strip()
                            if text:
                                result_text = text

                elif etype == "result":
                    result = event.get("result", "").strip()
                    if result:
                        result_text = result
            except json.JSONDecodeError:
                pass

        # Drain remaining output
        remaining = proc.stdout.read()
        if remaining:
            for raw in remaining.split(b"\n"):
                if raw.strip():
                    try:
                        event = json.loads(raw.decode("utf-8", errors="replace"))
                        if event.get("type") == "result":
                            result = event.get("result", "").strip()
                            if result:
                                result_text = result
                    except json.JSONDecodeError:
                        pass

        # Log stderr for debugging
        stderr = proc.stderr.read()
        if stderr:
            logger.warning(f"Navigate stderr: {stderr.decode()[:300]}")

        proc.wait(timeout=5)

        if not result_text:
            return "Navigation completed but no status returned."

        # HARD CAP — protect Gemini
        if len(result_text) > NAV_RESULT_CAP:
            result_text = result_text[:NAV_RESULT_CAP]

        return result_text

    except subprocess.TimeoutExpired:
        proc.kill()
        return "Navigation timed out."
    except Exception as e:
        return f"Navigation error: {str(e)[:200]}"


async def handle_navigate_browser(params: FunctionCallParams):
    """Navigate the browser to a destination. Runs Claude Code inline."""
    destination = params.arguments.get("destination", "")
    goal = params.arguments.get("goal", destination)

    if not destination:
        await params.result_callback({"result": "No destination specified."})
        return

    # Start browser in background thread (if not already running)
    try:
        await asyncio.to_thread(_ensure_nav_browser)
    except Exception as e:
        await params.result_callback({
            "result": f"Could not start browser: {str(e)[:200]}"
        })
        return

    # Run Claude Code navigation in thread (blocking, ~10-30s)
    logger.info(f"Navigate: {destination} — {goal}")
    result = await asyncio.to_thread(_run_nav_claude, destination, goal)
    logger.info(f"Navigate result: {result[:200]}")

    await params.result_callback({"result": result})


# ── 9. manage_windows ──

_screens_module = None

def _get_screens():
    """Lazy-import screens module from scripts/."""
    global _screens_module
    if _screens_module is None:
        scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
        sys.path.insert(0, os.path.abspath(scripts_dir))
        import screens
        _screens_module = screens
    return _screens_module


def _execute_window_action(action: str, app_name: str, position: str, screen: str, width: int, height: int, x: int, y: int) -> str:
    """Execute a window management action. Runs in thread."""
    s = _get_screens()

    if action == "list":
        windows = s.list_windows()
        if not windows:
            return "No visible windows found."
        lines = []
        for w in windows:
            lines.append(f"{w.process}: \"{w.title}\" at ({w.x},{w.y}) size {w.width}x{w.height}")
        return "\n".join(lines[:20])  # Cap at 20

    if not app_name:
        return "Error: app_name is required for this action."

    if action == "move":
        if position:
            s.snap_window(app_name, position, screen or "current")
            return f"Moved {app_name} to {position}" + (f" on {screen} screen" if screen else "")
        elif x is not None and y is not None:
            s.move_window(app_name, x, y)
            return f"Moved {app_name} to ({x}, {y})"
        else:
            return "Error: specify position (e.g. 'left', 'right') or x/y coordinates."

    elif action == "resize":
        if width and height:
            s.resize_window(app_name, width, height)
            return f"Resized {app_name} to {width}x{height}"
        else:
            return "Error: width and height required for resize."

    elif action == "maximize":
        s.maximize_window(app_name)
        return f"Maximized {app_name}"

    elif action == "minimize":
        ok = s.minimize_window(app_name)
        return f"Minimized {app_name}" if ok else f"Could not minimize {app_name}"

    elif action == "close":
        ok = s.close_window(app_name)
        return f"Closed {app_name}" if ok else f"Could not close {app_name}"

    elif action == "focus":
        s.focus_app(app_name)
        return f"Focused {app_name}"

    elif action == "find":
        win = s.find_window(app_name)
        if win:
            return f"{win.process}: \"{win.title}\" at ({win.x},{win.y}) size {win.width}x{win.height}"
        return f"No window found for '{app_name}'"

    else:
        return f"Unknown action: {action}"


async def handle_manage_windows(params: FunctionCallParams):
    """Manage windows — list, move, resize, close, minimize, maximize, focus."""
    action = params.arguments.get("action", "list")
    app_name = params.arguments.get("app_name", "")
    position = params.arguments.get("position", "")
    screen = params.arguments.get("screen", "")
    width = params.arguments.get("width", 0)
    height = params.arguments.get("height", 0)
    x = params.arguments.get("x")
    y = params.arguments.get("y")

    try:
        result = await asyncio.to_thread(
            _execute_window_action, action, app_name, position, screen, width, height, x, y
        )
    except Exception as e:
        result = f"Error: {str(e)[:200]}"

    await params.result_callback({"result": result})


# =============================================================================
# Tool schema — 9 tools
# =============================================================================

TOOLS = [
    {
        "function_declarations": [
            {
                "name": "connect_project",
                "description": "Connect to a project for coding. Say 'connect to X'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Project name"},
                        "session_choice": {
                            "type": "string",
                            "enum": ["last", "previous", "new"],
                            "description": "Which session. Omit to show options.",
                        },
                    },
                    "required": ["project"],
                },
            },
            {
                "name": "list_sessions",
                "description": "List coding sessions across projects.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "close_session",
                "description": "Close a coding session for a project.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "project": {"type": "string", "description": "Project name"},
                    },
                    "required": ["project"],
                },
            },
            {
                "name": "management",
                "description": "Access calendar, reminders, or email.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "enum": ["all", "calendar", "reminders", "email"],
                        },
                        "query": {"type": "string"},
                    },
                    "required": ["source"],
                },
            },
            {
                "name": "search_documents",
                "description": "Search the user's LOCAL document archive (OneDrive files) by keywords. NOT for web search — use navigate_browser for Google.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "github",
                "description": "Check GitHub repos and commits.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "sleep",
                "description": "Go to sleep. Say 'sleep' or 'goodbye'.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "navigate_browser",
                "description": "Open a website, navigate to a page, or search the web. Use for: 'search for X on Google', 'search Google Images for X', 'open Gmail spam', 'go to Shopify settings'. Handles Google Search, Google Images, Google News, Google Maps, and any website navigation.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "destination": {
                            "type": "string",
                            "description": "The website or search engine (e.g. 'gmail', 'google', 'google images', 'google news', 'shopify', 'figma')",
                        },
                        "goal": {
                            "type": "string",
                            "description": "What to do (e.g. 'search for drone lidar', 'spam folder', 'settings page')",
                        },
                    },
                    "required": ["destination", "goal"],
                },
            },
            {
                "name": "manage_windows",
                "description": "Manage application windows: list, move, resize, close, minimize, maximize, or focus. Use for any window management request.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["list", "move", "resize", "maximize", "minimize", "close", "focus", "find"],
                            "description": "What to do. 'list' shows all windows. 'move' positions a window. 'resize' changes size. 'maximize' fills the screen. 'close' closes the window.",
                        },
                        "app_name": {
                            "type": "string",
                            "description": "App name or substring to match (e.g. 'Chrome', 'iTerm', 'Finder', 'Slack'). Required for all actions except 'list'.",
                        },
                        "position": {
                            "type": "string",
                            "enum": ["left", "right", "top-left", "top-right", "bottom-left", "bottom-right", "center", "full"],
                            "description": "Preset position for 'move' action. Snaps the window to that portion of the screen.",
                        },
                        "screen": {
                            "type": "string",
                            "enum": ["main", "secondary", "other"],
                            "description": "Which screen to place the window on. 'other' moves it to the opposite screen from where it is now.",
                        },
                        "width": {"type": "integer", "description": "Width in pixels (for resize)."},
                        "height": {"type": "integer", "description": "Height in pixels (for resize)."},
                        "x": {"type": "integer", "description": "X position in pixels (for move, instead of preset position)."},
                        "y": {"type": "integer", "description": "Y position in pixels (for move, instead of preset position)."},
                    },
                    "required": ["action"],
                },
            },
        ]
    }
]


# =============================================================================
# System prompt
# =============================================================================

SYSTEM_PROMPT = """\
You are Jarvis, a personal assistant. Sharp, friendly, concise.

Rules:
- ALWAYS say something before calling a tool. Never go silent.
- When a tool returns data, summarize the key points in 2-4 spoken sentences. Do not read raw data.
- Keep ALL responses under 25 seconds of speech. Be brief. If there is too much data, give the highlights and ask if the user wants more detail.
- For briefings: top 3 items only, one sentence each.

Browser — the keyword "browser" means use navigate_browser:
- "browser, search for X" → navigate_browser (destination=google, goal=EXACT search query as user said it)
- "browser, search images of X" → navigate_browser (destination=google images, goal=EXACT search query)
- "browser, go to Gmail" → navigate_browser (destination=gmail, goal=open)
- "browser, open Shopify settings" → navigate_browser (destination=shopify, goal=settings)
- Any request starting with "browser" → navigate_browser. Always.
- Pass the user's search query as they said it. Do not rephrase or substitute search terms.
- When the result says "Login required", tell the user to enter credentials in the browser and say "done" when ready, then call navigate_browser again.

Document search — only when user says "search my documents" or "find in my files":
- "search my documents for X" → search_documents

Window requests (move, resize, close) → manage_windows

Available projects: {projects}
"""


# =============================================================================
# Wake word detection — reused from audio.py
# =============================================================================

WAKEWORD_THRESHOLD = 0.7


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
    llm.register_function("connect_project", track_activity(handle_connect_project),
                          cancel_on_interruption=False, timeout_secs=15)
    llm.register_function("list_sessions", track_activity(handle_list_sessions))
    llm.register_function("close_session", track_activity(handle_close_session))
    llm.register_function("management", track_activity(handle_management),
                          cancel_on_interruption=False, timeout_secs=60)
    llm.register_function("search_documents", track_activity(handle_search_documents),
                          timeout_secs=15)
    llm.register_function("github", track_activity(handle_github),
                          cancel_on_interruption=False, timeout_secs=30)
    llm.register_function("sleep", track_activity(handle_sleep),
                          cancel_on_interruption=False)
    llm.register_function("navigate_browser", track_activity(handle_navigate_browser),
                          cancel_on_interruption=False, timeout_secs=120)
    llm.register_function("manage_windows", track_activity(handle_manage_windows),
                          cancel_on_interruption=False, timeout_secs=15)

    # Build initial context
    if is_first:
        initial_msg = "Greet the user. You just started up. Be brief — one sentence."
    elif app.returned_from_claude:
        app.returned_from_claude = False
        proj = app.returned_project or "a project"
        active = get_all_session_statuses()
        status_info = ""
        if proj in active:
            status_info = f" Claude is still {active[proj]} on it."
        initial_msg = (
            f"The user just came back from coding on '{proj}'.{status_info} "
            "Welcome them back briefly and ask what they need."
        )
    else:
        initial_msg = "The user just said 'hey jarvis'. Acknowledge briefly — you're ready."

    context = LLMContext([{"role": "user", "content": initial_msg}])
    app.context = context  # Store for trimming

    user_params = LLMUserAggregatorParams(
        user_mute_strategies=[AlwaysUserMuteStrategy()],
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

    async def start_conversation():
        await asyncio.sleep(1)
        logger.info(f"Triggering conversation: {initial_msg[:80]}")
        await task.queue_frames([LLMRunFrame()])

    async def keepalive():
        from google.genai.types import Blob, Content, Part
        silent = Blob(data=b"\x00" * 320, mime_type="audio/pcm;rate=16000")
        last_connection_id = id(llm._session) if llm._session else None

        while not session_stop.is_set():
            try:
                await asyncio.wait_for(session_stop.wait(), timeout=10)
                break
            except asyncio.TimeoutError:
                pass
            try:
                if llm._session and not llm._disconnecting:
                    # Detect reconnection — session object changed
                    current_id = id(llm._session)
                    if last_connection_id and current_id != last_connection_id:
                        logger.info("Reconnection detected — re-prompting Gemini")
                        try:
                            msg = Content(
                                role="user",
                                parts=[Part(text="You just reconnected. Say 'I'm back' briefly.")]
                            )
                            await llm._session.send_client_content(
                                turns=[msg], turn_complete=True,
                            )
                        except Exception as e:
                            logger.error(f"Re-prompt failed: {e}")
                    last_connection_id = current_id

                    await llm._session.send_realtime_input(audio=silent)
            except Exception:
                pass

    async def context_trimmer():
        """Keep context at a fixed char budget — never let it bloat.

        Every 15s, measure total context size in chars. If over MAX_CONTEXT_CHARS,
        summarize the oldest messages until we're under budget. The context
        sent to Gemini stays roughly the same size always.
        """
        MAX_CONTEXT_CHARS = 2000

        def _measure(msgs):
            total = 0
            for msg in msgs:
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                total += len(str(content))
            return total

        while not session_stop.is_set():
            try:
                await asyncio.wait_for(session_stop.wait(), timeout=15)
                break
            except asyncio.TimeoutError:
                pass

            try:
                messages = context.get_messages()
                total_chars = _measure(messages)

                if total_chars <= MAX_CONTEXT_CHARS:
                    continue

                # Walk from the end to find how many recent messages fit in half the budget
                keep_budget = MAX_CONTEXT_CHARS // 2
                keep_chars = 0
                keep_from = len(messages)
                for i in range(len(messages) - 1, -1, -1):
                    content = messages[i].get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    msg_len = len(str(content))
                    if keep_chars + msg_len > keep_budget and keep_from < len(messages):
                        break
                    keep_chars += msg_len
                    keep_from = i

                old_messages = messages[:keep_from]
                recent_messages = messages[keep_from:]

                # Summarize old messages
                old_text = []
                for msg in old_messages:
                    role = msg.get("role", "?")
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                    if content:
                        old_text.append(f"{role}: {str(content)[:200]}")

                if not old_text:
                    continue

                summary = await asyncio.to_thread(
                    _summarize_context, "\n".join(old_text)
                )

                new_messages = [
                    {"role": "user", "content": f"[Context: {summary}]"},
                ] + recent_messages
                context.set_messages(new_messages)
                logger.info(
                    f"Context trimmed: {total_chars} → {_measure(new_messages)} chars"
                )
            except Exception as e:
                logger.error(f"Context trim error: {e}")

    async def idle_monitor():
        while not session_stop.is_set():
            try:
                await asyncio.wait_for(session_stop.wait(), timeout=10)
                break
            except asyncio.TimeoutError:
                pass

            if time.time() - app.last_activity > IDLE_TIMEOUT:
                logger.info("Idle timeout — disconnecting")
                await task.queue_frames([EndFrame()])
                return

    async def notification_monitor():
        """Check for Claude Code task completions and inject into Gemini."""
        while not session_stop.is_set():
            try:
                await asyncio.wait_for(session_stop.wait(), timeout=5)
                break
            except asyncio.TimeoutError:
                pass

            notifications = check_notifications()
            for project, summary in notifications:
                logger.info(f"Delivering notification: {project}")
                try:
                    if llm._session and not llm._disconnecting:
                        from google.genai.types import Content, Part
                        msg = Content(
                            role="user",
                            parts=[Part(text=(
                                f"IMPORTANT: Claude just finished a coding task on '{project}'. "
                                f"Tell the user immediately: '{summary[:200]}'. "
                                "Be brief but make sure they know."
                            ))],
                        )
                        await llm._session.send_client_content(
                            turns=[msg], turn_complete=True,
                        )
                        app.last_activity = time.time()
                except Exception as e:
                    logger.error(f"Notification delivery failed: {e}")

    async def run_pipeline():
        runner = PipelineRunner(handle_sigint=False)
        await runner.run(task)
        session_stop.set()

    try:
        await asyncio.gather(
            run_pipeline(), start_conversation(), keepalive(),
            idle_monitor(), notification_monitor(), context_trimmer(),
        )
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning(f"Session ended: {e}")
    finally:
        session_stop.set()
        app.pipeline_task = None
        app.llm = None


# =============================================================================
# Main loop — Gemini ↔ Claude mode switching
# =============================================================================

async def main():
    print("\n  Jarvis starting. Say 'hey jarvis' to wake. Ctrl+C to stop.\n")

    # Pre-initialize acknowledgment cache for Claude mode
    try:
        await asyncio.to_thread(init_ack_cache)
        start_hotkey_listener()
        # Pre-load Whisper so Claude mode doesn't wait 4s on first use
        from audio import get_whisper
        await asyncio.to_thread(get_whisper)
    except Exception as e:
        logger.warning(f"Ack cache init failed (non-fatal): {e}")

    is_first = True
    while True:
        app.sleep_requested = False
        app.enter_claude_mode = False

        # ── Run Gemini pipeline ──
        logger.info("Gemini session starting...")
        await run_pipeline_session(is_first=is_first)
        logger.info("Gemini session ended")
        is_first = False

        # ── Check for Claude mode handoff ──
        if app.enter_claude_mode:
            logger.info(f"Entering Claude mode: {app.claude_mode_project}")
            result = await run_claude_mode(
                app.claude_mode_project,
                app.claude_mode_session,
                app.claude_mode_path,
            )
            logger.info(f"Claude mode returned: {result}")

            app.returned_from_claude = True
            app.returned_project = app.claude_mode_project

            # Reset handoff state
            app.enter_claude_mode = False
            app.claude_mode_project = None
            app.claude_mode_session = None
            app.claude_mode_path = None

            # Go straight back to Gemini (no wake word needed)
            continue

        # ── Idle — wait for wake word ──
        if not app.sleep_requested:
            # Pipeline ended for other reasons (timeout, error)
            pass

        print("  Idle — listening for 'hey jarvis'...")
        loop = asyncio.get_event_loop()
        while True:
            detected = await loop.run_in_executor(None, wait_for_wakeword, 5.0)
            if detected:
                logger.info("Wake word detected")
                break
            # Also check for task completion notifications
            notifications = check_notifications()
            if notifications:
                logger.info("Task completed while idle — reconnecting to notify")
                break


def _force_exit(signum, frame):
    # Kill any active Claude Code sessions
    for project in list(get_all_session_statuses().keys()):
        kill_session(project)
    # Stop persistent browser if running
    try:
        from browser import stop_browser
        stop_browser()
    except Exception:
        pass
    print("\n  Jarvis stopped.\n")
    os._exit(0)


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGINT, _force_exit)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _force_exit(None, None)
