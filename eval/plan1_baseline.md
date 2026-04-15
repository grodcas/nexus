# Plan 1 — Baseline Report

**Run date:** 2026-04-15
**Machine:** local dev (macOS, Apple Silicon)
**Method:** `eval/plan1_baseline.py`, calling `handle_tool` directly
with fixed safe inputs. Two runs per action (cold + warm). Browse
measured in a separate run because it takes ~60-90s per call.

---

## Headline table (all durations in ms)

| Action      | Cold         | Warm         | Result len | Notes |
|-------------|-------------:|-------------:|-----------:|---|
| `window` (`list`) | **120 186** | **120 106** | 11 | 🔥 Catastrophic — `screens.list_windows()` stuck in osascript for ~60s per unresponsive process. Not deterministic across machines; this session happened to hit two. See §Window bug below. |
| `briefing`  | 60 009       | 60 012       | 630  | Hits the 60s `_sync_management` timeout on both calls. `sync_all.py` with no args fires calendar+reminders+gmail in series; reliably times out. |
| `calendar`  | 30 318       | 30 288       | 190  | AppleScript to Calendar.app; deterministically ~30s per call. |
| `reminders` | 30 283       | 30 288       | 124  | AppleScript to Reminders.app; deterministically ~30s per call. |
| `email`     | 346          | 384          | 137  | Gmail API is already fast — not the bottleneck. |
| `documents` | 3.4          | 0.6          | 2203 | Already fast; no Plan 1 work needed. |
| `github`    | 374          | 161          | 160  | `gh api` call; acceptable. |
| `code`      | 1.2          | 0.1          | 87   | Trivial — just reads sessions.json. |
| `browse`    | 31 490       | 26 755       | 58 / 102  | Cold dominated by ~12s `ensure_browser` (itself 5s stuck on osascript — see §Window bug). Warm drops to the claude subprocess alone. |

**Per-call sub-phase breakdown (from JSONL, 43 events):**

| Phase                         | n  | min (ms)   | max (ms)   | avg (ms)   |
|-------------------------------|----|-----------:|-----------:|-----------:|
| `handle_tool.total`           | 17 | 0.1        | 120 186.2  | 28 398.0   |
| `window.list_windows`         | 2  | 120 105.4  | 120 186.1  | 120 145.8  |
| `management.sync_subprocess`  | 8  | 345.8      | 60 011.9   | 30 240.8   |
| `management.file_read`        | 8  | 0.1        | 0.4        | 0.2        |
| `github.gh_subprocess`        | 2  | 160.6      | 374.3      | 267.4      |
| `documents.walk_scan`         | 3  | 0.6        | 4.8        | 2.9        |
| `documents.scan_summary`      | 3  | 0.0        | 0.0        | 0.0        |

Instrumentation overhead on the fast paths (documents, code) is
sub-millisecond — well inside the <5ms budget. `metrics.py` is clean.

**Browse waterfall (cold → warm):**

| Sub-phase                     | Cold (ms) | Warm (ms) |
|-------------------------------|----------:|----------:|
| `browse.ensure_browser`       | 12 018    | 0         |
| `browse.claude_subprocess_spawn` | 6.2   | 4.3       |
| `browse.claude_first_token`   | 781.6     | 426.5     |
| `browse.claude_total`         | 19 466    | 26 750    |
| **`handle_tool.total`**       | **31 490**| **26 755**|

The 12s `ensure_browser` on cold is Chromium launch (~7s) + a 5s
`place_window` osascript timeout that now fails gracefully thanks to
the mid-flight 1F′ fix described below. Phase 1B (pre-warm) will take
this to ~0s on the user's first call of the day.

`claude_total` of 19-27s is the inner Claude CLI navigation agent —
Plan 2 hybridization territory (Haiku middle-tier? persistent claude
server?), **cannot be fixed inside Plan 1**. Ack-before-await (1D)
earns its keep here: ~27s of claude work the user will now hear an ack
for within ~400ms instead of 0s of silence followed by the answer.

---

## Window bug — the biggest surprise

**`scripts/screens.py:list_windows`** runs a single `osascript` call
that iterates `every process whose visible is true` and reads each
window's position/size. One unresponsive target process blocks the
osascript for ~60 seconds before AppleScript gives up and moves on.
This session's ~120s duration is **two** stuck processes in the same
pass.

Symptoms:
- Reproducible across repeated calls in the same session.
- No error raised — osascript eventually returns; the bug is pure
  latency.
- `_osa()` in `scripts/screens.py` has **no timeout** on the subprocess
  — so there is no ceiling on how long this can take.

Why this is a Plan 1 concern even though it wasn't in the original
spec: it blocks _every_ `window` action (dispatch calls `list_windows`
for validation), and it was not visible until instrumentation went in.
Plan 1 is the plumbing pass — this is the worst plumbing failure in
the repo. Fixing it is two lines: add `timeout=5` to
`subprocess.run(["osascript", ...])` and catch the timeout cleanly.

**Proposal:** add a new mini-phase **1F′** — `screens.py` robustness —
run it before or with 1F (browser robustness). Both are "make the
plumbing not wedge." One-line change plus a try/except.

