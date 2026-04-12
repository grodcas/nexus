#!/usr/bin/env python3
"""
Jarvis Slim — direct Gemini Live API, no pipecat.

Mic → Gemini Live websocket → speaker.
Gemini's own VAD, minimal prompt, slim tools.
Long tool results bypass Gemini and go straight to TTS.
"""

import asyncio
import json
import os
import subprocess
import sys
import time

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)

from google import genai
from google.genai import types
from loguru import logger
import pyaudio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))
from session_manager import load_projects, format_sessions_for_display
from claude_mode import run_claude_mode
import screens

_handoff: dict = {"project": None, "session": None, "path": None}

logger.remove(0)
logger.add(sys.stderr, level="INFO")


# =============================================================================
# Config
# =============================================================================

PROJECTS = load_projects()
SAMPLE_RATE = 16000
RECV_RATE = 24000
CHUNK = 960  # 60ms at 16kHz

# Tool result limits — above this, TTS speaks directly, Gemini gets "Done."
SHORT_RESULT_LIMIT = 300


# =============================================================================
# System prompt — under 300 chars
# =============================================================================

SYSTEM_PROMPT = "Be brief. Answer from your own knowledge first. Use the do tool only when the request needs an action."

# =============================================================================
# Tool declarations — minimal
# =============================================================================

TOOL_DECLARATIONS = [
    types.FunctionDeclaration(
        name="do",
        description="Execute an actionable request.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "action": types.Schema(
                    type=types.Type.STRING,
                    description="One of: browse, search, calendar, email, reminders, briefing, documents, code, github, window, sleep.",
                ),
                "query": types.Schema(
                    type=types.Type.STRING,
                    description=(
                        "Details in user's words. For code: project name "
                        f"(one of: {', '.join(PROJECTS.keys())}). "
                        "For window: a verb-led command like 'move chrome left', "
                        "'move chrome to other screen', 'move chrome left on secondary screen', "
                        "'maximize iterm on main', 'close finder', 'list'."
                    ),
                ),
                "session": types.Schema(
                    type=types.Type.STRING,
                    description="For code only: 'last', 'previous', or 'new'. Omit on the first call to list available sessions.",
                ),
            },
            required=["action"],
        ),
    ),
]


# =============================================================================
# Print what Gemini sees
# =============================================================================

def print_budget():
    """Print the token budget breakdown."""
    tools_str = str([{
        "name": d.name,
        "description": d.description,
        "parameters": str(d.parameters),
    } for d in TOOL_DECLARATIONS])

    print("\n  === GEMINI BUDGET ===")
    print(f"  System prompt:     {len(SYSTEM_PROMPT):>4} chars")
    print(f"  Tool declarations: {len(tools_str):>4} chars")
    print(f"  Context budget:    ~200 chars (managed)")
    print(f"  ─────────────────────────")
    print(f"  Total baseline:    {len(SYSTEM_PROMPT) + len(tools_str):>4} chars")
    print()
    print(f"  System prompt: \"{SYSTEM_PROMPT}\"")
    print()
    print(f"  Tool: do(action, query)")
    print(f"    actions: browse, search, calendar, email, reminders, briefing, documents, code, github, window, sleep")
    print()


# =============================================================================
# Tool handlers
# =============================================================================

WORKTREE_ROOT = os.path.expanduser("~/.nexus/documents")
MANAGEMENT_ROOT = os.path.expanduser("~/.nexus/management")
MANAGEMENT_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "scripts", "management")


def _sync_management(source="all"):
    venv_python = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "venv", "bin", "python3"))
    cmd = [venv_python, "sync_all.py"]
    if source != "all":
        cmd.append(f"--{source}")
    try:
        subprocess.run(cmd, cwd=MANAGEMENT_SCRIPTS, capture_output=True, text=True, timeout=60)
    except Exception as e:
        logger.error(f"Sync failed: {e}")


def _read_file(path):
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return ""


