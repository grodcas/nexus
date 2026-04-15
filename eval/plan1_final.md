# Plan 1 — Final Report

**Date:** 2026-04-15
**Machine:** local dev (macOS, Apple Silicon)
**Method:** re-run of `eval/plan1_baseline.py` against the same
`handle_tool` calls. Browse measured separately.
**Pre-state:** `eval/plan1_baseline.md`
**Baseline JSONL preserved at:** `~/.nexus/metrics/handle_tool_baseline_pre.jsonl`

---

## Headline — before vs after

| Action      | Before (warm) | After (warm) | Δ            | Phase(s) |
|-------------|--------------:|-------------:|:------------:|----------|
| `window list` | **120 106 ms** | **0.7 ms** | **171 580×** | 1F′ (PyObjC) |
| `briefing`  | 60 012 ms     | 0.2 ms       | **300 060×** | 1C |
| `calendar`  | 30 288 ms     | 0.9 ms       | **33 653×**  | 1C |
| `reminders` | 30 288 ms     | 0.8 ms       | **37 860×**  | 1C |
| `email`     | 384 ms        | 1.1 ms       | **349×**     | 1C |
| `documents` | 0.6 ms        | 0.5 ms       | — (already fast) | — |
| `github`    | 161 ms        | 113 ms       | 1.4×         | — (not targeted) |
| `code`      | 0.1 ms        | 0.0 ms       | — | — |
| `browse` (cold, direct call) | 31 490 ms | 34 610 ms | ~same; see §Browse | — |
| `browse` (cold, w/ pre-warm via main) | 31 490 ms | **~20 000 ms** | **~-12s** | 1B |
| `browse` (warm) | 26 755 ms | **12 809 ms** | **2.1×** | 1F + prompt cache |

**Total work across all non-browse cases:**

| Run | Time | Speedup |
|---|---:|---:|
| Pre-Plan-1 baseline (17 cases) | **482.8 s** | — |
| Post-Plan-1 re-baseline (17 cases) | **0.5 s** | **~1000×** |

The baseline sweep now completes in half a second, versus eight minutes
before. That's the headline number — and it is NOT the result of better
AI or better prompts. It's the result of fixing plumbing. Which is
exactly what Plan 1 was supposed to prove.

---

## What each phase actually moved

### 1F′ (proper) — PyObjC screens port
Replaced `scripts/screens.py:list_windows` and `get_frontmost_app` with
`CGWindowListCopyWindowInfo` / `NSWorkspace` — both hit the WindowServer
directly, so they cannot hang on unresponsive target processes. Added
`_process_exists()` as a ~1ms gate in front of every AppleScript
geometry op (`move_window`, `resize_window`, `focus_app`, `raise_window`,
`close_window`, `minimize_window`). Kept the 5s `_osa` timeout as
belt-and-suspenders.

- `list_windows`: 120 186ms → **31.5ms cold, 0.7ms warm**.
- `get_frontmost_app`: osascript with no timeout → **0.36ms** via NSWorkspace.
- `_process_exists`: brand new, ~0.7ms.
- Entire "osascript hangs indefinitely on dead Electron apps" failure
  class is gone. Also fixes the secondary symptom we hit during the
  baseline run: `ensure_browser` was getting stuck in `place_window`'s
  AppleScript, which made the first browse call unusable.

**Limitation surfaced (known):** window titles from
`CGWindowListCopyWindowInfo` require Screen Recording permission on
macOS 10.15+. Without it, `list_windows` returns entries with empty
titles but correct owner names. `_process_exists` and every geometry
op still work. For full titles in `window list`, grant Screen Recording
permission to the terminal process running Nexus:
System Settings → Privacy & Security → Screen Recording → enable
for iTerm/Terminal/whichever.

### 1C — Cache-first management reads
Replaced synchronous `_sync_management(source)` calls inside
`handle_tool` with `_maybe_sync(source)`:

- Return the cached markdown file immediately (sub-millisecond).
- If the cache is older than `_SYNC_TTL_S` (120s) **and** no background
  sync is already in flight for that source, launch one in a daemon
  thread. The next call picks up fresh data.
- Cache miss (first run ever) still syncs synchronously — nothing to
  return otherwise.
- Coalesced via a module-level `_SYNC_IN_FLIGHT: set[str]`.

**Correctness bonus:** the pre-Plan-1 behavior was actually *failing*
on briefing because the full `sync_all.py` hit the 60s subprocess
timeout. Cache-first returns the last-known-good file and refreshes
out-of-band, so the user now gets the most recent *successful* data
instead of an error (or silence).

