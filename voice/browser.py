#!/usr/bin/env python3
"""
browser.py — Persistent browser for Nexus.

Launches Chromium with:
  - Persistent profile (~/.nexus/playwright_profile/) for cookies/logins
  - Anti-automation flags stripped so Google/etc. don't block login
  - Unix socket server so nav.py can send commands from any process

Threading model:
  - Playwright thread: owns the browser, executes all page operations
  - Socket server thread: accepts nav.py connections, marshals commands
    to the Playwright thread via a Queue, waits for results
  - Main thread (jarvis): calls ensure_browser() / stop_browser()
"""

from __future__ import annotations

import json
import os
import queue
import socket
import sys
import threading
import time

from loguru import logger

try:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import screens
except ImportError:
    screens = None


# =============================================================================
# Configuration
# =============================================================================

PROFILE_DIR = os.path.expanduser("~/.nexus/playwright_profile")
SOCKET_PATH = os.path.expanduser("~/.nexus/browser.sock")
BROWSER_WINDOW_X = 50
BROWSER_WINDOW_Y = 50
BROWSER_WINDOW_W = 1300
BROWSER_WINDOW_H = 950


# =============================================================================
# Playwright thread — all browser operations happen here
# =============================================================================

_pw_thread = None
_cmd_queue = queue.Queue()
_browser_ready = threading.Event()
_browser_context = None  # Only accessed from _pw_thread
_shutdown = False


def _pw_thread_run():
    """Dedicated thread for all Playwright operations."""
    global _browser_context

    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()

    try:
        _browser_context = pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            ignore_default_args=[
                "--enable-automation",
                "--disable-component-extensions-with-background-pages",
            ],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-session-crashed-bubble",
                "--disable-infobars",
                "--no-default-browser-check",
                "--hide-crash-restore-bubble",
                f"--window-size={BROWSER_WINDOW_W},{BROWSER_WINDOW_H}",
                f"--window-position={BROWSER_WINDOW_X},{BROWSER_WINDOW_Y}",
            ],
            viewport={"width": BROWSER_WINDOW_W, "height": BROWSER_WINDOW_H},
        )

        if not _browser_context.pages:
            _browser_context.new_page()

        # Dismiss "restore pages" dialog if present
        try:
            page = _browser_context.pages[0]
            restore_btn = page.get_by_role("button", name="Restore")
            if restore_btn.count() > 0:
                # Click the X or dismiss — don't restore old tabs
                page.keyboard.press("Escape")
                time.sleep(0.3)
        except Exception:
            pass

        logger.info(f"Browser started (profile: {PROFILE_DIR})")
        _browser_ready.set()

        # Command processing loop
        while not _shutdown:
            try:
                item = _cmd_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if item is None:
                break  # Shutdown signal

            cmd, response_event, response_holder = item
            try:
                result = _execute_command(cmd)
            except Exception as e:
                result = {"error": str(e)[:300]}
            response_holder["result"] = result
            response_event.set()

    except Exception as e:
        logger.error(f"Playwright thread error: {e}")
        _browser_ready.set()  # Unblock waiting threads even on failure
    finally:
        if _browser_context:
            try:
                _browser_context.close()
            except Exception:
                pass
            _browser_context = None
        try:
            pw.stop()
        except Exception:
            pass


def _execute_on_pw_thread(cmd: dict, timeout: float = 20) -> dict:
    """Thread-safe: marshal a command to the Playwright thread and wait."""
    evt = threading.Event()
    holder = {}
    _cmd_queue.put((cmd, evt, holder))
    if evt.wait(timeout=timeout):
        return holder.get("result", {"error": "No result"})
    return {"error": "Command timed out"}


def _get_page():
    """Get the active page. ONLY call from Playwright thread."""
    if _browser_context is None:
        return None
    pages = _browser_context.pages
    if pages:
        return pages[0]
    return _browser_context.new_page()


