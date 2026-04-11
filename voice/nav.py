#!/usr/bin/env python3
"""
nav.py — Browser navigation CLI for Claude Code.

Sends commands to the browser daemon (browser.py) via Unix socket.
No CDP, no Playwright import needed — just socket communication.

Usage (from Claude Code):
    python voice/nav.py state              # what's on screen
    python voice/nav.py goto <url>         # navigate
    python voice/nav.py click "<text>"     # click link/button by visible text
    python voice/nav.py type "<sel>" "val" # type into input
    python voice/nav.py press Enter        # press key
    python voice/nav.py screenshot         # save screenshot, return path
    python voice/nav.py scroll down        # scroll page
"""

from __future__ import annotations

import json
import os
import socket
import sys


SOCKET_PATH = os.path.expanduser("~/.nexus/browser.sock")


def send(cmd: dict) -> str:
    """Send command to browser daemon, return result string."""
    if not os.path.exists(SOCKET_PATH):
        return "BROWSER ERROR: not running. Start it first."

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(20)
    try:
        sock.connect(SOCKET_PATH)
        sock.sendall(json.dumps(cmd).encode("utf-8"))
        sock.shutdown(socket.SHUT_WR)

        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk

        result = json.loads(data.decode("utf-8"))
        return result.get("result") or result.get("error", "Unknown error")
    except ConnectionRefusedError:
        return "BROWSER ERROR: connection refused. Restart browser."
    except socket.timeout:
        return "BROWSER ERROR: command timed out."
    except Exception as e:
        return f"BROWSER ERROR: {str(e)[:200]}"
    finally:
        sock.close()


def main():
    if len(sys.argv) < 2:
        print("Usage: nav.py <command> [args...]")
        print("Commands: state, goto, click, type, press, screenshot, scroll")
        sys.exit(1)

    cmd = sys.argv[1].lower()

    if cmd == "state":
        print(send({"action": "state"}))
    elif cmd == "goto" and len(sys.argv) >= 3:
        print(send({"action": "goto", "url": sys.argv[2]}))
    elif cmd == "click" and len(sys.argv) >= 3:
        print(send({"action": "click", "text": " ".join(sys.argv[2:])}))
    elif cmd == "type" and len(sys.argv) >= 4:
        print(send({"action": "type", "selector": sys.argv[2], "value": " ".join(sys.argv[3:])}))
    elif cmd == "press" and len(sys.argv) >= 3:
        print(send({"action": "press", "key": sys.argv[2]}))
    elif cmd == "screenshot":
        print(send({"action": "screenshot"}))
    elif cmd == "scroll":
        direction = sys.argv[2] if len(sys.argv) >= 3 else "down"
        print(send({"action": "scroll", "direction": direction}))
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
