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
        "exposes a stable URL for the section you need. "
        "Final response under 150 chars. If login needed say 'Login required'."
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
            # Stage a real shutdown: the receive loop reads this flag
            # after sending the tool_response, lets the goodbye line
            # play, then breaks out of the main loop cleanly — same
            # pattern as the code handoff. Without this, sleep just
            # printed "Going to sleep." and the session stayed hot,
            # Gemini then repeating the goodbye on ambient noise.
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
            return result[:SHORT_RESULT_LIMIT], False

        elif action == "window":
            return _handle_window(query), False

        elif action in ("calendar", "email", "reminders"):
            data = _maybe_sync(action)
            if not data:
                return f"No {action} data.", False
            # Long result — TTS speaks it, Gemini gets short confirmation
            return data[:3000], True

        elif action == "briefing":
            data = _maybe_sync("all")
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

TRIGGER_TOKENS: set[str] = {"nexus", "jarvis"}

# STT-tolerant substring forms. Gemini Live's input transcription
# occasionally mangles the opening trigger word — seen in the wild
# as "I request Hey, can you browse..." for what was clearly
# "hey jarvis, can you browse...". "jarv" and "nexu" are rare
# enough in English that false positives are effectively zero (no
# common word starts with either), so expanding the substring set
# costs nothing and catches the partial-transcription cases.
_TRIGGER_FUZZY: tuple[str, ...] = (
    "jarvis", "jarv", "nexus", "nexu",
)

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


def _transcript_has_trigger(transcript: str) -> bool:
    """Substring check for any configured trigger token, case-insensitive."""
    if not transcript:
        return False
    t = transcript.lower()
    return any(tok in t for tok in _TRIGGER_FUZZY)


def _gate_in_trust_window() -> bool:
    """True if we're within the post-successful-call trust window."""
    if _last_gate_pass_ts <= 0.0:
        return False
    return (time.monotonic() - _last_gate_pass_ts) < _GATE_TRUST_WINDOW_S


def _mark_gate_pass() -> None:
    """Called after an allowed gated tool call — opens the trust window."""
    global _last_gate_pass_ts
    _last_gate_pass_ts = time.monotonic()


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


def tts_speak_long(text: str) -> None:
    """
    Fire-and-forget speech of `text` via macOS `say`. Returns
    immediately — does NOT wait for `say` to finish playing.

    This is load-bearing for Gemini Live flow control: the caller
    MUST be able to return "Done. Already spoken to user." to Gemini
    within ~100ms of dispatching the tool, otherwise the mic-side
    audio backlog overflows and Gemini drops the websocket with
    1011 (happened in the live-run regression on 2026-04-15).
    """
    global _ACTIVE_TTS
    if not text:
        return
    # Kill any in-flight briefing/calendar playback — a new tool
    # result supersedes the old speech.
    _kill_active_tts()
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
        # Push the text into stdin and close the pipe. `say` buffers
        # the input internally and plays it from there — we do NOT
        # call proc.communicate() because that would block until
        # playback finishes. The kernel pipe buffer (~64 KB) is far
        # larger than any realistic briefing (callers cap at 3000
        # chars), so the write returns immediately.
        assert proc.stdin is not None
        proc.stdin.write(text.encode("utf-8"))
        proc.stdin.close()
    except Exception as e:
        logger.error(f"tts_speak_long failed: {e}")
        _ACTIVE_TTS = None


# =============================================================================
# Main loop
# =============================================================================

async def main():
    global _MAIN_LOOP
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
            _handoff["project"] = None
            _handoff["session"] = None
            _handoff["path"] = None
            # Fresh session starts with a closed trust window —
            # the first gated call of the day must carry a real
            # trigger. Prevents ambient-trigger from a previous run.
            global _last_gate_pass_ts
            _last_gate_pass_ts = 0.0

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

                    # Phase 1E — rolling transcript buffer for the
                    # current user turn. Reset whenever the turn ends.
                    current_transcript = [""]

                    async def receive():
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
                                            gemini_result = (
                                                "No trigger word heard. "
                                                "Say jarvis or nexus first."
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
                logger.info("Sleep requested — exiting Nexus")
                break

            break
    finally:
        pa.terminate()
        print("\n  Jarvis stopped.\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  Jarvis stopped.\n")