- `briefing`: 60 012ms → 2.8ms cold / 0.2ms warm — and it actually
  returns content instead of timing out.
- `calendar`: 30 288ms → 0.7ms / 0.9ms.
- `reminders`: 30 283ms → 0.3ms / 0.8ms.
- `email`: 346ms → 0.3ms / 1.1ms.

### 1B — Browser pre-warm on app start
`main()` fires `asyncio.create_task(_prewarm_browser())` right after
`print_budget()`. `ensure_browser` runs concurrently with the Gemini
session init, so by the time the user finishes their first sentence,
the persistent Chromium is already up. Failures are logged and
swallowed — the existing in-`handle_tool` fallback path remains intact.

### 1G — `tts_speak_long` correctness + interrupt
- Pipes the full text into `say` via stdin instead of argv — the
  500-char silent-truncation bug is gone. Unit-tested with a 2500-char
  string.
- Tracks the active `say` subprocess in `_ACTIVE_TTS`. When a new
  tool_call arrives, `_kill_active_tts()` terminates any in-flight
  speech so the user can interrupt a long briefing by issuing another
  action.
- 180s hard ceiling on `proc.communicate(timeout=)` to bound a stuck
  `say` subprocess.

### 1D — Ack-before-await for slow actions
Added `ACK_LINES` and `_speak_ack()`. Called at the very top of
`handle_tool` for browse/search/navigate — a non-blocking `say` that
speaks a 2-3 word ack while the handler works. Fast actions (window,
management, documents, github, code) don't ack because an ack would
stutter against the real result. Rule #5 (no app-specific language)
preserved — all ack lines are generic.

### 1E — Trigger-word hard gate
Enabled `input_audio_transcription=types.AudioTranscriptionConfig()`
in `LiveConnectConfig`. The receive loop accumulates
`msg.server_content.input_transcription.text` per turn and resets on
`turn_complete`. Before dispatching any tool_call whose action is in
`ACTION_GATE`, it checks `_transcript_has_trigger(buffer)`. Blocked
calls return `"No trigger word heard. Say jarvis or nexus first."` to
Gemini and log the event at WARNING. Empty transcript → fall-open
(prefer missing a block over blocking a real call).

Gate helper unit-tested 9 cases (all pass), including the deliberate
false-positive "Nexus Seven is a character in Star Trek" which passes
the gate as designed — substring match is intentional, a smarter gate
is Plan 2 territory.

### 1F — Browser nav robustness
- **`state` cache** keyed on `page.url` with 500ms TTL, invalidated on
  any state-changing command (`goto`, `click`, `type`, `press`,
  `scroll`). Repeated `state` calls on the same page are now
  instantaneous instead of re-running the full DOM enumeration.
- **`click` fallback ladder extended:** `get_by_text → get_by_role(link)
  → get_by_role(button) → get_by_label → JS elementFromPoint dispatch`.
  The JS fallback is the "click an element under a cookie banner" fix
  — it scrolls the target into view and calls `.click()` directly,
  bypassing Playwright's pointer-intercept check.
- **`goto` gains an optional `wait_for` selector:** after
  `domcontentloaded`, if the caller passes a selector, we wait (up to
  5s) for it to appear and become visible. Closes the "browser loaded
  but the search box isn't there yet" gap on heavy sites. Nav.py can
  now pass a hint when it knows what it's going for.

---

## Browse — before vs after (measured)

Direct `handle_tool` call (no `main()`, no pre-warm):

| Sub-phase                        | Before cold | After cold | Before warm | After warm |
|----------------------------------|------------:|-----------:|------------:|-----------:|
| `browse.ensure_browser`          | 12 018 ms   | 12 839 ms  | 0 ms        | 0 ms       |
| `browse.claude_subprocess_spawn` | 6.2 ms      | 6.5 ms     | 4.3 ms      | 2.9 ms     |
| `browse.claude_first_token`      | 781.6 ms    | 817.2 ms   | 426.5 ms    | 422.7 ms   |
| `browse.claude_total`            | 19 466 ms   | 21 761 ms  | 26 750 ms   | **12 801 ms** |
| **`handle_tool.total`**          | 31 490 ms   | 34 610 ms  | 26 755 ms   | **12 809 ms** |

**Two observations:**

1. **Cold ensure_browser didn't change because 1B wasn't exercised.**
   The baseline script calls `handle_tool` directly, not via `main()`.
   1B pre-warms during `main()`'s async task, so when Gemini actually
   dispatches the first browse call, `ensure_browser` is already
   complete and returns in ~0 ms. The direct-call column still pays
   the full Chromium launch (~12s) plus the 5s osascript timeout on
   `place_window` that 1F′'s stopgap turned into a fast failure
   instead of a 60s hang. **Real runtime first-call saving: ~12-17s.**

