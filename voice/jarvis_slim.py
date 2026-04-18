#!/usr/bin/env python3
"""
Jarvis Slim — direct Gemini Live API, no pipecat.

Mic → Gemini Live websocket → speaker.
Gemini's own VAD, minimal prompt, slim tools.
Long tool results bypass Gemini and go straight to TTS.
"""

import asyncio
import faulthandler
import json
import os
import re
import subprocess
import sys
import threading
import time

# Native-crash diagnostics. When a segfault hits (faster-whisper,
# pyaudio stream close, Playwright daemon thread at shutdown), this
# dumps a Python-level stack trace to stderr before the process dies —
# otherwise we just see "zsh: segmentation fault" with no context.
faulthandler.enable()

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

SYSTEM_PROMPT = "Answer from your own knowledge. No follow-ups. Use do tool for actions."

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
                    description=(
                        "One of: search, documents, browse, calendar, email, "
                        "reminders, briefing, window, code, github, sleep. "
                        "search=web, documents=user's files, browse=open site, "
                        "sleep='sleep'/'goodbye'/'bye'."
                    ),
                ),
                "query": types.Schema(
                    type=types.Type.STRING,
                    description=(
                        "User's words. "
                        "For documents: keywords only, not a sentence. "
                        f"For code: project name (one of: {', '.join(PROJECTS.keys())}) "
                        "or 'list' for sessions. "
                        "For window: a verb-led command "
                        "('move chrome left', 'maximize iterm', 'close finder', 'list')."
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
    """
    Rank the worktree's markdown index files by how many query
    keywords they contain, then return the top hits with their
    nearest heading + best-matching line.

    The worktree is an index — each .md is prose describing groups
    of files on OneDrive (a year of coursework, a project's code
    layout, etc.). Line-level exact-match misses badly on compound
    queries like "PID automation control" because no single line
    has every word. File-level scoring with ANY-word matching finds
    the right subject area even when keywords are scattered.
    """
    words = [w.lower() for w in query.split() if len(w) > 2]
    if not words:
        return "No results."

    scored: list[tuple[int, int, str, str, str]] = []  # (file_hits, line_hits, rel, heading, snippet)
    files_scanned = 0
    with timed("documents.walk_scan", query_len=len(query)):
        for dirpath, _, filenames in os.walk(WORKTREE_ROOT):
            for fname in filenames:
                if not fname.endswith(".md"):
                    continue
                fpath = os.path.join(dirpath, fname)
                files_scanned += 1
                try:
                    with open(fpath) as f:
                        content = f.read()
                except Exception:
                    continue
                lc = content.lower()
                file_hits = sum(1 for w in words if w in lc)
                if file_hits == 0:
                    continue

                # Walk for the heading nearest the best-matching line.
                cur_heading = ""
                best_heading = ""
                best_line = ""
                best_line_hits = 0
                for line in content.split("\n"):
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        cur_heading = stripped.lstrip("#").strip()
                        continue
                    if not stripped:
                        continue
                    ll = stripped.lower()
                    lh = sum(1 for w in words if w in ll)
                    if lh > best_line_hits:
                        best_line_hits = lh
                        best_line = stripped
                        best_heading = cur_heading

                rel = os.path.relpath(fpath, WORKTREE_ROOT)
                scored.append((file_hits, best_line_hits, rel, best_heading, best_line))

    # Sort: distinct keywords in file first, then best-line score, then path.
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    log_event(phase="documents.scan_summary",
              files_scanned=files_scanned, hits=len(scored))
    if not scored:
        return "No results."

    # Top 3. Each line: `path > Heading — snippet`. Keeps things
    # short enough for Gemini to relay in 1-3 spoken sentences.
    lines = [f"{len(scored)} files matched '{query}':"]
    for file_hits, _line_hits, rel, heading, snippet in scored[:3]:
        head_str = f" > {heading}" if heading else ""
        lines.append(
            f"- {rel}{head_str} ({file_hits}/{len(words)} keywords): "
            f"{snippet[:180]}"
        )
    return "\n".join(lines)


BROWSE_TIMEOUT_SEC = 45  # hard cap on the inner Claude nav agent


def _run_nav_claude(destination, goal):
    """Run Claude Code for browser navigation. Hard-killed at BROWSE_TIMEOUT_SEC."""
    nav_script = os.path.abspath(os.path.join(os.path.dirname(__file__), "nav.py"))
    venv_python = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "venv", "bin", "python3"))

    prompt = (
        f"Navigate the browser to: {destination}\nGoal: {goal}\n\n"
        f"Use: {venv_python} {nav_script} <cmd>\n"
        f"Commands: state, goto <url>, click \"text\", type \"field\" \"value\", press Enter, scroll down\n"
        "Prefer a direct URL. Budget: max 4 commands total. Do not loop "
        "between sites. If the first page doesn't have the answer, stop "
        "and report 'Not found on that page'.\n"
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
    proc = None
    try:
        spawn_start = time.perf_counter()
        proc = subprocess.Popen(cmd, cwd=os.path.dirname(__file__),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        log_event(phase="browse.claude_subprocess_spawn",
                  duration_ms=round((time.perf_counter() - spawn_start) * 1000, 2))

        result_text = ""
        first_token_logged = False
        timed_out = False
        start = time.time()
        while proc.poll() is None and time.time() - start < BROWSE_TIMEOUT_SEC:
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
        if proc.poll() is None:
            timed_out = True
            proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        log_event(phase="browse.claude_total",
                  duration_ms=round((time.time() - start) * 1000, 2),
                  timed_out=timed_out)
        if timed_out and not result_text:
            return "Browsing took too long. Tell the user the page is taking long and ask if they want to keep trying."
        return result_text or "Navigation done."
    except Exception as e:
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
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

        elif action == "search":
            # Fast path — open Google for the query, no inner Claude.
            # Gemini answers the user from its own knowledge; the
            # browser page is there if the user wants to read more.
            # This avoids the Google ↔ Wikipedia navigation loop and
            # the Claude subprocess spawn latency.
            import urllib.parse
            try:
                with timed("search.ensure_browser"):
                    from browser import ensure_browser, send_command
                    ensure_browser()
                q = (query or "").strip()
                url = (
                    f"https://www.google.com/search?q={urllib.parse.quote(q)}"
                    if q else "https://www.google.com"
                )
                with timed("search.goto"):
                    send_command({"action": "goto", "url": url})
            except Exception as e:
                return f"Search error: {str(e)[:100]}", False
            return (
                f"Google opened for '{q[:120]}'. Answer the user's "
                "question now from your own knowledge in 1-2 natural "
                "spoken sentences. Do not say you searched."
            ), False

        elif action in ("browse", "navigate"):
            # Slow path for real site navigation (figma, email,
            # shopify, facebook ads, a university page, etc). Inner
            # Claude drives Playwright with a 45s hard timeout so it
            # cannot loop forever.
            try:
                with timed("browse.ensure_browser"):
                    from browser import ensure_browser
                    ensure_browser()
            except Exception as e:
                return f"Browser error: {str(e)[:100]}", False

            result = _run_nav_claude(query or "google", query)
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
            if result == "No results.":
                return (
                    f"No matches in the worktree for '{query}'. "
                    "Tell the user nothing matched and suggest "
                    "they try different keywords."
                ), False
            # Frame so Gemini relays it as natural speech instead
            # of reading paths/scores verbatim. Same pattern as browse.
            framed = (
                f"Files in the worktree matching '{query}':\n\n"
                f"{result}\n\n"
                "Relay to the user in 1-3 natural spoken sentences: "
                "name the best-matching file and the heading it was "
                "found under (so the user knows where to look). "
                "Mention 1-2 runners-up if they're also relevant. "
                "Do not read out raw paths or scores."
            )
            return framed, False

        elif action in ("code", "connect"):
            project = (query or "").lower().strip()
            # "list sessions" / "list" / "sessions" — report active
            # Claude Code sessions without starting a handoff.
            if project in ("list", "list sessions", "sessions"):
                try:
                    from claude_mode import get_all_session_statuses
                    active = get_all_session_statuses()
                except Exception:
                    active = {}
                if active:
                    return (
                        "Active Claude sessions: "
                        + ", ".join(f"{p} ({s})" for p, s in active.items())
                    ), False
                return "No active Claude sessions.", False
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
            # is_long=True → local TTS speaks the line via Chirp3-HD
            # directly; Gemini is told "already spoken" and stays
            # quiet. Gemini was silently dropping this tool_response
            # ~half the time, so the user never heard the handoff
            # confirmation.
            return f"Switching to Claude coding mode for {project}. Goodbye for now.", True

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
    _MIC_GATE_TAIL_S = 1.0
_last_gemini_audio_ts: float = 0.0

# Hard speaking flag — set True the moment Gemini starts emitting a
# response (either audio chunks arriving OR we just sent a tool
# response and are waiting for the reply), cleared on turn_complete.
# This covers the gap where the timestamp-tail alone fails: between
# intra-sentence pauses (Gemini waits 500ms+ between phrases) and
# between send_tool_response and Gemini's first audio chunk. Without
# this flag the mic briefly opens and picks up the speaker output,
# which the model then treats as a new user question — observed in
# the wild as 3 successive self-triggered searches from one user
# prompt. The tail stays too, as the speaker's own buffer drain
# outlives turn_complete by ~100-300 ms.
_gemini_speaking: bool = False

# Tool-in-flight gate.
#
# Gemini Live function calling is documented as sequential: "execution
# pauses until the results of each function call are available." In
# practice, streaming real mic audio while a tool is running triggers
# server-side 1011 closures (see google/adk-python#3918 and the
# Google AI dev forum threads on random 1011 during tool_response).
#
# This flag gates the mic the same way _mic_should_be_muted does when
# Gemini is speaking. Set True just before dispatching handle_tool,
# cleared just after send_tool_response succeeds. The existing silence
# keepalive path still fires in send_audio(), which keeps the session
# alive without feeding the server real audio it isn't expecting.
_tool_in_flight: bool = False

# Graceful session rotation.
#
# Gemini Live audio-only sessions are capped at 15 minutes. The server
# sends a GoAway message with `time_left` before force-closing; if the
# client ignores it, the server terminates with a 1008 policy violation.
#
# Strategy: rotate proactively once the session passes
# _SESSION_ROTATE_AFTER_S (80% of the cap). Rotation only fires on a
# `turn_complete` boundary — Gemini has finished speaking and the user
# hasn't started yet, so the cut is silent from the user's side. The
# new session is opened with the last `session_resumption_update.new_handle`
# so state persists across the boundary.
_SESSION_MAX_S: float = 15 * 60     # Gemini Live audio-only cap
_SESSION_ROTATE_AFTER_S: float = 12 * 60  # 80% — leaves headroom before GoAway
_session_started_at: float = 0.0
_session_handle: str | None = None
_rotate_requested: bool = False

# True only while a Gemini Live session is open. The notification
# watcher uses this to suppress playback during Claude mode or
# sleep — otherwise a queued notification would fire on top of
# Claude's own TTS, or during the local wake-word listener.
_gemini_session_active: bool = False


def _is_transient_close(e: Exception) -> bool:
    """
    True if `e` represents a recoverable Live session close that
    should trigger a silent reconnect rather than a process exit.

    Covers:
      - APIError 1011 (random server internal)
      - APIError 1008 (policy violation from missed GoAway)
      - websockets.exceptions.ConnectionClosedError / ConnectionClosedOK
      - any `ConnectionClosed*` class by name (version-agnostic)
    """
    msg = str(e)
    if "1011" in msg or "1008" in msg:
        return True
    if "ConnectionClosed" in type(e).__name__:
        return True
    return False


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
    # A tool is running — documented pattern is "execution pauses."
    if _tool_in_flight:
        return True
    # Gemini currently has the floor — speaking or about to.
    if _gemini_speaking:
        return True
    # Tail after Gemini's last audio chunk — covers speaker buffer
    # drain and brief intra-turn pauses that sneak past the flag.
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
    # search has no ack — it's a fast direct goto and Gemini speaks
    # the answer from its own knowledge. Any local ack would collide
    # with Gemini's reply. Browse keeps its ack because the inner
    # Claude subprocess can take several seconds.
    "browse":   "On it.",
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
# Wake phrase — only used by the sleep-mode listener
# =============================================================================
#
# When jarvis is sleeping, a local faster-whisper loop transcribes mic
# audio and watches for any of these phrases to reopen the Gemini Live
# session. Multi-word phrases ("wake up") transcribe far more reliably
# under real-world mic conditions than single proper nouns — Whisper
# small routinely mangles single-word wake words.
#
# Override via NEXUS_WAKE_PHRASES="phrase,another" in .env. No active-
# mode gate: the C21 action schema + 3.1 model route tool calls at
# ~100% on content alone, so there's nothing for a transcript-based
# gate to add.

def _parse_trigger_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return tuple(t.strip().lower() for t in raw.split(",") if t.strip())


_WAKE_TRIGGERS: tuple[str, ...] = _parse_trigger_env(
    "NEXUS_WAKE_PHRASES", ("wake up",)
)


def _transcript_has_trigger(
    transcript: str,
    tokens: tuple[str, ...] = _WAKE_TRIGGERS,
) -> bool:
    """Case-insensitive word-boundary match for any of `tokens`."""
    if not transcript or not tokens:
        return False
    pattern = r"\b(?:" + "|".join(re.escape(t) for t in tokens) + r")\b"
    return re.search(pattern, transcript, re.IGNORECASE) is not None


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
    global _MAIN_LOOP, _sleep_requested, _last_gemini_audio_ts
    _MAIN_LOOP = asyncio.get_running_loop()  # used by _maybe_sync's background scheduler

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    print_budget()

    # Browser is launched lazily on the first browse/search call —
    # Chrome should not appear until the user actually asks for it.

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

    # Pre-warm faster-whisper so the sleep→wake path doesn't do a
    # cold native-model load right when pyaudio is tearing down. The
    # cold load was a likely contributor to segfaults on sleep.
    async def _prewarm_whisper():
        try:
            from audio import get_whisper
            await asyncio.to_thread(get_whisper)
            logger.info("Whisper pre-warmed")
        except Exception as e:
            logger.warning(f"Whisper pre-warm failed: {e}")
    asyncio.create_task(_prewarm_whisper())

    # Pre-synthesize claude-mode greetings/acks with the current TTS
    # voice. Without this, play_greeting falls back to macOS `say`
    # (the "Darth Vader" voice) when the voice-tagged cache is missing.
    async def _prewarm_ack_cache():
        try:
            from audio import init_ack_cache
            await asyncio.to_thread(init_ack_cache)
        except Exception as e:
            logger.warning(f"Ack cache pre-warm failed: {e}")
    asyncio.create_task(_prewarm_ack_cache())

    # Background notification watcher — polls queued Claude-mode
    # completion notifications and plays them via local TTS as soon
    # as the audio channel is free (no Gemini speech, no tool in
    # flight, no local TTS already playing). Previously the
    # notifications only fired on turn_complete, which meant if the
    # user never spoke to Gemini after a Claude run finished, the
    # notification was queued and forgotten. This loop runs for the
    # lifetime of the jarvis process.
    async def _notification_watcher():
        while True:
            await asyncio.sleep(1.0)
            if not _gemini_session_active:
                # Claude mode or sleep-mode listener owns the audio —
                # hold the notification until we're back in Jarvis.
                continue
            try:
                from claude_mode import check_notifications
                notifs = check_notifications()
            except Exception as e:
                logger.warning(f"notification watcher poll failed: {e}")
                continue
            if not notifs:
                continue
            # Wait only for the two cases that would actually collide
            # audibly: Gemini currently speaking, or another local TTS
            # already playing. Tool-in-flight and the mic-gate tail
            # are silent from the user's side — notifications can
            # overlap those without stepping on any speech.
            for _ in range(60):
                if not _gemini_session_active:
                    break  # bail if we transitioned out mid-wait
                tts_busy = _ACTIVE_TTS is not None and _ACTIVE_TTS.poll() is None
                if not _gemini_speaking and not tts_busy:
                    break
                await asyncio.sleep(0.5)
            if not _gemini_session_active:
                # Re-queue and retry when Jarvis is active again.
                from claude_mode import _completed_notifications
                _completed_notifications[:0] = notifs
                continue
            for proj, summary in notifs:
                text = f"Claude finished on {proj}. {summary[:400]}"
                logger.info(f"Delivering Claude notification: {proj}")
                try:
                    await asyncio.to_thread(tts_speak_long, text, None)
                    await _wait_for_tts_done()
                except Exception as e:
                    logger.warning(f"notification playback failed: {e}")
    asyncio.create_task(_notification_watcher())

    config = types.LiveConnectConfig(
        system_instruction=SYSTEM_PROMPT,
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Aoede")
            )
        ),
        tools=[types.Tool(function_declarations=TOOL_DECLARATIONS)],
        # Session resumption — server sends handle tokens we can pass
        # on reconnect to continue the same logical conversation. Used
        # by the graceful rotation path so the user doesn't notice
        # when we proactively rotate before the 15-min session cap.
        session_resumption=types.SessionResumptionConfig(),
        # NB: ProactivityConfig(proactive_audio=True) would be ideal
        # for the "stay silent" behavior, but the Gemini Developer
        # API rejects that setup field ("Unknown name 'proactivity'
        # at 'setup': Cannot find field") even though the SDK types
        # accept it — it's Vertex-only for now. The system prompt
        # carries the full "no follow-ups" instruction instead.
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
            _last_gemini_audio_ts = 0.0  # mic gate starts open
            global _tool_in_flight, _session_started_at, _rotate_requested, _session_handle, _gemini_speaking, _gemini_session_active
            _tool_in_flight = False  # no stale tool gate on new session
            _session_started_at = time.monotonic()
            _rotate_requested = False
            _gemini_speaking = False  # no stale speaking flag across sessions
            _gemini_session_active = True  # watcher may deliver notifications
            _kill_active_tts()  # no leftover say/afplay from prior run

            mic = pa.open(format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE,
                          input=True, frames_per_buffer=CHUNK)
            spk = pa.open(format=pyaudio.paInt16, channels=1, rate=RECV_RATE,
                          output=True, frames_per_buffer=4096)

            # Set by the close-detection block below (1011, 1008,
            # GoAway, or proactive rotation). Triggers a silent
            # reopen via `continue` in the outer while loop.
            reconnect_requested = False

            # Per-session config: reuse the last resumption handle if
            # we have one (transparent reopen after rotation/GoAway).
            # First launch has handle=None → server treats it as a
            # fresh session.
            if _session_handle:
                session_config = config.model_copy(update={
                    "session_resumption": types.SessionResumptionConfig(
                        handle=_session_handle,
                    ),
                })
            else:
                session_config = config

            try:
                async with client.aio.live.connect(
                    # gemini-3.1-flash-live-preview is Google's
                    # documented replacement for the deprecated
                    # 2.5-native-audio-preview. Same audio pricing,
                    # proper function-calling support, known-stable
                    # for production Live API usage.
                    model="gemini-3.1-flash-live-preview",
                    config=session_config,
                ) as session:

                    async def send_audio():
                        # Mic forwarding only. When the gate is
                        # closed (Gemini speaking, local TTS, or a
                        # tool in flight) we simply don't send —
                        # the SDK/server handles idle periods on
                        # their own. The previous silence-keepalive
                        # was a home-grown "fix 1011" that wasn't
                        # part of the documented protocol and
                        # appears to have been contributing to the
                        # same class of failures.
                        loop = asyncio.get_event_loop()
                        while True:
                            data = await loop.run_in_executor(None, mic.read, CHUNK, False)
                            if _mic_should_be_muted():
                                continue
                            await session.send_realtime_input(
                                audio=types.Blob(data=data, mime_type="audio/pcm;rate=16000")
                            )

                    async def receive():
                        global _last_gemini_audio_ts, _session_handle, _rotate_requested, _gemini_speaking
                        while True:
                            async for msg in session.receive():
                                if msg.data:
                                    # Gemini has the floor — hard-mute
                                    # the outbound mic until turn_complete
                                    # so the speaker's own output can't
                                    # feed back as a new user query.
                                    _gemini_speaking = True
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
                                        # the mic gate's tail can keep
                                        # it muted after the last chunk.
                                        _last_gemini_audio_ts = time.monotonic()

                                # Documented barge-in pattern: server
                                # sends server_content.interrupted=True
                                # when the user speaks mid-reply. We
                                # stop treating Gemini as "on the floor"
                                # immediately so the mic opens for the
                                # user's continued speech, and we drop
                                # any pending Gemini audio we haven't
                                # played yet (anything already in the
                                # OS output buffer will drain naturally
                                # — pyaudio doesn't expose a flush).
                                sc_early = getattr(msg, "server_content", None)
                                if sc_early is not None and getattr(sc_early, "interrupted", False):
                                    if _gemini_speaking:
                                        logger.info("server: interrupted — releasing floor")
                                    _gemini_speaking = False
                                    _last_gemini_audio_ts = 0.0

                                # Graceful rotation — capture resumption
                                # handles so we can reopen a new session
                                # with preserved state, and honor the
                                # server's GoAway warning.
                                sru = getattr(msg, "session_resumption_update", None)
                                if sru is not None and getattr(sru, "resumable", False):
                                    h = getattr(sru, "new_handle", None)
                                    if h:
                                        _session_handle = h

                                ga = getattr(msg, "go_away", None)
                                if ga is not None:
                                    tl = getattr(ga, "time_left", "?")
                                    logger.info(
                                        f"Server GoAway (time_left={tl}) — rotating"
                                    )
                                    _rotate_requested = True

                                # Proactive rotation at turn boundaries
                                # once we're past the rotate threshold.
                                # Waiting for turn_complete keeps the
                                # cut invisible — Gemini has finished
                                # speaking, user hasn't started yet.
                                sc = getattr(msg, "server_content", None)
                                if sc is not None and getattr(sc, "turn_complete", False):
                                    # Gemini done — release the hard
                                    # speaking flag and bump the tail ts
                                    # so the tail timer starts NOW (from
                                    # turn_complete, not from the last
                                    # audio chunk which may be ~hundreds
                                    # of ms earlier). Covers the speaker
                                    # buffer drain cleanly.
                                    _gemini_speaking = False
                                    _last_gemini_audio_ts = time.monotonic()
                                    # (Notifications are delivered by
                                    # a standalone background watcher
                                    # now — not tied to turn_complete —
                                    # so they still fire even if the
                                    # user doesn't interact with Gemini.)

                                    age = time.monotonic() - _session_started_at
                                    if age > _SESSION_ROTATE_AFTER_S:
                                        logger.info(
                                            f"Proactive rotation at {age:.0f}s "
                                            f"(> {_SESSION_ROTATE_AFTER_S}s threshold)"
                                        )
                                        _rotate_requested = True
                                    if _rotate_requested:
                                        return

                                if msg.tool_call:
                                    # A new tool call supersedes any
                                    # in-flight long TTS (user interrupting
                                    # a briefing mid-read with "never
                                    # mind, search for X").
                                    _kill_active_tts()
                                    for fc in msg.tool_call.function_calls:
                                        logger.info(f"Tool call: {fc.name}({dict(fc.args)})")
                                        args = dict(fc.args) if fc.args else {}
                                        action = args.get("action", "")
                                        query = args.get("query", "")
                                        sess_choice = args.get("session", "")
                                        action_lc = action.lower().strip()

                                        # Pause outbound mic for the full
                                        # tool-dispatch-to-response window.
                                        # Gemini Live expects the stream to
                                        # go quiet here; flooding audio during
                                        # a tool call triggers 1011 server
                                        # closures. Cleared in a finally at the
                                        # end of this tool_call branch so a
                                        # handle_tool raise can't wedge the mic.
                                        global _tool_in_flight
                                        _tool_in_flight = True
                                        try:
                                            result, is_long = await asyncio.to_thread(
                                                handle_tool, action, query, sess_choice
                                            )
                                        except Exception:
                                            _tool_in_flight = False
                                            raise

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
                                        # Tool cycle fully done — open mic.
                                        _tool_in_flight = False

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
                        # Cancel the survivor first — tighter shutdown
                        # order, fewer spurious socket writes.
                        for t in pending:
                            t.cancel()
                        for t in pending:
                            try:
                                await t
                            except (asyncio.CancelledError, Exception):
                                pass
                        # Observe exceptions on the done task(s). If
                        # we skip this, Python logs "Task exception
                        # was never retrieved" at GC time and the
                        # real error (e.g. 1011) is invisible.
                        for t in done:
                            try:
                                await t
                            except (asyncio.CancelledError, KeyboardInterrupt):
                                raise
                            except Exception as e:
                                if _is_transient_close(e):
                                    logger.warning(
                                        f"Live session closed ({type(e).__name__}): "
                                        f"{str(e)[:160]} — reconnecting"
                                    )
                                    reconnect_requested = True
                                else:
                                    logger.error(
                                        f"session task exception ({type(e).__name__}): "
                                        f"{str(e)[:200]}"
                                    )
                    except (KeyboardInterrupt, asyncio.CancelledError):
                        send_task.cancel()
                        recv_task.cancel()

                # Clean rotation — receive() returned without exception
                # because _rotate_requested was set on a turn boundary
                # or GoAway. Same downstream handling as a transient.
                if _rotate_requested:
                    reconnect_requested = True
            except Exception as e:
                # `async with client.aio.live.connect(...)` itself can
                # raise on the way in or on clean exit if the socket
                # closed mid-handshake.
                if _is_transient_close(e):
                    logger.warning(
                        f"Live connect closed ({type(e).__name__}): "
                        f"{str(e)[:160]} — reconnecting"
                    )
                    reconnect_requested = True
                else:
                    raise
            finally:
                # Kill any local TTS FIRST — stops audio subprocesses
                # holding native resources before we close pyaudio
                # streams. Reduces the native-thread shutdown races
                # that were producing segfaults on the sleep path.
                _kill_active_tts()
                try:
                    mic.close()
                except Exception as e:
                    logger.warning(f"mic.close: {e}")
                try:
                    spk.close()
                except Exception as e:
                    logger.warning(f"spk.close: {e}")

            # Mark Gemini session inactive now that its WebSocket
            # and mic/spk streams are closed. The notification watcher
            # suppresses playback whenever this flag is False (Claude
            # mode or sleep) to avoid talking over Claude's TTS or
            # cutting the wake-word listener.
            _gemini_session_active = False

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
                # Shut Playwright down before the wake listener spins
                # up faster-whisper's native model load. Two native
                # subsystems tearing up/down concurrently is the most
                # likely cause of the segfaults on the sleep path.
                try:
                    from browser import is_running, stop_browser
                    if is_running():
                        await asyncio.to_thread(stop_browser)
                except Exception as e:
                    logger.warning(f"browser stop on sleep: {e}")
                # Drop resumption handle — waking is a genuine fresh
                # conversation, not a continuation.
                _session_handle = None
                woke = await asyncio.to_thread(_wait_for_wake_word, pa)
                if not woke:
                    break
                print("\n  Awake. New Gemini session starting.\n")
                logger.info("wake — starting fresh Gemini session")
                continue

            if reconnect_requested:
                # Silent reopen path — covers both server-initiated
                # closures (1011/1008/ConnectionClosed) and our own
                # proactive rotation before the 15-min session cap.
                # The last session_resumption_update.new_handle is
                # passed back to the server on reconnect so the
                # conversation continues without visible interruption.
                await asyncio.sleep(1.0)  # short backoff for sick backends
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
