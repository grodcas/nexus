#!/usr/bin/env python3
"""
Claude coding mode — direct voice-to-Claude Code with Claudia API assist.

States:
    IDLE              — waiting for a trigger
    RECORDING_CLAUDE  — buffering speech for Claude Code
    RECORDING_CLAUDIA — buffering speech for Claudia API
    WAITING_CLAUDE    — Claude Code subprocess running
    WAITING_CLAUDIA   — Claudia API call in flight

Keywords (days of the week — STT-robust, see audio.py header):
    "friday"          — toggle Claude Code (open recording → submit)
    "wednesday"       — toggle Claudia (open recording → submit)
    "stop friday"     — interrupt Claude's TTS playback
    "stop wednesday"  — interrupt Claudia's TTS playback
    "jarvis"          — exit to Gemini mode (session stays alive)
    "close session"   — kill session, exit to Gemini

Single-utterance form also works: "friday do X friday" enters and
exits the recording in one breath — detected by counting trigger
occurrences in the transcript.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from enum import Enum

from anthropic import Anthropic
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audio import (
    detect_keyword,
    has_keyword,
    count_keyword,
    strip_keyword,
    transcribe,
    speak,
    play_greeting,
    play_ack,
    record_speech,
)
from session_manager import save_session, get_session_id, load_projects


# =============================================================================
# State machine
# =============================================================================

class State(Enum):
    IDLE = "idle"
    RECORDING_CLAUDE = "recording_claude"
    RECORDING_CLAUDIA = "recording_claudia"
    WAITING_CLAUDE = "waiting_claude"
    WAITING_CLAUDIA = "waiting_claudia"


# =============================================================================
# Claude Code session — manages subprocess
# =============================================================================

class ClaudeCodeSession:
    """Runs and monitors a Claude Code subprocess."""

    def __init__(self, project: str = ""):
        self.project = project
        self.proc = None
        self.status = "idle"  # idle | working | done | error
        self.result_text = ""
        self.session_id = None
        self.started_at = 0
        self._monitor_task = None
        self._events = []
        self._notify_on_complete = False  # True when running in background (not in claude_mode)

    @property
    def is_busy(self):
        return self.status == "working"

    def kill(self):
        """Kill subprocess and cancel monitor."""
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

    async def run(self, instruction: str, repo_path: str, session_id: str | None = None):
        """Start a Claude Code task. Previous task is killed first."""
        self.kill()

        if "concise" not in instruction.lower() and "short" not in instruction.lower():
            instruction += "\n\nKeep your final summary to 3-5 sentences."

        self.status = "working"
        self._events = []
        self.result_text = ""
        self.started_at = time.time()

        cmd = [
            "claude", "--print", "--verbose",
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
        ]

        if session_id:
            cmd.extend(["--resume", session_id])

        cmd.extend(["-p", instruction])

        self.proc = subprocess.Popen(
            cmd, cwd=repo_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self._monitor_task = asyncio.create_task(self._monitor())
        logger.info(f"Claude Code started: {instruction[:100]}")

    async def _monitor(self):
        """Read stream-json output until process exits."""
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

            # Drain remaining
            if self.proc:
                remaining = await loop.run_in_executor(None, self.proc.stdout.read)
                if remaining:
                    for raw in remaining.split(b"\n"):
                        if raw.strip():
                            try:
                                event = json.loads(raw.decode("utf-8", errors="replace"))
                                self._events.append(event)
                                self._process_event(event)
                            except json.JSONDecodeError:
                                pass

            if self.status == "working":
                self.status = "done"

            # Notify if running in background (user is in Jarvis mode)
            if self._notify_on_complete and self.status == "done" and self.project:
                summary = self.result_text[:150] if self.result_text else "Task completed."
                _notify_completion(self.project, summary)

        except asyncio.CancelledError:
            return
        except Exception as e:
            self.status = "error"
            logger.error(f"Claude monitor error: {e}")
            if self._notify_on_complete and self.project:
                _notify_completion(self.project, f"Error: task failed.")

    def _process_event(self, event):
        etype = event.get("type", "")

        if etype == "system" and event.get("subtype") == "init":
            sid = event.get("session_id")
            if sid:
                self.session_id = sid
                logger.info(f"Claude session ID: {sid}")

        elif etype == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    if text.strip():
                        self.result_text = text

        elif etype == "result":
            self.status = "done"
            result = event.get("result", "")
            if result.strip():
                self.result_text = result
            duration = event.get("duration_ms", 0)
            cost = event.get("total_cost_usd", 0)
            logger.info(f"Claude done: {duration / 1000:.1f}s, ${cost:.4f}")

    def get_progress(self) -> str:
        """Human-readable progress string."""
        if self.status == "working" and self.proc and self.proc.poll() is not None:
            self.status = "error"

        if self.status == "idle":
            return "No task running."

        elapsed = int(time.time() - self.started_at)
        ops = sum(
            1 for e in self._events
            if e.get("type") == "assistant"
            and any(b.get("type") == "tool_use"
                    for b in e.get("message", {}).get("content", []))
        )

        if self.status == "working":
            return f"Working... {elapsed}s, {ops} operations so far."
        elif self.status == "done":
            return f"Done in {elapsed}s, {ops} operations."
        return f"Error after {elapsed}s."


# =============================================================================
# Claudia — lightweight Claude API for explaining Claude Code output
# =============================================================================

class Claudia:
    """Haiku-based assistant for understanding Claude Code output."""

    def __init__(self):
        self.client = Anthropic()

    def ask(self, question: str, context: str) -> str:
        """Ask Claudia about Claude Code's output."""
        response = self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=(
                "You are Claudia, a concise assistant that explains coding work. "
                "The user is working with a coding agent and needs help understanding "
                "its output. Be brief — your response will be spoken aloud via TTS. "
                "No markdown, no formatting, no bullet points. Natural speech only."
            ),
            messages=[{
                "role": "user",
                "content": (
                    f"Here is the coding agent's output:\n\n"
                    f"{context[:3000]}\n\n"
                    f"My question: {question}"
                ),
            }],
        )
        return response.content[0].text


