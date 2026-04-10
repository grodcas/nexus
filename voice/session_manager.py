#!/usr/bin/env python3
"""
Session manager for Claude Code sessions.

Persists session IDs and descriptions to ~/.nexus/sessions.json.
Keeps last 2 sessions per project for quick resume.
"""

import json
import os
import time

from loguru import logger

SESSIONS_FILE = os.path.expanduser("~/.nexus/sessions.json")
PROJECTS_FILE = os.path.expanduser("~/.nexus/projects.json")


# =============================================================================
# Projects
# =============================================================================

def load_projects() -> dict:
    """Load project name → path mapping."""
    projects = {"nexus": "~/nexus"}
    if os.path.exists(PROJECTS_FILE):
        with open(PROJECTS_FILE) as f:
            projects.update(json.load(f))
    return projects


# =============================================================================
# Sessions persistence
# =============================================================================

def _load() -> dict:
    if os.path.exists(SESSIONS_FILE):
        with open(SESSIONS_FILE) as f:
            return json.load(f)
    return {}


def _save(data: dict):
    os.makedirs(os.path.dirname(SESSIONS_FILE), exist_ok=True)
    with open(SESSIONS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_sessions(project: str) -> list[dict]:
    """
    Get recent sessions for a project.
    Returns list of {"session_id", "description", "last_active"}, newest first.
    """
    data = _load()
    entry = data.get(project, {})

    # Handle legacy format
    if isinstance(entry, str):
        return [{"session_id": entry, "description": "Legacy session", "last_active": "unknown"}]

    sessions = entry.get("sessions", [])

    # Handle old single-session format
    if not sessions and isinstance(entry, dict) and entry.get("session_id"):
        desc = entry.get("last_result", "No description")[:100]
        when = entry.get("last_result_time", "unknown")
        return [{"session_id": entry["session_id"], "description": desc, "last_active": when}]

    return sessions[:2]


def save_session(project: str, session_id: str, description: str = ""):
    """Save or update a session. Keeps last 2 per project."""
    data = _load()
    if not isinstance(data.get(project), dict):
        data[project] = {}

    sessions = data[project].get("sessions", [])
    now = time.strftime("%Y-%m-%d %H:%M")

    # Update existing or add new
    updated = False
    for s in sessions:
        if s["session_id"] == session_id:
            s["last_active"] = now
            if description:
                s["description"] = description[:100]
            sessions.remove(s)
            sessions.insert(0, s)  # move to front
            updated = True
            break

    if not updated:
        sessions.insert(0, {
            "session_id": session_id,
            "description": description[:100] if description else "New session",
            "last_active": now,
        })

    data[project]["sessions"] = sessions[:2]
    _save(data)
    logger.info(f"Session saved: {project}/{session_id[:12]}... — {description[:50]}")


def get_session_id(project: str, choice: str) -> str | None:
    """
    Get session ID for a choice.

    Args:
        choice: "last", "previous", or "new"

    Returns:
        Session ID string, or None (= start fresh).
    """
    if choice == "new":
        return None

    sessions = get_sessions(project)
    if choice == "last" and sessions:
        return sessions[0]["session_id"]
    if choice == "previous" and len(sessions) > 1:
        return sessions[1]["session_id"]

    return None


def close_session(project: str):
    """Remove all sessions for a project."""
    data = _load()
    if project in data:
        data[project] = {"sessions": []}
        _save(data)
        logger.info(f"Sessions cleared for {project}")


def format_sessions_for_display(project: str) -> str:
    """Format sessions for Jarvis to read aloud."""
    sessions = get_sessions(project)
    if not sessions:
        return f"No previous sessions for {project}. Will start a new one."

    parts = []
    for i, s in enumerate(sessions):
        label = "Last session" if i == 0 else "Previous session"
        parts.append(f"{label}: {s['description']}, {s['last_active']}")
    parts.append("Say continue last, resume previous, or start new.")
    return " ".join(parts)


def format_all_sessions() -> str:
    """Format all project sessions for listing."""
    projects = load_projects()
    parts = []
    for name in projects:
        sessions = get_sessions(name)
        if sessions:
            s = sessions[0]
            parts.append(f"{name}: {s['description']} ({s['last_active']})")
        else:
            parts.append(f"{name}: no sessions")
    return "\n".join(parts) if parts else "No sessions."