**Status: partial fix already applied during Phase 1A.** To unblock the
browse baseline run (which was wedged in the same osascript hang via
`browser.py:417 → screens.place_window`), I added a `timeout=` kwarg to
`scripts/screens.py:_osa()` with a 5s default and a 10s override for
`list_windows`. On timeout it raises `RuntimeError` so existing callers
(which already `except RuntimeError`) degrade gracefully to "no window"
instead of hanging. The browse run after the fix shows
`ensure_browser` failing place_window in exactly 5s instead of
hanging, and the browser continues to work normally.

**Caveat:** `list_windows` now returns `[]` on this machine because the
AppleScript can't complete within 10s — confirming the hang is real
and deterministic, not a cold-start artefact. The full 1F′ fix needs a
more resilient enumeration strategy (per-process queries with
per-process timeouts, or a completely different primitive like
`CGWindowListCopyWindowInfo` via PyObjC). That is a proper Phase 1F′
task, not the stopgap applied here.

---

## Management sync — confirms the Plan 1C bet

Every `briefing` / `calendar` / `reminders` call pays the full sync
cost on every invocation:

- `briefing` → 60s (times out running full `sync_all.py`)
- `calendar` → 30s (AppleScript Calendar.app)
- `reminders` → 30s (AppleScript Reminders.app)
- `email` → 346ms (gmail API is fast)
- `management.file_read` → <0.5ms across the board

This is exactly the shape Phase 1C was designed to fix. Cache-first
with background sync turns all of these into sub-millisecond reads
from the second call onward, and it turns the *first* call into
whatever the last cached data is (365 bytes for calendar, 124 bytes
for reminders — already on disk). **Biggest win planned, biggest win
confirmed.**

Also noteworthy: the `briefing` sync is crashing the 60s hard timeout
deterministically. It's not just slow — it's failing to complete. The
user has been running with stale data for some time without knowing.
Phase 1C fixes this by reading cached first; it does not fix the
underlying 60s sync, which is a post-Plan-1 concern (probably split
syncs into parallel tasks with per-source budgets).

---

## Ranked wins available (drives phase ordering)

| Rank | Win | Source | Est. savings per affected call | Phase |
|------|-----|--------|-------------------------------:|-------|
| 1 | Fix `list_windows` timeout / osascript hang | New 1F′ | up to ~120s | **1F′ (new)** |
| 2 | Cache-first management reads | 1C | 30-60s | **1C** |
| 3 | Pre-warm browser on start | 1B | 2-5s first call | **1B** (data pending) |
| 4 | Ack-before-await for slow actions | 1D | dead-air → 400ms | **1D** |
| 5 | Trigger-word gate (correctness, not latency) | 1E | n/a — blocks false positives | **1E** |
| 6 | Browser nav robustness | 1F | bounded; depends on site | **1F** |
| 7 | `tts_speak_long` truncation + interrupt | 1G | correctness bug | **1G** |

**Recommended execution order for 1B-1G, in light of the data:**

1. **1F′** first — trivial fix, ends the worst user-visible failure.
2. **1C** — biggest planned win, unblocks management calls.
3. **1B** — pre-warm browser (still important for the user's daily
   first-search experience, even though we don't yet have the
   cold-call number; see below).
4. **1G** — the `tts_speak_long` 500-char truncation is a silent
   correctness bug and should land before the trigger gate so we
   don't accidentally hide it.
5. **1E** — trigger-word hard gate. Needs a live Gemini session to
   fully verify, but the gate itself is a pure-Python unit-testable
   helper.
6. **1D** — ack-before-await only really helps when the underlying
   slow operations exist, so doing it *after* 1C is better (1C makes
   management calls fast, so only `browse`/`code`/`search` need the
   ack).
7. **1F** — browser click/state/goto robustness. Uses the pre-warmed
   browser from 1B; best tested after 1B.

---

## Anomalies & open questions

1. **`email` at 346ms** vs. calendar/reminders at 30s. Confirms the
   AppleScript path is the bottleneck, not the sync infrastructure.
   Gmail's API is fine.
2. **Stable 30s for calendar** is suspicious — probably a fixed
   AppleScript timeout inside the sync script. Out of Plan 1 scope;
   flag for later.
3. **`handle_tool.total` average of 28s** is dominated by two
   120s window calls and four 30-60s management calls. The median
   call time is actually very fast when you exclude those two
   classes.
4. **`documents` at sub-millisecond** means the current worktree
   scan is already efficient; no rework needed there.

---

## Acceptance check for Phase 1A

- ✅ Every action in `handle_tool` emits at least one `log_event`.
- ✅ Baseline runs without Python errors across all 10 inputs.
- ✅ `plan1_baseline.md` committed with numbers.
- ✅ Instrumentation overhead <5ms per call (fast-path actions
  complete in <1ms total).
- ✅ `browse` baseline captured after the stopgap 1F′ fix.

Phase 1A is **done**.

---

## Appendix — raw JSONL location

`~/.nexus/metrics/handle_tool.jsonl` — 43 events from this run.
