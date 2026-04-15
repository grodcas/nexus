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
import re
import subprocess
import sys
import threading
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
from metrics import timed, log_event, mark_cold_warm
import screens

_handoff: dict = {"project": None, "session": None, "path": None}
_sleep_requested: bool = False  # set by the sleep action; main loop exits

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
# Browse/search return a richer natural-language summary so Gemini
# can relay it conversationally. We let this path use up to 800
# chars so the inner Claude agent can return 2-4 full sentences
# with concrete facts.
BROWSE_RESULT_LIMIT = 800


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

# Phase 1C — cache-first management reads.
#
# Plan 1A measured every management action (briefing/calendar/reminders/
# email) paying the full sync cost on every call: 30s for calendar/
# reminders, 60s for briefing (which hit the hard timeout and was
# *failing* silently). The same data rarely changes more than once per
# few minutes, so we switch to a cache-first pattern:
#
#   - Return the cached markdown file immediately (sub-millisecond).
#   - If the cache is older than _SYNC_TTL and no background sync is
#     already in flight for that source, launch one on the main event
#     loop. The next call picks up the fresh data.
#   - If there is no cache on disk yet (first-ever call on this machine),
#     run the sync synchronously — there's nothing to return otherwise.
#
# The main event loop reference is captured in main() so handle_tool
# (which runs inside asyncio.to_thread) can schedule coroutines onto
# the real loop via run_coroutine_threadsafe.

_SYNC_TTL_S = 120.0  # cached data older than this triggers a background refresh
_LAST_SYNC: dict[str, float] = {}   # source → monotonic timestamp of last successful sync start
_SYNC_IN_FLIGHT: set[str] = set()   # sources currently being synced in the background
_MAIN_LOOP: asyncio.AbstractEventLoop | None = None  # set in main()


def _sync_management(source="all"):
    venv_python = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "venv", "bin", "python3"))
    cmd = [venv_python, "sync_all.py"]
    if source != "all":
        cmd.append(f"--{source}")
    with timed("management.sync_subprocess", source=source):
        try:
            subprocess.run(cmd, cwd=MANAGEMENT_SCRIPTS, capture_output=True, text=True, timeout=60)
        except Exception as e:
            logger.error(f"Sync failed: {e}")


def _management_path(source: str) -> str:
    filename = {
        "calendar": "calendar.md",
        "reminders": "reminders.md",
        "email": "email.md",
        "all": "root.md",
    }.get(source, "root.md")
    return os.path.join(MANAGEMENT_ROOT, filename)


def _background_sync(source: str) -> None:
    """Run _sync_management in a thread and clear the in-flight flag."""
    try:
        _sync_management(source)
        _LAST_SYNC[source] = time.monotonic()
    except Exception as e:
        logger.error(f"Background sync {source} failed: {e}")
    finally:
        _SYNC_IN_FLIGHT.discard(source)


def _maybe_sync(source: str) -> str:
    """
    Return the cached management data for `source` immediately. If
    the cache is stale (>_SYNC_TTL_S old) or missing, refresh.

    Cache hit (fresh): sub-ms return, no sync.
    Cache hit (stale): sub-ms return, background sync kicked off.
    Cache miss (first run): synchronous sync, then return.
    """
    path = _management_path(source)
    data = _read_file(path)
    age = time.monotonic() - _LAST_SYNC.get(source, 0.0)

    if not data:
        # No cache — must sync synchronously, nothing to return otherwise.
        _sync_management(source)
        _LAST_SYNC[source] = time.monotonic()
        log_event(phase="management.cache_miss_sync", source=source)
        return _read_file(path)

    if age > _SYNC_TTL_S and source not in _SYNC_IN_FLIGHT:
        _SYNC_IN_FLIGHT.add(source)
        log_event(phase="management.background_sync_started", source=source,
                  age_s=round(age, 1))
        # Run the sync in a daemon thread so we return immediately.
        # We don't schedule on the event loop (even though one exists)
        # because handle_tool already runs inside asyncio.to_thread,
        # and a plain thread.Thread here is simpler and needs no loop
        # reference at all. The TTL + in-flight set coalesce duplicates.
        import threading
        threading.Thread(
            target=_background_sync,
            args=(source,),
            daemon=True,
            name=f"nexus-mgmt-sync-{source}",
        ).start()
    else:
        log_event(phase="management.cache_hit", source=source, age_s=round(age, 1))

    return data


def _read_file(path):
    with timed("management.file_read", path=os.path.basename(path)):
        if os.path.exists(path):
            with open(path) as f:
                return f.read()
        return ""


def _search_worktree(query):
    words = [w.lower() for w in query.split() if len(w) > 2]
    if not words:
        return "No results."
    results = []
    files_scanned = 0
    with timed("documents.walk_scan", query_len=len(query)):
        for dirpath, _, filenames in os.walk(WORKTREE_ROOT):
            for fname in filenames:
                if not fname.endswith(".md"):
                    continue
                fpath = os.path.join(dirpath, fname)
                files_scanned += 1
                with open(fpath) as f:
                    for line in f:
                        if all(w in line.lower() for w in words):
                            results.append(f"[{fname}] {line.strip()}")
                if len(results) >= 10:
                    break
    log_event(phase="documents.scan_summary", files_scanned=files_scanned,
              hits=len(results))
    return "\n".join(results) if results else "Nothing found."


