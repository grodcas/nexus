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
from session_manager import load_projects

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

SYSTEM_PROMPT = "Be brief. Use the do tool for any actionable request."

# =============================================================================
# Tool declarations — minimal
# =============================================================================

TOOL_DECLARATIONS = [
    types.FunctionDeclaration(
        name="do",
        description="Execute any user request: browse web, search, open sites, check calendar/email/reminders, search documents, connect to coding project, sleep.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "action": types.Schema(
                    type=types.Type.STRING,
                    description="What to do: browse, search, calendar, email, reminders, documents, code, github, sleep",
                ),
                "query": types.Schema(
                    type=types.Type.STRING,
                    description="Details: URL, search terms, project name, etc.",
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
    print(f"    actions: browse, search, calendar, email, reminders, documents, code, github, sleep")
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


def handle_tool(action: str, query: str = "") -> tuple[str, bool]:
    """
    Execute tool. Returns (result_text, is_long).
    If is_long=True, result should be spoken by TTS directly (bypass Gemini).
    """
    action = action.lower().strip()

    if action == "sleep":
        return "Going to sleep.", False

    elif action in ("browse", "search", "navigate"):
        # Determine destination
        q = query.lower()
        if "image" in q:
            dest = "google images"
        elif "news" in q:
            dest = "google news"
        elif "map" in q:
            dest = "google maps"
        elif any(site in q for site in ["gmail", "shopify", "figma", "youtube", "github"]):
            dest = q.split()[0] if q else "google"
        elif action == "browse" and query:
            dest = query
        else:
            dest = "google"

        # Start browser
        try:
            from browser import ensure_browser
            ensure_browser()
        except Exception as e:
            return f"Browser error: {str(e)[:100]}", False

        result = _run_nav_claude(dest, query)
        return result[:SHORT_RESULT_LIMIT], False

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
        project = query.lower().strip() if query else ""
        if project in PROJECTS:
            return f"CONNECT:{project}", False
        return f"Unknown project. Available: {', '.join(PROJECTS.keys())}", False

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
    mic = pa.open(format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE,
                  input=True, frames_per_buffer=CHUNK)
    spk = pa.open(format=pyaudio.paInt16, channels=1, rate=RECV_RATE,
                  output=True, frames_per_buffer=4096)

    print("  Jarvis Slim running. Just talk. Ctrl+C to quit.\n")

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
                    # Play audio
                    if msg.data:
                        spk.write(msg.data)

                    # Handle tool calls
                    if msg.tool_call:
                        for fc in msg.tool_call.function_calls:
                            logger.info(f"Tool call: {fc.name}({dict(fc.args)})")
                            args = dict(fc.args) if fc.args else {}
                            action = args.get("action", "")
                            query = args.get("query", "")

                            result, is_long = await asyncio.to_thread(
                                handle_tool, action, query
                            )

                            if is_long:
                                # Speak directly via TTS, tell Gemini it's done
                                await asyncio.to_thread(tts_speak_long, result)
                                gemini_result = "Done. Already spoken to user."
                            else:
                                gemini_result = result

                            logger.info(f"Result ({len(gemini_result)} chars): {gemini_result[:100]}")

                            # Send result back to Gemini
                            await session.send_tool_response(
                                function_responses=[types.FunctionResponse(
                                    name=fc.name,
                                    id=fc.id,
                                    response={"result": gemini_result},
                                )]
                            )

        try:
            await asyncio.gather(send_audio(), receive())
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    mic.close()
    spk.close()
    pa.terminate()
    print("\n  Jarvis stopped.\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  Jarvis stopped.\n")