# =============================================================================
# Active sessions — persist across mode switches
# =============================================================================

_active_sessions: dict[str, ClaudeCodeSession] = {}


def get_active_session(project: str) -> ClaudeCodeSession | None:
    """Get active Claude Code session for a project (if any)."""
    return _active_sessions.get(project)


def get_all_session_statuses() -> dict[str, str]:
    """Get status of all active sessions. For Jarvis to report."""
    return {p: s.status for p, s in _active_sessions.items() if s.status != "idle"}


def kill_session(project: str):
    """Kill and remove an active session."""
    session = _active_sessions.pop(project, None)
    if session:
        session.kill()
        logger.info(f"Killed session for {project}")


# =============================================================================
# Cross-mode notifications — Claude Code task completions
# =============================================================================

_completed_notifications: list[tuple[str, str]] = []  # (project, summary)


def check_notifications() -> list[tuple[str, str]]:
    """Check and clear completed task notifications. Thread-safe in asyncio."""
    global _completed_notifications
    result = _completed_notifications[:]
    _completed_notifications.clear()
    return result


def _notify_completion(project: str, summary: str):
    """Called by ClaudeCodeSession monitor when a task finishes."""
    _completed_notifications.append((project, summary))
    logger.info(f"Notification queued: {project} — {summary[:80]}")


# =============================================================================
# Main loop
# =============================================================================