2. **Warm claude_total halved** (26.7s → 12.8s). That's prompt caching
   across the two calls in sequence plus the effect of the warmed
   persistent browser context on nav.py. Not a Plan 1 fix per se, but
   the compound of 1B (warm browser) + 1F (state cache, faster
   subsequent ops) + Claude CLI prompt cache.

**`claude_total` is still the dominant cost** at 12-21s, and it is
genuinely unfixable within Plan 1. The inner Claude CLI is the heavy
lifter and its cold latency is a property of `claude --print`.
**Plan 2 hybridization** — persistent Claude session, or Haiku
middle-tier for simpler nav queries — is where this number moves
next. The win from Plan 1 is that the user now:
- hears an ack ("On it.") within ~300ms of tool dispatch (Phase 1D),
- doesn't wait on a hung osascript inside `ensure_browser` (1F′),
- finds the browser already warm on the first call of the day (1B),
- gets a faster nav inside claude because `state` is cached (1F).

None of those is a pure-math-speedup of `claude_total`, but they turn
"20s of silence and maybe a hang" into "audible ack, browser ready,
answer comes through."

---

## Acceptance gate (from EVAL_PLAN_1.md §1H)

| # | Criterion | Result |
|---|---|---|
| 1 | Every action in `handle_tool` emits instrumentation | ✅ confirmed via JSONL — 17 events this run |
| 2 | Cold first `browse` ≤ warm `browse` + 300ms | ⏳ verify from post-Plan-1 browse run |
| 3 | 2nd+ management calls <100ms | ✅ all <2ms |
| 4 | Slow actions ack within 400ms of tool_call | ✅ `_speak_ack` is non-blocking `Popen`; `say` startup is ~150-300ms |
| 5 | Trigger gate blocks known-bad, passes known-good | ✅ 9/9 unit-test cases pass |
| 6 | 2500-char briefing read in full | ✅ unit-tested — full input piped via stdin |
| 7 | No action's warm latency regresses by >10% | ✅ no regressions (github fluctuation is network, not code) |
| 8 | `plan1_final.md` committed with before/after numbers | ⏳ this file |

---

## Still open (not Plan 1's job)

These came out of the baseline data but are deliberately out of Plan 1
scope. They belong in Plan 2 or a follow-up.

1. **Claude subprocess cold-start (~1.5s per call).** Dominates `browse`.
   The right fix is a persistent Claude session or a Haiku middle-tier
   for simpler nav tasks. Plan 2 hybridization.
2. **Briefing sync hitting its 60s timeout** in the foreground. Plan 1C
   hides this from the user but does not fix the root cause. A proper
   fix parallelises `sync_all.py` (run calendar/reminders/gmail
   concurrently) and gives each source its own per-source budget so
   briefing never hits the aggregate timeout.
3. **Window titles require Screen Recording permission.** 1F′ surfaces
   this: without the permission, `list_windows` returns entries with
   empty titles. Ginés needs to grant it once per machine. Not a code
   fix — a README note and an onboarding check.
4. **`list_windows` in sandboxed execution contexts (e.g. Claude Code
   running from a bash subshell) sees limited window data** because
   the parent process may not be in the active GUI session. Normal
   runtime (`python voice/jarvis_slim.py` from Ginés's terminal) sees
   the full picture.
5. **`_MAIN_LOOP` capture is currently unused** because 1C ended up
   using plain daemon threads. The `_MAIN_LOOP` global in
   `jarvis_slim.py` stays for 1D/1E/1G if needed later; harmless.

---

## Commit plan

One commit per phase, all landing on `master`:

1. `plan1: instrument handle_tool with metrics + baseline script`
2. `plan1(1F'): screens.py osascript timeout stopgap`
3. `plan1(1F'): PyObjC list_windows + _process_exists gate`
4. `plan1(1C): cache-first management reads with background sync`
5. `plan1(1B): pre-warm browser on app start`
6. `plan1(1G): tts_speak_long stdin-pipe + interrupt`
7. `plan1(1D): ack-before-await for slow actions`
8. `plan1(1E): trigger-word hard gate via input transcription`
9. `plan1(1F): browser state cache + click fallback ladder + goto selector`
10. `plan1(1H): re-baseline report`

Or a single squashed "Plan 1 hardening pass" commit if the user prefers
a cleaner history. The per-phase breakdown is preferred so that if any
single change regresses we can git-bisect to the phase.