def _execute_command(cmd: dict) -> dict:
    """Execute a navigation command. ONLY call from Playwright thread."""
    page = _get_page()
    if page is None:
        return {"error": "No browser page available"}

    action = cmd.get("action", "")

    try:
        if action == "state":
            url = page.url
            title = page.title()
            elements = page.evaluate("""() => {
                const r = { links: [], buttons: [], inputs: [] };
                document.querySelectorAll('a[href]').forEach(a => {
                    const t = a.innerText?.trim();
                    if (t && t.length > 0 && t.length < 80 && a.offsetParent !== null)
                        r.links.push(t.substring(0, 60));
                });
                r.links = [...new Set(r.links)].slice(0, 25);
                document.querySelectorAll('button, [role="button"], input[type="submit"]').forEach(b => {
                    const t = (b.innerText || b.value || b.getAttribute('aria-label') || '').trim();
                    if (t && t.length > 0 && b.offsetParent !== null)
                        r.buttons.push(t.substring(0, 40));
                });
                r.buttons = [...new Set(r.buttons)].slice(0, 15);
                document.querySelectorAll('input:not([type="hidden"]), textarea, select').forEach(inp => {
                    if (inp.offsetParent === null) return;
                    const l = inp.getAttribute('aria-label')
                        || inp.getAttribute('placeholder')
                        || inp.getAttribute('name')
                        || inp.type || '';
                    r.inputs.push(l.substring(0, 40));
                });
                r.inputs = r.inputs.slice(0, 10);
                return r;
            }""")
            lines = [f"URL: {url}", f"Title: {title}"]
            if elements.get("links"):
                lines.append(f"Links: {', '.join(elements['links'][:20])}")
            if elements.get("buttons"):
                lines.append(f"Buttons: {', '.join(elements['buttons'])}")
            if elements.get("inputs"):
                lines.append(f"Inputs: {', '.join(elements['inputs'])}")
            return {"result": "\n".join(lines)}

        elif action == "goto":
            url = cmd.get("url", "")
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            return {"result": f"OK: {page.url} — {page.title()}"}

        elif action == "click":
            text = cmd.get("text", "")
            locator = page.get_by_text(text, exact=False)
            if locator.count() == 0:
                locator = page.get_by_role("link", name=text)
            if locator.count() == 0:
                locator = page.get_by_role("button", name=text)
            if locator.count() == 0:
                return {"result": f"NOT FOUND: no element with text '{text}'"}
            locator.first.click(timeout=5000)
            page.wait_for_load_state("domcontentloaded", timeout=5000)
            time.sleep(0.5)
            return {"result": f"OK: clicked '{text}' → {page.url}"}

        elif action == "type":
            selector = cmd.get("selector", "")
            value = cmd.get("value", "")
            locator = page.get_by_placeholder(selector)
            if locator.count() == 0:
                locator = page.get_by_label(selector)
            if locator.count() == 0:
                locator = page.locator(selector)
            if locator.count() == 0:
                return {"result": f"NOT FOUND: no input matching '{selector}'"}
            locator.first.fill(value, timeout=5000)
            return {"result": f"OK: typed into '{selector}'"}

        elif action == "press":
            key = cmd.get("key", "Enter")
            page.keyboard.press(key)
            time.sleep(0.5)
            return {"result": f"OK: pressed {key} → {page.url}"}

        elif action == "screenshot":
            ss_dir = os.path.expanduser("~/.nexus/screenshots")
            os.makedirs(ss_dir, exist_ok=True)
            path = os.path.join(ss_dir, f"nav_{int(time.time())}.png")
            page.screenshot(path=path)
            return {"result": f"OK: {path}"}

        elif action == "scroll":
            direction = cmd.get("direction", "down")
            delta = 500 if direction == "down" else -500
            page.mouse.wheel(0, delta)
            time.sleep(0.3)
            return {"result": f"OK: scrolled {direction}"}

        else:
            return {"error": f"Unknown action: {action}"}

    except Exception as e:
        return {"error": str(e)[:300]}


# =============================================================================
# Socket server — accepts nav.py connections (runs in its own thread)
# =============================================================================

_server_thread = None
_server_socket = None


def _handle_client(conn: socket.socket):
    """Handle one client connection."""
    try:
        data = b""
        while True:
            chunk = conn.recv(8192)
            if not chunk:
                break
            data += chunk
            try:
                json.loads(data.decode("utf-8"))
                break  # Valid JSON received
            except json.JSONDecodeError:
                continue

        if not data:
            return

        cmd = json.loads(data.decode("utf-8"))
        # Marshal to Playwright thread
        result = _execute_on_pw_thread(cmd)
        conn.sendall(json.dumps(result).encode("utf-8"))
    except Exception as e:
        try:
            conn.sendall(json.dumps({"error": str(e)[:200]}).encode())
        except Exception:
            pass
    finally:
        conn.close()