async def run_claude_mode(project: str, session_choice: str, project_path: str) -> str:
    """
    Run the Claude coding mode state machine.

    Args:
        project: Project name (e.g., "nexus")
        session_choice: "last", "previous", or "new"
        project_path: Absolute path to project directory

    Returns:
        "jarvis" — user wants to go back to Gemini
        "close"  — user closed the session
    """
    session_id = get_session_id(project, session_choice)

    # Get or create Claude Code session
    if project in _active_sessions:
        claude = _active_sessions[project]
        logger.info(f"Resuming active session for {project} (status={claude.status})")
    else:
        claude = ClaudeCodeSession(project=project)
        _active_sessions[project] = claude

    claude.project = project
    claude._notify_on_complete = False  # User is here, no background notifications

    def _exit_to_jarvis():
        """Enable background notifications if Claude is still working."""
        claude._notify_on_complete = claude.is_busy
        if claude.is_busy:
            logger.info(f"Claude still working on {project} — notifications enabled")
        return "jarvis"

    claudia = Claudia()
    last_claude_output: str | None = None
    state = State.IDLE
    buffer: list[str] = []

    logger.info(f"Claude mode: project={project}, choice={session_choice}, sid={session_id}")

    # If there's a pending result from background work, deliver it
    if claude.status == "done" and claude.result_text:
        last_claude_output = claude.result_text
        await asyncio.to_thread(speak, f"Connected to {project}. Result from earlier.")
        interrupted = await asyncio.to_thread(speak, last_claude_output)
        claude.status = "idle"
        if claude.session_id:
            session_id = claude.session_id
            await asyncio.to_thread(save_session, project, session_id,
                                    last_claude_output[:100])
    else:
        # Quick local TTS — no API call delay
        await asyncio.to_thread(speak, f"Connected to {project}")

    while True:
        # ── IDLE ──────────────────────────────────────────────────────
        if state == State.IDLE:
            # Check if Claude finished in background
            if claude.status == "done" and claude.result_text:
                last_claude_output = claude.result_text
                logger.info(f"Claude done, reading result ({len(last_claude_output)} chars)")
                await asyncio.to_thread(speak, last_claude_output)
                claude.status = "idle"
                if claude.session_id:
                    session_id = claude.session_id
                    await asyncio.to_thread(save_session, project, session_id,
                                            last_claude_output[:100])
                continue

            # 0.3s silence keeps the trigger snappy. The real STT
            # reliability lever is the minimum-speech-duration guard
            # inside record_speech — it rejects noise blips that
            # previously produced Whisper hallucinations like
            # "thanks for watching!".
            audio = await asyncio.to_thread(record_speech, 0.3, 5, 5.0)
            if audio is None:
                continue

            text = await asyncio.to_thread(transcribe, audio)
            logger.info(f"[IDLE] {text}")

            if not text or len(text.strip()) < 2:
                continue

            # Cancel keywords take precedence over the triggers because
            # "stop friday" contains "friday" as a substring — without
            # this check, "stop friday" in IDLE would enter recording.
            if has_keyword(text, "stop_claude") or has_keyword(text, "stop_claudia"):
                # Nothing to cancel in IDLE — ignore silently.
                continue

            # Trigger words toggle: one hearing opens recording,
            # two hearings in one utterance = full single-shot prompt.
            claude_hits = count_keyword(text, "claude_trigger")
            claudia_hits = count_keyword(text, "claudia_trigger")
            keyword = detect_keyword(text)

            if claude_hits >= 1:
                if claude.is_busy:
                    await asyncio.to_thread(speak, "Hold on, Claude is still working on it.")
                    continue

                remaining = strip_keyword(text, "claude_trigger").strip()

                if claude_hits >= 2 and remaining and len(remaining) > 3:
                    # Full command in one utterance: "friday do X friday"
                    prompt = strip_keyword(remaining, "claude_trigger").strip()
                    if prompt:
                        logger.info(f"Single-utterance prompt: '{prompt}'")
                        await asyncio.to_thread(play_ack)
                        state = State.WAITING_CLAUDE
                        await claude.run(prompt, project_path, session_id)
                    else:
                        await asyncio.to_thread(speak, "I didn't catch a prompt.")
                else:
                    # Just "friday" or "friday, start of prompt..." — open
                    # recording, next "friday" will submit.
                    state = State.RECORDING_CLAUDE
                    buffer = []
                    if remaining and len(remaining) > 3:
                        buffer.append(remaining)
                    else:
                        await asyncio.to_thread(play_greeting)
                continue

            elif claudia_hits >= 1:
                if not last_claude_output:
                    await asyncio.to_thread(
                        speak, "I don't have context yet. Ask Claude something first."
                    )
                    continue
                if claude.is_busy:
                    await asyncio.to_thread(speak, "Hold on, Claude is still working.")
                    continue

                state = State.RECORDING_CLAUDIA
                buffer = []
                remaining = strip_keyword(text, "claudia_trigger").strip()

                if claudia_hits >= 2 and remaining and len(remaining) > 3:
                    question = strip_keyword(remaining, "claudia_trigger").strip()
                    if question:
                        buffer.append(question)
                        logger.info(f"Single-utterance Claudia: '{question}'")
                        await asyncio.to_thread(play_ack)
                        state = State.WAITING_CLAUDIA
                    else:
                        await asyncio.to_thread(speak, "I didn't catch a question.")
                elif remaining and len(remaining) > 3:
                    buffer.append(remaining)
                else:
                    await asyncio.to_thread(play_greeting)
                continue

            elif keyword == "jarvis":
                return _exit_to_jarvis()

            elif keyword == "close_session":
                claude.kill()
                _active_sessions.pop(project, None)
                await asyncio.to_thread(speak, f"Session closed for {project}.")
                return "close"

            # No keyword — ignore
            continue

        # ── RECORDING for Claude ──────────────────────────────────────
        elif state == State.RECORDING_CLAUDE:
            audio = await asyncio.to_thread(record_speech)
            if audio is None:
                continue

            text = await asyncio.to_thread(transcribe, audio)
            logger.info(f"[REC_CLAUDE] {text}")

            if not text or len(text.strip()) < 2:
                continue

            keyword = detect_keyword(text)

            # "stop friday" — cancel recording, drop buffer. Checked
            # BEFORE the submit keyword because "stop friday" contains
            # the literal word "friday" and would otherwise submit.
            if has_keyword(text, "stop_claude"):
                logger.info(f"Claude recording cancelled; dropped {len(buffer)} buffered lines")
                buffer = []
                state = State.IDLE
                await asyncio.to_thread(speak, "Cancelled.")
                continue

            # Second "friday" hearing → submit buffered prompt.
            if has_keyword(text, "claude_trigger"):
                remaining = strip_keyword(text, "claude_trigger").strip()
                if remaining and len(remaining) > 3:
                    buffer.append(remaining)
                prompt = " ".join(buffer)
                if not prompt.strip():
                    await asyncio.to_thread(speak, "I didn't catch a prompt. Try again.")
                    state = State.IDLE
                    continue
                logger.info(f"Multi-utterance prompt: '{prompt}'")
                await asyncio.to_thread(play_ack)
                state = State.WAITING_CLAUDE
                await claude.run(prompt, project_path, session_id)
                continue

            elif keyword == "jarvis":
                return _exit_to_jarvis()

            else:
                buffer.append(text)
                continue

        # ── RECORDING for Claudia ─────────────────────────────────────
        elif state == State.RECORDING_CLAUDIA:
            audio = await asyncio.to_thread(record_speech)
            if audio is None:
                continue

            text = await asyncio.to_thread(transcribe, audio)
            logger.info(f"[REC_CLAUDIA] {text}")

            if not text or len(text.strip()) < 2:
                continue

            keyword = detect_keyword(text)

            # "stop wednesday" — cancel Claudia recording, drop buffer.
            if has_keyword(text, "stop_claudia"):
                logger.info(f"Claudia recording cancelled; dropped {len(buffer)} buffered lines")
                buffer = []
                state = State.IDLE
                await asyncio.to_thread(speak, "Cancelled.")
                continue

            # Second "wednesday" hearing → submit buffered question.
            if has_keyword(text, "claudia_trigger"):
                remaining = strip_keyword(text, "claudia_trigger").strip()
                if remaining and len(remaining) > 3:
                    buffer.append(remaining)
                question = " ".join(buffer)
                if not question.strip():
                    await asyncio.to_thread(speak, "I didn't catch a question. Try again.")
                    state = State.IDLE
                    continue
                logger.info(f"Claudia question: '{question}'")
                state = State.WAITING_CLAUDIA
                continue

            elif keyword == "jarvis":
                return _exit_to_jarvis()

            else:
                buffer.append(text)
                continue

        # ── WAITING for Claude Code ───────────────────────────────────
        elif state == State.WAITING_CLAUDE:
            if claude.status in ("done", "error"):
                if claude.status == "done" and claude.result_text:
                    last_claude_output = claude.result_text
                    if claude.session_id:
                        session_id = claude.session_id
                        await asyncio.to_thread(
                            save_session, project, session_id,
                            last_claude_output[:100],
                        )
                    logger.info(f"Reading result ({len(last_claude_output)} chars)")
                    await asyncio.to_thread(speak, last_claude_output)
                    claude.status = "idle"
                else:
                    await asyncio.to_thread(speak, "Something went wrong with Claude.")
                    claude.status = "idle"
                state = State.IDLE
                continue

            # Listen briefly while waiting — catch "stop friday", "jarvis",
            # or a progress query.
            audio = await asyncio.to_thread(record_speech, 1.0, 3.0, 3.0)
            if audio is not None:
                text = await asyncio.to_thread(transcribe, audio)
                logger.info(f"[WAIT_CLAUDE] {text}")
                kw = detect_keyword(text)
                if has_keyword(text, "stop_claude"):
                    # Abort the running subprocess and return to IDLE.
                    logger.info("Claude run aborted by user")
                    claude.kill()
                    claude.status = "idle"
                    state = State.IDLE
                    await asyncio.to_thread(speak, "Cancelled.")
                    continue
                if kw == "jarvis":
                    # Exit but keep Claude running in background
                    return _exit_to_jarvis()
                elif text and len(text.strip()) > 2:
                    progress = claude.get_progress()
                    await asyncio.to_thread(
                        speak, f"Claude is still cooking. {progress}"
                    )

            await asyncio.sleep(0.5)
            continue

        # ── WAITING for Claudia API ───────────────────────────────────
        elif state == State.WAITING_CLAUDIA:
            question = " ".join(buffer)
            logger.info(f"Claudia: {question[:100]}")

            context = last_claude_output or ""
            logger.info(f"Claudia context: {len(context)} chars, first 80: '{context[:80]}'")

            try:
                response = await asyncio.to_thread(
                    claudia.ask, question, context
                )
                logger.info(f"Claudia response: {response[:100]}")
                await asyncio.to_thread(speak, response)
            except Exception as e:
                logger.error(f"Claudia error: {e}")
                await asyncio.to_thread(speak, "Claudia ran into an error. Try again.")

            state = State.IDLE
            continue