def _search_worktree(query):
    words = [w.lower() for w in query.split() if len(w) > 2]
    if not words:
        return "No results."
    results = []
    for dirpath, _, filenames in os.walk(WORKTREE_ROOT):
        for fname in filenames:
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(dirpath, fname)
            with open(fpath) as f:
                for line in f:
                    if all(w in line.lower() for w in words):
                        results.append(f"[{fname}] {line.strip()}")
            if len(results) >= 10:
                break
    return "\n".join(results) if results else "Nothing found."


def _run_nav_claude(destination, goal):
    """Run Claude Code for browser navigation."""
    nav_script = os.path.abspath(os.path.join(os.path.dirname(__file__), "nav.py"))
    venv_python = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "venv", "bin", "python3"))

    prompt = (
        f"Navigate the browser to: {destination}\nGoal: {goal}\n\n"
        f"Use: {venv_python} {nav_script} <cmd>\n"
        f"Commands: state, goto <url>, click \"text\", type \"field\" \"value\", press Enter, scroll down\n"
        "Start with state. Final response under 150 chars. If login needed say 'Login required'."
    )

    cmd = ["claude", "--print", "--verbose", "--output-format", "stream-json",
           "--dangerously-skip-permissions", "-p", prompt]
    try:
        proc = subprocess.Popen(cmd, cwd=os.path.dirname(__file__),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        result_text = ""
        start = time.time()
        while proc.poll() is None and time.time() - start < 90:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                event = json.loads(line.decode("utf-8", errors="replace"))
                if event.get("type") == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        if block.get("type") == "text" and block.get("text", "").strip():
                            result_text = block["text"].strip()
                elif event.get("type") == "result":
                    r = event.get("result", "").strip()
                    if r:
                        result_text = r
            except json.JSONDecodeError:
                pass
        proc.wait(timeout=5)
        return result_text or "Navigation done."
    except Exception as e:
        return f"Error: {str(e)[:100]}"


_WINDOW_POSITIONS = [
    "top-left", "top-right", "bottom-left", "bottom-right",
    "left", "right", "center", "full",
]

_WINDOW_SCREENS = {
    "other": "other",
    "main": "main",
    "primary": "main",
    "secondary": "secondary",
}

_BROWSER_PROCESS_HINTS = (
    "chrome", "safari", "arc", "brave", "firefox", "edge", "vivaldi", "opera"
)


def _resolve_app_alias(app: str) -> str:
    """
    Resolve voice-friendly aliases like 'browser' or 'current browser'
    to the actual running process name.
    """
    a = app.strip().lower()
    if a in ("browser", "current browser", "the browser", "web browser"):
        try:
            front = screens.get_frontmost_app()
            if front and any(h in front.lower() for h in _BROWSER_PROCESS_HINTS):
                return front
            for w in screens.list_windows():
                if any(h in w.process.lower() for h in _BROWSER_PROCESS_HINTS):
                    return w.process
        except Exception:
            pass
    return app


def _handle_window(query: str) -> str:
    """Parse a freeform window command and dispatch to scripts/screens.py."""
    q = (query or "").lower().strip()
    if not q or q == "list":
        wins = screens.list_windows()
        if not wins:
            return "No windows."
        return "\n".join(f"{w.process}: {w.title[:40]}" for w in wins[:15])

    # Strip stop-words that voice STT often inserts.
    for noise in (" to the ", " to a ", " to ", " on the ", " on ", " the "):
        q = q.replace(noise, " ")
    parts = q.replace("-", " ").split()
    verb = parts[0] if parts else ""
    rest = parts[1:]
    rest_joined = " ".join(rest)

    # Extract screen target ("other screen", "main display", etc.).
    screen = None
    for word, value in _WINDOW_SCREENS.items():
        if word in rest_joined.split():
            screen = value
            rest_joined = " ".join(w for w in rest_joined.split() if w != word)
            break
    # Drop the literal "screen"/"display"/"monitor" trailing tokens.
    rest_joined = " ".join(
        w for w in rest_joined.split() if w not in ("screen", "display", "monitor")
    )

    # Extract position.
    position = None
    for p in _WINDOW_POSITIONS:
        if p.replace("-", " ") in rest_joined:
            position = p
            rest_joined = rest_joined.replace(p.replace("-", " "), "").strip()
            break

    app = rest_joined.strip()
    if not app and verb != "list":
        return "Specify an app name."
    app = _resolve_app_alias(app)

    try:
        if verb in ("move", "snap", "place", "send"):
            # Cross-screen move with no explicit position → default to full.
            if screen and not position:
                position = "full"
            if position:
                screens.snap_window(app, position, screen or "current")
            screens.raise_window(app)
            where = []
            if position:
                where.append(position)
            if screen:
                where.append(f"on {screen} screen")
            suffix = (" to " + " ".join(where)) if where else ""
            return f"Moved {app}{suffix}"
        if verb in ("maximize", "fullscreen", "full"):
            if screen:
                screens.snap_window(app, "full", screen)
            else:
                screens.maximize_window(app)
            screens.raise_window(app)
            return f"Maximized {app}" + (f" on {screen} screen" if screen else "")
        if verb == "minimize":
            screens.minimize_window(app)
            return f"Minimized {app}"
        if verb == "close":
            screens.close_window(app)
            return f"Closed {app}"
        if verb == "focus":
            screens.focus_app(app)
            return f"Focused {app}"
        return f"Unknown window verb: {verb}"
    except Exception as e:
        return f"Window error: {str(e)[:80]}"


def handle_tool(action: str, query: str = "", session: str = "") -> tuple[str, bool]:
    """
    Execute tool. Returns (result_text, is_long).
    If is_long=True, result should be spoken by TTS directly (bypass Gemini).
    """
    action = action.lower().strip()

    if action == "sleep":
        return "Going to sleep.", False

    elif action in ("browse", "search", "navigate"):
        # No hardcoded destination map — pass the user's query through.
        # The inner nav agent decides where to go from the query itself.
        try:
            from browser import ensure_browser
            ensure_browser()
        except Exception as e:
            return f"Browser error: {str(e)[:100]}", False

        result = _run_nav_claude(query or "google", query)
        return result[:SHORT_RESULT_LIMIT], False

    elif action == "window":
        return _handle_window(query), False

    elif action in ("calendar", "email", "reminders"):
        _sync_management(action)
        filename = {"calendar": "calendar.md", "reminders": "reminders.md", "email": "email.md"}
        data = _read_file(os.path.join(MANAGEMENT_ROOT, filename.get(action, "calendar.md")))
        if not data:
            return f"No {action} data.", False
        # Long result — TTS speaks it, Gemini gets short confirmation
        return data[:3000], True

    elif action == "briefing":
        _sync_management("all")
        data = _read_file(os.path.join(MANAGEMENT_ROOT, "root.md"))
        return data[:3000], True

    elif action == "documents":
        result = _search_worktree(query)
        if len(result) > SHORT_RESULT_LIMIT:
            return result, True
        return result, False

    elif action in ("code", "connect"):
        project = (query or "").lower().strip()
        if project not in PROJECTS:
            return f"Unknown project. Available: {', '.join(PROJECTS.keys())}", False

        path = os.path.expanduser(PROJECTS[project])
        if not os.path.isdir(path):
            return f"Path '{path}' not found.", False

        choice = (session or "").lower().strip()
        if choice not in ("last", "previous", "new"):
            return format_sessions_for_display(project), False

        # Stage handoff — main loop will close Gemini and run Claude mode.
        _handoff["project"] = project
        _handoff["session"] = choice
        _handoff["path"] = path
        return f"Switching to Claude coding mode for {project}. Goodbye for now.", False

    elif action == "github":
        try:
            r = subprocess.run(
                ["gh", "api", "/user/repos?sort=pushed&per_page=5",
                 "--jq", r'.[] | "\(.name) — \(.pushed_at)"'],
                capture_output=True, text=True, timeout=15,
            )
            return r.stdout.strip()[:SHORT_RESULT_LIMIT] or "No repos.", False
        except Exception:
            return "GitHub error.", False

    return f"Unknown action: {action}", False


# =============================================================================
# TTS — for long results that bypass Gemini
# =============================================================================

def tts_speak_long(text: str):
    """Speak long text directly via macOS say. Non-blocking-ish."""
    # Summarize for speech — just first ~500 chars
    short = text[:500]
    subprocess.run(["say", "-r", "200", short], timeout=60)


# =============================================================================
# Main loop
# =============================================================================

async def main():
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    print_budget()

    config = types.LiveConnectConfig(
        system_instruction=SYSTEM_PROMPT,
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Aoede")
            )
        ),
        tools=[types.Tool(function_declarations=TOOL_DECLARATIONS)],
    )

    pa = pyaudio.PyAudio()
    print("  Jarvis Slim running. Just talk. Ctrl+C to quit.\n")

    try:
        while True:
            _handoff["project"] = None
            _handoff["session"] = None
            _handoff["path"] = None

            mic = pa.open(format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE,
                          input=True, frames_per_buffer=CHUNK)
            spk = pa.open(format=pyaudio.paInt16, channels=1, rate=RECV_RATE,
                          output=True, frames_per_buffer=4096)

            try:
                async with client.aio.live.connect(
                    model="gemini-2.5-flash-native-audio-preview-12-2025",
                    config=config,
                ) as session:

                    async def send_audio():
                        loop = asyncio.get_event_loop()
                        while True:
                            data = await loop.run_in_executor(None, mic.read, CHUNK, False)
                            await session.send_realtime_input(
                                audio=types.Blob(data=data, mime_type="audio/pcm;rate=16000")
                            )

                    async def receive():
                        while True:
                            async for msg in session.receive():
                                if msg.data:
                                    spk.write(msg.data)

                                if msg.tool_call:
                                    for fc in msg.tool_call.function_calls:
                                        logger.info(f"Tool call: {fc.name}({dict(fc.args)})")
                                        args = dict(fc.args) if fc.args else {}
                                        action = args.get("action", "")
                                        query = args.get("query", "")
                                        sess_choice = args.get("session", "")

                                        result, is_long = await asyncio.to_thread(
                                            handle_tool, action, query, sess_choice
                                        )

                                        if is_long:
                                            await asyncio.to_thread(tts_speak_long, result)
                                            gemini_result = "Done. Already spoken to user."
                                        else:
                                            gemini_result = result

                                        logger.info(f"Result ({len(gemini_result)} chars): {gemini_result[:100]}")

                                        await session.send_tool_response(
                                            function_responses=[types.FunctionResponse(
                                                name=fc.name,
                                                id=fc.id,
                                                response={"result": gemini_result},
                                            )]
                                        )

                                        if _handoff["project"]:
                                            # Let the goodbye line play, then exit Gemini.
                                            await asyncio.sleep(2.5)
                                            return

                    send_task = asyncio.create_task(send_audio())
                    recv_task = asyncio.create_task(receive())
                    try:
                        done, pending = await asyncio.wait(
                            [send_task, recv_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for t in pending:
                            t.cancel()
                        for t in pending:
                            try:
                                await t
                            except (asyncio.CancelledError, Exception):
                                pass
                    except (KeyboardInterrupt, asyncio.CancelledError):
                        send_task.cancel()
                        recv_task.cancel()
            finally:
                mic.close()
                spk.close()

            if _handoff["project"]:
                proj = _handoff["project"]
                sess = _handoff["session"]
                path = _handoff["path"]
                logger.info(f"Entering Claude mode: {proj} ({sess})")
                try:
                    await run_claude_mode(proj, sess, path)
                except Exception as e:
                    logger.error(f"Claude mode error: {e}")
                logger.info("Claude mode returned — back to Gemini")
                continue

            break
    finally:
        pa.terminate()
        print("\n  Jarvis stopped.\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  Jarvis stopped.\n")