def _run_server():
    """Socket server loop."""
    global _server_socket

    try:
        os.unlink(SOCKET_PATH)
    except OSError:
        pass

    _server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    _server_socket.bind(SOCKET_PATH)
    _server_socket.listen(5)
    _server_socket.settimeout(1.0)

    logger.info(f"Nav server listening on {SOCKET_PATH}")

    while _server_socket is not None and not _shutdown:
        try:
            conn, _ = _server_socket.accept()
            # Handle in a short-lived thread so server stays responsive
            t = threading.Thread(target=_handle_client, args=(conn,), daemon=True)
            t.start()
        except socket.timeout:
            continue
        except OSError:
            break


def _start_server():
    global _server_thread
    if _server_thread and _server_thread.is_alive():
        return
    _server_thread = threading.Thread(target=_run_server, daemon=True)
    _server_thread.start()


def _stop_server():
    global _server_socket
    if _server_socket:
        try:
            _server_socket.close()
        except Exception:
            pass
    _server_socket = None
    try:
        os.unlink(SOCKET_PATH)
    except OSError:
        pass


# =============================================================================
# Public API
# =============================================================================

def is_running() -> bool:
    """Check if the browser is running."""
    return _pw_thread is not None and _pw_thread.is_alive() and _browser_ready.is_set()


def ensure_browser() -> None:
    """Start the persistent browser + socket server if not already running."""
    global _pw_thread, _shutdown

    if is_running():
        return

    _shutdown = False
    os.makedirs(PROFILE_DIR, exist_ok=True)

    # Clean stale lock files
    for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            os.remove(os.path.join(PROFILE_DIR, lock))
        except OSError:
            pass

    # Capture focused app
    previous_app = None
    if screens:
        previous_app = screens.get_frontmost_app()

    # Start Playwright on its own thread
    _browser_ready.clear()
    _pw_thread = threading.Thread(target=_pw_thread_run, daemon=True)
    _pw_thread.start()

    # Wait for browser to be ready
    if not _browser_ready.wait(timeout=30):
        logger.error("Browser failed to start within 30s")
        return

    # Start socket server
    _start_server()

    # Refocus user's app and position window
    if screens and previous_app:
        try:
            time.sleep(0.3)
            screens.focus_app(previous_app)
        except Exception:
            pass

    if screens:
        time.sleep(0.5)
        try:
            screens.place_window(
                screens.BROWSER_PROCESS,
                BROWSER_WINDOW_X, BROWSER_WINDOW_Y,
                BROWSER_WINDOW_W, BROWSER_WINDOW_H,
            )
            if previous_app:
                screens.focus_app(previous_app)
        except Exception as e:
            logger.warning(f"Window placement failed: {e}")


def stop_browser() -> None:
    """Kill browser and socket server."""
    global _pw_thread, _shutdown

    _shutdown = True
    _stop_server()

    # Signal Playwright thread to stop
    _cmd_queue.put(None)

    if _pw_thread and _pw_thread.is_alive():
        _pw_thread.join(timeout=5)
    _pw_thread = None

    _browser_ready.clear()
    logger.info("Browser stopped")


# Client function for nav.py (imported directly)
def send_command(cmd: dict) -> dict:
    """Send a command to the browser daemon via Unix socket."""
    if not os.path.exists(SOCKET_PATH):
        raise RuntimeError("Browser not running.")

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
        return json.loads(data.decode("utf-8"))
    except ConnectionRefusedError:
        raise RuntimeError("Browser socket refused. Restart browser.")
    finally:
        sock.close()


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("cmd", choices=["start", "stop", "status"])
    args = parser.parse_args()

    if args.cmd == "start":
        ensure_browser()
        print("Browser running. Press Ctrl+C to stop")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop_browser()
    elif args.cmd == "stop":
        stop_browser()
    elif args.cmd == "status":
        print(f"Running: {is_running()}")