def _run_nav_claude(destination, goal):
    """Run Claude Code for browser navigation."""
    nav_script = os.path.abspath(os.path.join(os.path.dirname(__file__), "nav.py"))
    venv_python = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "venv", "bin", "python3"))

    prompt = (
        f"Navigate the browser to: {destination}\nGoal: {goal}\n\n"
        f"Use: {venv_python} {nav_script} <cmd>\n"
        f"Commands: state, goto <url>, click \"text\", type \"field\" \"value\", press Enter, scroll down\n"
        "Start with state. Prefer direct URLs over clicking when the site "
        "exposes a stable URL for the section you need.\n"
        "\n"
        "FINAL RESPONSE: write 2 to 4 full sentences that directly answer the "
        "user's question with the actual facts you found on the page. Include "
        "concrete numbers, dates, names, or quoted phrases when they appear. "
        "Do NOT describe the browsing process. Do NOT say 'I navigated to...'. "
        "Write as if you are telling a friend the answer. Aim for 200 to 500 "
        "characters. If login is required say 'Login required'. If the page "
        "did not have the information say 'Not found on that page' and suggest "
        "one alternative URL to try."
    )

    cmd = ["claude", "--print", "--verbose", "--output-format", "stream-json",
           "--dangerously-skip-permissions", "-p", prompt]
    try:
        spawn_start = time.perf_counter()
        proc = subprocess.Popen(cmd, cwd=os.path.dirname(__file__),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        log_event(phase="browse.claude_subprocess_spawn",
                  duration_ms=round((time.perf_counter() - spawn_start) * 1000, 2))

        result_text = ""
        first_token_logged = False
        start = time.time()
        while proc.poll() is None and time.time() - start < 90:
            line = proc.stdout.readline()
            if not line:
                break
            if not first_token_logged:
                log_event(phase="browse.claude_first_token",
                          duration_ms=round((time.time() - start) * 1000, 2))
                first_token_logged = True
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
        log_event(phase="browse.claude_total",
                  duration_ms=round((time.time() - start) * 1000, 2))
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

def _open_window_processes() -> list[str]:
    """Distinct process names from currently visible windows."""
    try:
        with timed("window.list_windows"):
            return sorted({w.process for w in screens.list_windows()})
    except Exception:
        return []


def _match_open_window(app: str) -> str | None:
    """
    Try to map a user-given app string to an actually-open window process.
    Returns the real process name on success, None if nothing plausible.
    """
    a = app.strip().lower()
    if not a:
        return None
    procs = _open_window_processes()
    if not procs:
        return None
    # Substring either way (handles 'chrome' → 'Google Chrome', 'iterm' → 'iTerm2').
    for p in procs:
        pl = p.lower()
        if a in pl or pl in a:
            return p
    # Token overlap (handles multi-word app names).
    user_words = set(a.split())
    for p in procs:
        if user_words & set(p.lower().split()):
            return p
    return None


def _handle_window(query: str) -> str:
    """Parse a freeform window command and dispatch to scripts/screens.py."""
    q = (query or "").lower().strip()
    if not q or q == "list":
        with timed("window.list_windows"):
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

    matched = _match_open_window(app)
    if matched:
        app = matched
    elif verb not in ("list",):
        # Hand Gemini the live list so it can retry with a real name.
        procs = _open_window_processes()
        if procs:
            return f"No window matches '{app}'. Open windows: {', '.join(procs)}."
        return f"No open windows."

    try:
      with timed("window.applescript_dispatch", verb=verb):
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
    cold = mark_cold_warm(f"handle_tool.{action}")
    call_start = time.perf_counter()

    # Phase 1D — ack-before-await.
    # Some actions unavoidably take several seconds (browse/search fire
    # an inner Claude subprocess that is the dominant latency). Instead
    # of silent dead air, speak a short local ack line NOW via macOS
    # `say`, non-blocking. The ack plays for ~0.8s while the handler
    # works; by the time the real answer comes back, the ack has long
    # since finished. Only actions whose post-1C warm latency is >1s
    # get an ack — everything else would stutter over itself.
    _speak_ack(action)

    try:
        if action == "sleep":
            # Close the Gemini session and enter a local wake-word
            # listener. The main loop reads _sleep_requested, tears
            # down the current Gemini connection, and calls
            # _wait_for_wake_word() which streams mic audio into
            # faster-whisper locally (no Gemini cost, no network)
            # until the user says the wake word again, then reopens
            # a fresh Gemini session. Ctrl+C is still the real exit.
            global _sleep_requested
            _sleep_requested = True
            return "Goodbye.", False

        elif action in ("browse", "search", "navigate"):
            # No hardcoded destination map — pass the user's query through.
            # The inner nav agent decides where to go from the query itself.
            try:
                with timed("browse.ensure_browser"):
                    from browser import ensure_browser
                    ensure_browser()
            except Exception as e:
                return f"Browser error: {str(e)[:100]}", False

            result = _run_nav_claude(query or "google", query)
            # Frame the findings so Gemini relays them in a natural
            # spoken sentence instead of just saying "done" or
            # dropping the user on a page without an explanation.
            # This framing lives in the tool_response, NOT the system
            # prompt, so the Gemini budget discipline is preserved —
            # these extra tokens only count when search is used.
            framed = (
                f"Findings from the web for the user's request "
                f"'{(query or '').strip()[:120]}':\n\n"
                f"{result[:BROWSE_RESULT_LIMIT]}\n\n"
                "Relay this to the user now in 1-3 natural spoken "
                "sentences. Include the concrete facts above. Do not "
                "say you searched; just give the answer."
            )
            return framed, False

        elif action == "window":
            return _handle_window(query), False

        elif action in ("calendar", "email", "reminders"):
            data = _maybe_sync(action)
            if not data:
                return f"No {action} data.", False
            # Long result — TTS speaks it, Gemini gets short confirmation.
            # The cached intro from _ACTION_INTRO plays first (instant,
            # 0 ms gap), then this body synthesizes while the intro is
            # still playing, so there's no dead air.
            body = data[:3000].strip()
            return body, True

        elif action == "briefing":
            data = _maybe_sync("all")
            if not data:
                return "No briefing data available.", False
            body = data[:3000].strip()
            return body, True

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
                with timed("github.gh_subprocess"):
                    r = subprocess.run(
                        ["gh", "api", "/user/repos?sort=pushed&per_page=5",
                         "--jq", r'.[] | "\(.name) — \(.pushed_at)"'],
                        capture_output=True, text=True, timeout=15,
                    )
                return r.stdout.strip()[:SHORT_RESULT_LIMIT] or "No repos.", False
            except Exception:
                return "GitHub error.", False

        return f"Unknown action: {action}", False
    finally:
        log_event(
            phase="handle_tool.total",
            action=action,
            query_len=len(query or ""),
            duration_ms=round((time.perf_counter() - call_start) * 1000, 2),
            cold=cold,
        )


# =============================================================================
# TTS — for long results that bypass Gemini
# =============================================================================

# TTS bypass — fire-and-forget macOS `say`, interruptible.
#
# Contract (JARVIS_GUIDE rule #6): when a tool returns is_long=True,
# Nexus must return "Done. Already spoken to user." to Gemini
# *instantly* so Gemini's audio flow control stays sane, while `say`
# plays the real content through the speaker in the background. The
# receive loop must never block on `say` finishing — doing so causes
# the tool_response to arrive seconds late, the mic audio backlog to
# overflow, and Gemini Live to drop the websocket with 1011 (learned
# the hard way by shipping it blocking on 2026-04-15 and watching
# the first live briefing kill the session).
#
# Design:
#   1. Start `say` via Popen with stdin=PIPE. No communicate(), no
#      wait. We push the text into stdin and close the pipe; `say`
#      plays it from its own buffer.
#   2. Store the Popen handle in _ACTIVE_TTS so the next tool_call
#      can call _kill_active_tts() to interrupt (the briefing-in-
#      progress "never mind, search for X" flow).
#   3. Voice is set via NEXUS_TTS_VOICE env var or the default
#      constant below. macOS Premium voices ("Ava (Premium)",
#      "Zoe (Premium)", "Tom (Premium)") sound dramatically better
#      than the ancient Samantha default; install via
#      System Settings → Accessibility → Spoken Content → System
#      Voice → Manage Voices.
#   4. No argv length limit because we use stdin — briefings up to
#      3000 chars pass cleanly (kernel pipe buffer is 64 KB).

_ACTIVE_TTS: subprocess.Popen | None = None

# =============================================================================
# Mic gating — prevent the speaker→mic feedback loop
# =============================================================================
#
# Without headphones, Gemini's own audio output through the laptop
# speaker is picked up by the laptop mic and streamed back into
# the Live websocket as "user input". Gemini then processes its
# own voice as if you had spoken, transcribes it, and generates
# follow-up turns that sound like it's talking to itself ("did you
# mean a notification?" → "yes notification, what about it?" →
# etc.). Classic feedback pathology in every hands-free voice UX.
#
# Fix: gate the outbound mic stream whenever EITHER:
#   (a) a local TTS subprocess is playing (afplay / say), OR
#   (b) Gemini has sent us audio within the last
#       _MIC_GATE_TAIL_S seconds (Gemini is mid-utterance or
#       just finished; the 400 ms tail catches the speaker ring
#       and the echo bounce).
#
# Trade-off: the user loses the ability to "barge in" on Gemini
# mid-sentence while the gate is active. That's acceptable because
# the alternative is Gemini talking to itself, which is unusable.
# Headphones still eliminate the problem entirely and disable the
# gate's usefulness, but the gate is cheap when not needed so we
# leave it on by default.
#
# Disable via NEXUS_MIC_GATE=0 in .env if you're on headphones
# and want barge-in back. Tune the tail via NEXUS_MIC_GATE_TAIL_MS.

_MIC_GATE_ENABLED = os.environ.get("NEXUS_MIC_GATE", "1").lower() not in ("0", "false", "no")
try:
    _MIC_GATE_TAIL_S = float(os.environ.get("NEXUS_MIC_GATE_TAIL_MS", "400")) / 1000.0
except ValueError:
    _MIC_GATE_TAIL_S = 0.4
_last_gemini_audio_ts: float = 0.0


def _mic_should_be_muted() -> bool:
    """
    Return True if the outbound mic stream should be suppressed
    this frame. Called from send_audio() every ~60 ms.
    """
    if not _MIC_GATE_ENABLED:
        return False
    # Local TTS playing → always mute (briefing / ack lines).
    if _ACTIVE_TTS is not None and _ACTIVE_TTS.poll() is None:
        return True
    # Within tail window after Gemini's last audio chunk → mute.
    if _last_gemini_audio_ts > 0.0:
        if (time.monotonic() - _last_gemini_audio_ts) < _MIC_GATE_TAIL_S:
            return True
    return False

# Override by setting NEXUS_TTS_VOICE=... in .env. First entry that
# actually exists on this machine is used; falls through to default
# system voice if none are installed.
_TTS_VOICE_CANDIDATES: tuple[str, ...] = (
    os.environ.get("NEXUS_TTS_VOICE", ""),
    "Ava (Premium)",
    "Zoe (Premium)",
    "Tom (Premium)",
    "Evan (Premium)",
    "Samantha",  # ancient but always present — last resort
)
_TTS_RATE = "175"  # macOS default; Premium voices (Ava) sound unnatural faster


def _pick_voice() -> str | None:
    """
    Pick the first installed voice from the candidate list. Caches
    the result on first call. Returns None if nothing matches (in
    which case `say` uses the system default, which is fine).
    """
    global _CHOSEN_VOICE
    if _CHOSEN_VOICE is not None:
        return _CHOSEN_VOICE or None  # empty string sentinel = no override
    try:
        out = subprocess.run(
            ["say", "-v", "?"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        _CHOSEN_VOICE = ""
        return None
    installed = {line.split()[0] if line.strip() else "" for line in out.splitlines()}
    # `say -v ?` outputs "Ava (Premium)      en_US    # ..." — the name
    # part may include parens, so use a substring match instead.
    installed_full = out
    for candidate in _TTS_VOICE_CANDIDATES:
        if candidate and candidate in installed_full:
            _CHOSEN_VOICE = candidate
            logger.info(f"TTS voice: {candidate}")
            return candidate
    _CHOSEN_VOICE = ""
    logger.info("TTS voice: system default")
    return None


_CHOSEN_VOICE: str | None = None

# Phase 1D — ack-before-await.
#
# Actions whose post-1C warm latency is >1s get a short local ack line
# spoken via `say` the moment handle_tool is called. Everything else is
# fast enough that an ack would stutter against the real result. Ack
# lines are deliberately generic (no app names, no segment language —
# rule #5 from JARVIS_GUIDE).
ACK_LINES: dict[str, str] = {
    "browse":   "On it.",
    "search":   "Searching.",
    "navigate": "On it.",
}


def _speak_ack(action: str) -> None:
    """Fire-and-forget local `say` with the ack line for this action."""
    line = ACK_LINES.get(action)
    if not line:
        return
    try:
        cmd = ["say", "-r", _TTS_RATE]
        voice = _pick_voice()
        if voice:
            cmd += ["-v", voice]
        cmd.append(line)
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        logger.warning(f"ack `say` failed: {e}")


# =============================================================================
# Phase 1E — Trigger-word hard gate
#
# Gemini Live sometimes decides to call a tool just because "the
# context sounds operational," even when the user said nothing that
# should have triggered an action. Prompt edits alone cannot fix this
# reliably (the JARVIS_GUIDE anti-patterns document at least four
# times we tried). The structural fix is to enable input audio
# transcription on the Live session, accumulate the user's current
# turn transcript in a rolling buffer, and — before dispatching any
# gated tool_call — confirm the buffer contains a trigger token.
#
# If the transcript is empty (e.g. the tool_call arrived before the
# transcription did), the gate falls OPEN: we'd rather miss a block
# than block a legitimate call. Quantify the rate later in Plan 2.
#
# `sleep` is explicitly ungated so "go to sleep" still works without
# saying a trigger word. Conversational answers (no tool call at all)
# are never touched by the gate — it only intercepts dispatch.
# =============================================================================

# =============================================================================
# Trigger words — two separate sets for two separate jobs
# =============================================================================
#
# COMMAND TRIGGER (the agent's "name")
#   Used by the trigger-word gate in active mode. Every first-turn
#   tool call must have this in the transcript. The trust window
#   then carries the next ~60s of follow-ups without needing it.
#
# WAKE PHRASE (only to leave sleep mode)
#   Used by the local wake-word listener when Nexus is sleeping.
#   Ignored by the active-mode gate. Meant to be something you'd
#   naturally say to a sleeping agent and that Whisper transcribes
#   reliably (multi-word phrases are best — see below).
#
# Lessons from the live iterations:
#
#   - Single proper-noun wake words ("jarvis", "atlas") are
#     unreliable in Gemini Live STT and Whisper small. Both
#     mangle or drop them under real-world mic conditions.
#   - Common English words — especially real words that happen to
#     make sense as names — are dramatically more robust because
#     they're over-represented in STT training data.
#   - Multi-word phrases are even more robust because STT models
#     are trained on phrase-level n-grams. "wake up" as the sleep
#     phrase transcribes ~100% of the time.
#
# Default command trigger: "friday"
#   Real English word (day of the week → top-100 frequency).
#   Sharp consonants (Fr-, -day), natural to say as an address,
#   pop-culture safe ("Friday, search for X"), and not a substring
#   of any common word.
#
# Default wake phrase: "wake up"
#   Semantically literal ("wake the sleeping agent"), both words
#   top-100 English, rare as a standalone utterance at a desk.
#
# Overrides via .env:
#   NEXUS_COMMAND_TRIGGERS="friday"       # comma-separated, active-mode
#   NEXUS_WAKE_PHRASES="wake up"          # comma-separated, sleep-mode
#
# Back-compat: NEXUS_TRIGGER_WORDS (from the earlier "one trigger
# for everything" attempt) still works — if set, it populates the
# command-trigger list as before. New installs should use the new
# vars.
#
# Good alternatives for command trigger:
#   "computer"  — Star-Trek classic, single word, 3 syllables
#   "morgan"    — real name, 2 syllables, very STT-reliable
#   "sage"      — short, distinctive, uncommon at desk
#   "sonny"     — proper name from pop culture, clear phonemes
#
# Bad:
#   "echo"      — too common, false positives everywhere
#   "halo"      — substring of "hall"
#   "nex"       — matches "next"
#   "max"       — too short, ambiguous

def _parse_trigger_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return tuple(t.strip().lower() for t in raw.split(",") if t.strip())


# Command triggers — active mode gate.
_COMMAND_TRIGGERS: tuple[str, ...] = _parse_trigger_env(
    "NEXUS_COMMAND_TRIGGERS",
    _parse_trigger_env("NEXUS_TRIGGER_WORDS", ("honey",)),  # back-compat
)

# Wake phrases — sleep mode listener only.
_WAKE_TRIGGERS: tuple[str, ...] = _parse_trigger_env(
    "NEXUS_WAKE_PHRASES", ("wake up",)
)

# Kept for back-compat with score.py / eval imports. Points at the
# active-mode set, which is what the gate uses.
_TRIGGER_FUZZY: tuple[str, ...] = _COMMAND_TRIGGERS
TRIGGER_TOKENS: set[str] = set(_COMMAND_TRIGGERS)

ACTION_GATE: set[str] = {
    "browse", "search", "navigate", "documents", "window", "code", "connect",
    "briefing", "calendar", "email", "reminders", "github",
}

# Stateful trust window (Finding #1 from plan2_baseline.md).
#
# After any successful gated tool call, the next _GATE_TRUST_WINDOW_S
# seconds of turns bypass the trigger-word check. Real conversations
# don't repeat the wake word on every follow-up — "jarvis put chrome
# on the left" is naturally followed by "and safari on the right"
# without re-saying "jarvis". The strict gate breaks that flow and
# was the single biggest failure cluster in the Plan 2 baseline.
#
# Reset at the start of each Gemini session so a fresh launch always
# requires an explicit trigger on the first call (no accidental
# ambient-trigger from a previous run).
_GATE_TRUST_WINDOW_S = 60.0
_last_gate_pass_ts: float = 0.0


def _transcript_has_trigger(
    transcript: str,
    tokens: tuple[str, ...] = _COMMAND_TRIGGERS,
) -> bool:
    """
    Word-boundary check for any of the given trigger tokens,
    case-insensitive.

    Word boundaries matter once you pick a common English word
    as a trigger — plain substring would have "honey" match
    "honeymoon"/"honeybee"/"honeycomb". We compile a regex
    that requires \\b on each side of every token so the
    trigger must appear as its own word (or phrase, for
    multi-word tokens like "wake up").

    Defaults to the active-mode command triggers; the sleep
    listener passes _WAKE_TRIGGERS explicitly.
    """
    if not transcript or not tokens:
        return False
    # Build the regex lazily-per-call. Small enough to not cache.
    pattern = r"\b(?:" + "|".join(re.escape(t) for t in tokens) + r")\b"
    return re.search(pattern, transcript, re.IGNORECASE) is not None


def _gate_in_trust_window() -> bool:
    """True if we're within the post-successful-call trust window."""
    if _last_gate_pass_ts <= 0.0:
        return False
    return (time.monotonic() - _last_gate_pass_ts) < _GATE_TRUST_WINDOW_S


def _mark_gate_pass() -> None:
    """Called after an allowed gated tool call — opens the trust window."""
    global _last_gate_pass_ts
    _last_gate_pass_ts = time.monotonic()


# =============================================================================
# Sleep mode — local wake-word listener
# =============================================================================
#
# When the user says "atlas go to sleep", the Gemini session closes
# (which stops the hot-mic billing and the audio websocket). Then we
# enter a local listener that reads the mic in ~2s windows, pipes
# each window to faster-whisper, and checks the transcript for a
# trigger word. When found, return True so the main loop can open a
# fresh Gemini session. On KeyboardInterrupt, return False and the
# main loop breaks out for real.
#
# faster-whisper "small" int8 runs at ~300-600ms per 2s window on
# Apple Silicon — well under the window length, so we never fall
# behind. First call loads the model (~1-2s); subsequent calls are
# cached.
#
# We do NOT send any audio to Gemini during sleep. Zero network, zero
# Gemini cost. The speaker is also free — nothing plays until wake.

_SLEEP_WINDOW_S = 2.0       # seconds of audio per whisper call
_SLEEP_POLL_S = 0.1         # how often to check the transcript buffer

def _wait_for_wake_word(pa: "pyaudio.PyAudio") -> bool:
    """
    Block until the user says a trigger word, or Ctrl+C.

    Returns True on wake, False on interrupt.
    """
    import numpy as np

    try:
        from audio import get_whisper, transcribe  # type: ignore
    except Exception as e:
        logger.error(f"wake listener unavailable: {e}")
        return False

    # Pre-load Whisper so the first wake window doesn't eat 1-2s on
    # model init while the user is already talking.
    try:
        get_whisper()
    except Exception as e:
        logger.error(f"whisper init failed, falling back to exit: {e}")
        return False

    # Speak a short local confirmation so the user knows sleep mode
    # is armed (and knows the wake phrase — this is the WAKE set,
    # NOT the command triggers used in active mode).
    wake_names = ", ".join(sorted(_WAKE_TRIGGERS))
    try:
        cmd = ["say", "-r", _TTS_RATE]
        voice = _pick_voice()
        if voice:
            cmd += ["-v", voice]
        cmd.append(f"Sleeping. Say {wake_names} to wake me.")
        subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

    logger.info(f"sleeping — waiting for wake phrase ({wake_names})")

    mic = pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK,
    )
    try:
        chunks_per_window = int(SAMPLE_RATE * _SLEEP_WINDOW_S / CHUNK)
        window: list[bytes] = []
        while True:
            try:
                data = mic.read(CHUNK, exception_on_overflow=False)
            except KeyboardInterrupt:
                return False
            window.append(data)
            if len(window) < chunks_per_window:
                continue
            # Full 2s window — transcribe and check for trigger.
            audio_bytes = b"".join(window)
            window = []
            try:
                audio_arr = np.frombuffer(audio_bytes, dtype=np.int16)
                text = transcribe(audio_arr)
            except Exception as e:
                logger.warning(f"wake transcribe failed: {e}")
                continue
            if not text:
                continue
            if _transcript_has_trigger(text, _WAKE_TRIGGERS):
                logger.info(f"wake phrase heard: {text!r}")
                return True
            # Slight breath before the next window so we don't pin a
            # core if whisper ever returns faster than realtime.
            # (Unlikely at 2s window / 300-600ms whisper, but cheap.)
            if _SLEEP_POLL_S > 0:
                time.sleep(_SLEEP_POLL_S)
    except KeyboardInterrupt:
        return False
    finally:
        try:
            mic.close()
        except Exception:
            pass


def _kill_active_tts() -> None:
    """Kill any in-flight `say` subprocess. Safe to call repeatedly."""
    global _ACTIVE_TTS
    proc = _ACTIVE_TTS
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass
    _ACTIVE_TTS = None


# Google Cloud TTS voice for long results (briefing / calendar /
# email / reminders / documents). Chirp3-HD-Aoede is the Cloud TTS
# equivalent of the Gemini Live Aoede voice we use for
# conversational audio, so briefing speech matches the rest of the
# product. Override via NEXUS_TTS_CLOUD_VOICE in .env if you want
# a different Chirp3 HD voice — see `gcloud text-to-speech voices
# list` for the full catalog (Puck, Charon, Kore, Fenrir, etc).
_CLOUD_TTS_VOICE = os.environ.get("NEXUS_TTS_CLOUD_VOICE", "en-US-Chirp3-HD-Aoede")
_CLOUD_TTS_SAMPLE_RATE = 24000  # Chirp3 HD default
_CLOUD_TTS_ENABLED = os.environ.get("NEXUS_TTS_CLOUD", "1").lower() not in ("0", "false", "no")

# -----------------------------------------------------------------------------
# Pre-cached intro phrases for smooth tool handovers
# -----------------------------------------------------------------------------
#
# Core UX problem before this: after the user asks for a briefing,
# Cloud TTS takes 500-1500ms to synthesize. During that gap the
# product sounds dead ("I talk and then I get a briefing"). The fix
# is to pre-synthesize short intro phrases at startup, cache them as
# WAVs on disk, and play the cached intro instantly (0 ms latency)
# while the body synth happens in parallel. The intro is ~1-2s long
# so by the time it finishes, the body is already synthesized and
# ready to play back-to-back. Perceived latency = 0.
#
# Cache lives in ~/.nexus/tts_cache/<voice>-<sha>.wav. The hash is
# over (voice, rate, text) so if the voice or rate changes the
# cache auto-refreshes. Cache survives across runs so the startup
# pre-warm is only slow the very first time.

_TTS_CACHE_DIR = os.path.expanduser("~/.nexus/tts_cache")
_CACHED_WAV: dict[str, str] = {}  # key → path

# Intro phrases by action. Each maps an action name to (key, text)
# where `key` is the cache lookup and `text` is what gets synthesized.
_ACTION_INTRO: dict[str, tuple[str, str]] = {
    "briefing":  ("briefing_intro",  "Here is your briefing for today."),
    "calendar":  ("calendar_intro",  "Here is your calendar."),
    "email":     ("email_intro",     "Here are your emails."),
    "reminders": ("reminders_intro", "Here are your reminders."),
    "documents": ("documents_intro", "Let me look."),
}


def _cache_key_path(key: str) -> str:
    import hashlib
    h = hashlib.sha1(
        f"{_CLOUD_TTS_VOICE}|{_CLOUD_TTS_SAMPLE_RATE}|{key}".encode()
    ).hexdigest()[:12]
    return os.path.join(_TTS_CACHE_DIR, f"{key}-{h}.wav")


def _synth_to_wav_path(text: str, out_path: str) -> None:
    """Synthesize `text` to Cloud TTS and write a LINEAR16 WAV at out_path."""
    from google.cloud import texttospeech  # type: ignore
    from audio import get_tts  # type: ignore
    import wave
    client = get_tts()
    resp = client.synthesize_speech(
        input=texttospeech.SynthesisInput(text=text),
        voice=texttospeech.VoiceSelectionParams(
            language_code="en-US",
            name=_CLOUD_TTS_VOICE,
        ),
        audio_config=texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.LINEAR16,
            sample_rate_hertz=_CLOUD_TTS_SAMPLE_RATE,
            speaking_rate=1.0,
        ),
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with wave.open(out_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_CLOUD_TTS_SAMPLE_RATE)
        w.writeframes(resp.audio_content)


def _precache_phrase(key: str, text: str) -> None:
    """
    Ensure a cached WAV exists for this key. Reuses on-disk cache
    if the (voice, rate, key) hash matches; synthesizes otherwise.
    """
    path = _cache_key_path(key)
    if os.path.exists(path) and os.path.getsize(path) > 100:
        _CACHED_WAV[key] = path
        return
    try:
        _synth_to_wav_path(text, path)
        _CACHED_WAV[key] = path
    except Exception as e:
        logger.warning(f"precache {key} failed: {e}")


def _prewarm_phrases() -> None:
    """
    Synthesize every _ACTION_INTRO entry on startup. Called from a
    background task in main(), so the app stays responsive during
    the ~3s initial cache build on first run. Subsequent runs are
    near-instant because the cache is persistent.
    """
    for _, (key, text) in _ACTION_INTRO.items():
        _precache_phrase(key, text)
    logger.info(f"tts cache ready: {len(_CACHED_WAV)} phrases")


def _afplay_popen(path: str) -> subprocess.Popen:
    """Spawn afplay on a WAV file, non-blocking."""
    return subprocess.Popen(
        ["afplay", path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _speak_via_cloud_tts(text: str, intro_key: str | None = None) -> None:
    """
    Play a smooth [cached intro] → [synthesized body] sequence.
    Runs in its own thread so tts_speak_long returns instantly.
    Sets _ACTIVE_TTS to whichever subprocess is currently playing
    so _kill_active_tts() can interrupt either the intro or body.
    Falls back to macOS `say` on any synth error.

    Flow:
      t=0     user's question ends, dispatch reaches here
      t=0     spawn afplay on cached intro WAV (instant, 0 ms gap)
      t=0     start synth of body in parallel
      t=~500  synth of body completes, wait for intro to finish
      t=~1200 intro afplay finishes, spawn body afplay
      t=~1200 body playback begins, continues for body length
              no perceptible gap between intro and body
    """
    global _ACTIVE_TTS
    try:
        import tempfile
        import wave
        from google.cloud import texttospeech  # type: ignore
        from audio import get_tts  # type: ignore

        # Phase 1 — play cached intro IMMEDIATELY if we have one.
        intro_proc: subprocess.Popen | None = None
        if intro_key and intro_key in _CACHED_WAV:
            intro_path = _CACHED_WAV[intro_key]
            intro_proc = _afplay_popen(intro_path)
            _ACTIVE_TTS = intro_proc

        # Phase 2 — synthesize the body in parallel with intro playback.
        client = get_tts()
        resp = client.synthesize_speech(
            input=texttospeech.SynthesisInput(text=text),
            voice=texttospeech.VoiceSelectionParams(
                language_code="en-US",
                name=_CLOUD_TTS_VOICE,
            ),
            audio_config=texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.LINEAR16,
                sample_rate_hertz=_CLOUD_TTS_SAMPLE_RATE,
                speaking_rate=1.0,
            ),
        )

        with tempfile.NamedTemporaryFile(
            suffix=".wav", delete=False, prefix="nexus-tts-",
        ) as f:
            wav_path = f.name
        with wave.open(wav_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(_CLOUD_TTS_SAMPLE_RATE)
            w.writeframes(resp.audio_content)

        # Phase 3 — wait for intro playback to finish, then spawn body.
        # If the intro was killed externally (_kill_active_tts), its
        # wait returns immediately; we don't start the body in that
        # case because the user has moved on.
        if intro_proc is not None:
            intro_proc.wait()
            if intro_proc.returncode not in (0, None):
                # Intro was killed (SIGTERM / SIGKILL) — don't play body.
                return

        body_proc = _afplay_popen(wav_path)
        _ACTIVE_TTS = body_proc
        logger.info(f"cloud TTS playing ({_CLOUD_TTS_VOICE}, {len(text)} chars)")
    except Exception as e:
        logger.warning(f"Google Cloud TTS failed ({e}); falling back to say")
        _speak_via_say(text)


def _speak_via_say(text: str) -> None:
    """Fallback: macOS `say` pipe-stdin, same shape as before."""
    global _ACTIVE_TTS
    try:
        cmd = ["say", "-r", _TTS_RATE]
        voice = _pick_voice()
        if voice:
            cmd += ["-v", voice]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _ACTIVE_TTS = proc
        assert proc.stdin is not None
        proc.stdin.write(text.encode("utf-8"))
        proc.stdin.close()
    except Exception as e:
        logger.error(f"say fallback failed: {e}")
        _ACTIVE_TTS = None


def tts_speak_long(text: str, intro_key: str | None = None) -> None:
    """
    Fire-and-forget long-form speech. Returns immediately — does
    NOT wait for synthesis or playback to finish. Synthesis and
    playback happen on a daemon thread; the caller uses
    _wait_for_tts_done() if it needs to know when playback is
    complete.

    If intro_key is set and matches a cached phrase, play the
    cached intro instantly as phase 1 of the sequence, then
    synthesize and play the body. This eliminates the 500-1500ms
    synth-latency gap at the start of a briefing, because the
    intro covers the synth time.

    Path:
      NEXUS_TTS_CLOUD=1 (default) → Google Cloud TTS Chirp3-HD-Aoede
      NEXUS_TTS_CLOUD=0           → macOS `say` with the chosen
                                    Ava Premium / fallback voice
      On any Cloud TTS error → falls back to `say`.
    """
    if not text:
        return
    _kill_active_tts()

    if not _CLOUD_TTS_ENABLED:
        threading.Thread(
            target=_speak_via_say,
            args=(text,),
            daemon=True,
            name="nexus-tts-say",
        ).start()
        return

    threading.Thread(
        target=_speak_via_cloud_tts,
        args=(text, intro_key),
        daemon=True,
        name="nexus-tts-cloud",
    ).start()


async def _wait_for_tts_done(
    max_start_s: float = 3.0,
    max_total_s: float = 120.0,
) -> None:
    """
    Async-wait until the in-flight long-form TTS has finished
    playing. Yields to the event loop so receive() and
    send_audio() keep running while we wait — which is what keeps
    the session alive and the mic gate effective.

    Two phases:
      1. Wait up to max_start_s for the background thread to
         actually start playback (synth + afplay.spawn). If it
         never starts (synth crashed without setting _ACTIVE_TTS),
         return early — the caller will send the tool_response
         anyway so the session doesn't stall.
      2. Wait up to max_total_s for the playback subprocess to
         exit. Bounded so a stuck afplay can't hang the session
         forever.
    """
    start = time.monotonic()

    # Phase 1 — wait for playback to start.
    while _ACTIVE_TTS is None:
        if (time.monotonic() - start) > max_start_s:
            return
        await asyncio.sleep(0.05)

    # Phase 2 — wait for playback to finish.
    proc = _ACTIVE_TTS
    while proc is not None and proc.poll() is None:
        if (time.monotonic() - start) > max_total_s:
            return
        await asyncio.sleep(0.15)
        proc = _ACTIVE_TTS  # could be replaced by a new TTS; refresh


# =============================================================================
# Main loop
# =============================================================================

async def main():
    # All module-level flags we assign inside main() must be declared
    # global up front — otherwise Python treats them as locals across
    # the whole function, and the nested receive() closure sees a
    # broken cell. _sleep_requested was silently falling out of scope
    # before this line was added, which is why sleep mode looked
    # "exactly like before" — the main loop never saw the flag flip.
    global _MAIN_LOOP, _sleep_requested, _last_gate_pass_ts, _last_gemini_audio_ts
    _MAIN_LOOP = asyncio.get_running_loop()  # used by _maybe_sync's background scheduler

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    print_budget()

    # Phase 1B — pre-warm browser on app start.
    # ensure_browser is idempotent; firing it now means the first
    # browse/search call of the day finds the browser already up.
    # Failures are logged and swallowed — the in-handle_tool fallback
    # path still works.
    async def _prewarm_browser():
        try:
            from browser import ensure_browser
            await asyncio.to_thread(ensure_browser)
            logger.info("Browser pre-warmed")
        except Exception as e:
            logger.warning(f"Browser pre-warm failed (continuing): {e}")
    asyncio.create_task(_prewarm_browser())

    # Pre-warm the TTS voice lookup — first call to `say -v ?` takes
    # ~140ms; doing it here makes the first real briefing instant.
    asyncio.create_task(asyncio.to_thread(_pick_voice))

    # Pre-warm the Google Cloud TTS client (~500ms first-call init)
    # AND pre-cache all the intro phrases so the first real tool
    # call has a zero-latency intro to play while the body synth
    # runs. One-time ~3s cost on first run; persistent cache on
    # disk makes subsequent runs near-instant.
    if _CLOUD_TTS_ENABLED:
        async def _prewarm_cloud_tts():
            try:
                from audio import get_tts
                await asyncio.to_thread(get_tts)
                logger.info("Cloud TTS pre-warmed")
                await asyncio.to_thread(_prewarm_phrases)
            except Exception as e:
                logger.warning(f"Cloud TTS pre-warm failed (will fall back to say): {e}")
        asyncio.create_task(_prewarm_cloud_tts())

    config = types.LiveConnectConfig(
        system_instruction=SYSTEM_PROMPT,
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Aoede")
            )
        ),
        tools=[types.Tool(function_declarations=TOOL_DECLARATIONS)],
        # Phase 1E — enable input transcription so we can gate tool
        # dispatch on trigger-word presence in the user's utterance.
        input_audio_transcription=types.AudioTranscriptionConfig(),
    )

    pa = pyaudio.PyAudio()
    print("  Jarvis Slim running. Just talk. Ctrl+C to quit.\n")

    try:
        while True:
            # Every iteration of this loop is a genuinely fresh
            # Gemini session — no turn history, no transcript
            # buffer, no trust window leak from the prior run.
            # Explicit reset of every piece of per-session state
            # we touch so waking from sleep is indistinguishable
            # from a cold start.
            _handoff["project"] = None
            _handoff["session"] = None
            _handoff["path"] = None
            _last_gate_pass_ts = 0.0
            _last_gemini_audio_ts = 0.0  # mic gate starts open
            _kill_active_tts()  # no leftover say/afplay from prior run

            mic = pa.open(format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE,
                          input=True, frames_per_buffer=CHUNK)
            spk = pa.open(format=pyaudio.paInt16, channels=1, rate=RECV_RATE,
                          output=True, frames_per_buffer=4096)

            try:
                async with client.aio.live.connect(
                    model="gemini-2.5-flash-native-audio-preview-12-2025",
                    config=config,
                ) as session:

                    # Pre-compute one chunk of silence (all zeros)
                    # for the mic-gate keepalive path below. 16-bit
                    # mono PCM at 16 kHz, same shape as real mic
                    # chunks so the server can't tell the difference
                    # at the framing layer. Gemini's VAD ignores
                    # silence so it never triggers a response or
                    # feeds into the feedback loop.
                    silence_chunk = b"\x00" * (CHUNK * 2)
                    last_sent_ts = 0.0

                    async def send_audio():
                        nonlocal last_sent_ts
                        loop = asyncio.get_event_loop()
                        while True:
                            data = await loop.run_in_executor(None, mic.read, CHUNK, False)
                            now = time.monotonic()
                            # Mic gate — suppress live mic audio
                            # while Gemini or local TTS is
                            # speaking so the mic doesn't echo
                            # its own voice back into the Live
                            # session (feedback loop fix).
                            if _mic_should_be_muted():
                                # Keepalive: if the gate has been
                                # closed for >500 ms without any
                                # outbound frame, push a silent
                                # chunk so the server sees the
                                # stream is alive. Prevents
                                # "keepalive ping timeout" 1011s
                                # during long tool calls (browse
                                # inner Claude subprocess takes
                                # 15-25s and the mic gate would
                                # otherwise go fully idle).
                                if (now - last_sent_ts) > 0.5:
                                    await session.send_realtime_input(
                                        audio=types.Blob(
                                            data=silence_chunk,
                                            mime_type="audio/pcm;rate=16000",
                                        )
                                    )
                                    last_sent_ts = now
                                continue
                            await session.send_realtime_input(
                                audio=types.Blob(data=data, mime_type="audio/pcm;rate=16000")
                            )
                            last_sent_ts = now

                    # Phase 1E — rolling transcript buffer for the
                    # current user turn. Reset whenever the turn ends.
                    current_transcript = [""]

                    async def receive():
                        global _last_gemini_audio_ts
                        while True:
                            async for msg in session.receive():
                                if msg.data:
                                    # Mute Gemini's audio while a local
                                    # TTS bypass is in progress. Without
                                    # this, Gemini's response to the
                                    # "Done. Already spoken to user."
                                    # tool_response plays on top of the
                                    # local `say` reading the real
                                    # briefing — the user hears two
                                    # voices at once.
                                    if _ACTIVE_TTS is None or _ACTIVE_TTS.poll() is not None:
                                        spk.write(msg.data)
                                        # Mark the moment Gemini put
                                        # audio through the speaker so
                                        # the mic gate can suppress
                                        # outbound input during and
                                        # just after the utterance.
                                        _last_gemini_audio_ts = time.monotonic()

                                # Accumulate user-side transcription.
                                sc = getattr(msg, "server_content", None)
                                if sc is not None:
                                    inp = getattr(sc, "input_transcription", None)
                                    if inp is not None and getattr(inp, "text", None):
                                        current_transcript[0] += inp.text
                                    if getattr(sc, "turn_complete", False):
                                        # Turn done — reset buffer for the next one.
                                        current_transcript[0] = ""

                                if msg.tool_call:
                                    # Phase 1G — a new tool call
                                    # supersedes any in-flight long TTS
                                    # (e.g. user interrupts a briefing
                                    # mid-read with "never mind, search
                                    # for X").
                                    _kill_active_tts()
                                    transcript_snapshot = current_transcript[0]
                                    for fc in msg.tool_call.function_calls:
                                        logger.info(f"Tool call: {fc.name}({dict(fc.args)})")
                                        args = dict(fc.args) if fc.args else {}
                                        action = args.get("action", "")
                                        query = args.get("query", "")
                                        sess_choice = args.get("session", "")

                                        # Trigger-word gate with a
                                        # trust window. Block gated
                                        # actions ONLY when the
                                        # transcript has no trigger
                                        # AND we're outside the post-
                                        # successful-call trust
                                        # window. Fall-open on empty
                                        # transcript.
                                        action_lc = action.lower().strip()
                                        gated = action_lc in ACTION_GATE
                                        has_trigger = _transcript_has_trigger(transcript_snapshot)
                                        in_trust = _gate_in_trust_window()
                                        if (
                                            gated
                                            and transcript_snapshot
                                            and not has_trigger
                                            and not in_trust
                                        ):
                                            logger.warning(
                                                f"gate blocked action={action_lc!r} "
                                                f"transcript={transcript_snapshot!r}"
                                            )
                                            log_event(
                                                phase="gate.blocked",
                                                action=action_lc,
                                                transcript_len=len(transcript_snapshot),
                                            )
                                            trigger_names = ", ".join(sorted(_COMMAND_TRIGGERS))
                                            gemini_result = (
                                                f"No trigger word heard. "
                                                f"Say {trigger_names} first."
                                            )
                                            await session.send_tool_response(
                                                function_responses=[types.FunctionResponse(
                                                    name=fc.name,
                                                    id=fc.id,
                                                    response={"result": gemini_result},
                                                )]
                                            )
                                            continue

                                        # Gate passed — open/extend
                                        # the trust window so the next
                                        # minute of follow-up turns
                                        # doesn't need another trigger.
                                        if gated:
                                            _mark_gate_pass()
                                            if in_trust and not has_trigger:
                                                logger.info(
                                                    f"gate trust-window allowed {action_lc!r} "
                                                    f"(transcript={transcript_snapshot!r})"
                                                )

                                        result, is_long = await asyncio.to_thread(
                                            handle_tool, action, query, sess_choice
                                        )

                                        if is_long:
                                            # Fire [cached intro] → [body]
                                            # sequence. The cached intro plays
                                            # instantly so there's no dead air
                                            # after the user's question; the
                                            # body synthesizes during intro
                                            # playback for a seamless handoff.
                                            intro = _ACTION_INTRO.get(action_lc)
                                            intro_key = intro[0] if intro else None
                                            await asyncio.to_thread(
                                                tts_speak_long, result, intro_key,
                                            )
                                            # Wait for the full sequence to
                                            # finish BEFORE sending the
                                            # tool_response, so Gemini's own
                                            # wrap (if any) happens strictly
                                            # after playback — never overlapping.
                                            # The mic gate keeps the session
                                            # alive during the wait.
                                            await _wait_for_tts_done()
                                            # Strict silence instruction —
                                            # the user has heard a full, framed
                                            # spoken result and doesn't need a
                                            # trailing "anything else?" from
                                            # Gemini. Stay silent unless the
                                            # user speaks first.
                                            gemini_result = (
                                                "The user has already heard the "
                                                "complete spoken result. Do NOT "
                                                "reply. Do NOT repeat any content. "
                                                "Stay silent until the user "
                                                "speaks again."
                                            )
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

                                        if _sleep_requested:
                                            # Let Gemini speak "Goodbye.",
                                            # then return from the receive
                                            # loop. The main loop sees the
                                            # flag and breaks out entirely.
                                            await asyncio.sleep(1.8)
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

            if _sleep_requested:
                # Local wake-word listener. Gemini session is closed
                # (so no cost, no hot mic), we transcribe the mic
                # locally with faster-whisper and wait for the
                # wake phrase. On wake, `continue` falls through to
                # the top of the while loop, which opens a genuinely
                # fresh Gemini Live session (new WebSocket, new
                # conversation context, zero shared state with the
                # pre-sleep session). On Ctrl+C inside the listener,
                # break out for real.
                _sleep_requested = False
                woke = await asyncio.to_thread(_wait_for_wake_word, pa)
                if not woke:
                    break
                print("\n  Awake. New Gemini session starting.\n")
                logger.info("wake — starting fresh Gemini session")
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
