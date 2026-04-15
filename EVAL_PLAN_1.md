# Plan 1 — Tool Hardening (Detailed)

> Companion to `EVAL_PLAN.md`. Plan 1 is the plumbing pass that prepares
> Nexus for AI-layer evaluation. No prompts, no schema, no new features.
> Every change must cut measured latency, fix a reproducible correctness
> bug, or remove dead air. Anything else is refactor-creep and waits.

Target branch: `master`. Every phase is one atomic commit (or a tight
cluster) with before/after numbers in the commit message. Phases are
ordered so earlier phases de-risk later ones — do not reorder without
re-reading the risk sections.

---

## Phase 1A — Instrumentation & baseline

**Goal.** Produce an honest, reproducible latency table for every action
in `handle_tool`, cold and warm, before changing any behavior. Without
this table, Plan 1 has no target and Plan 2 has no baseline.

**Files.**
- NEW `voice/metrics.py` — ~60-line timing harness.
- EDIT `voice/jarvis_slim.py` — wrap each `handle_tool` branch.
- NEW `eval/plan1_baseline.py` — scripted exercise (NOT a test runner,
  just a script that calls `handle_tool` directly with known inputs and
  prints the resulting JSONL).
- NEW `eval/plan1_baseline.md` — human-readable table produced from the
  JSONL run.

**Design — `voice/metrics.py`.**
A single-purpose append-only JSONL logger, no dependencies beyond the
stdlib. API:

```python
from voice.metrics import timed, log_event

with timed("browse.ensure_browser"):
    ensure_browser()

log_event("handle_tool", action="browse", query_len=14,
          duration_ms=4820, cold=True, ok=True)
```

Output path: `~/.nexus/metrics/handle_tool.jsonl`. One line per event.
Fields: `ts`, `action`, `phase`, `query_len`, `duration_ms`, `cold`,
`ok`, `error`. The `phase` field lets us instrument sub-steps within a
single action (e.g. `browse.ensure_browser`, `browse.claude_subprocess`,
`browse.result_format`) and later reconstruct the waterfall.

**Sub-phases to instrument (non-negotiable).**

| Action | Sub-phases to time |
|---|---|
| `browse` / `search` | `ensure_browser`, `claude_subprocess_spawn`, `claude_first_token`, `claude_total`, `result_truncate` |
| `calendar`/`email`/`reminders` | `sync_subprocess`, `file_read`, `truncate` |
| `briefing` | `sync_subprocess`, `file_read`, `truncate` |
| `documents` | `walk`, `scan`, `format` |
| `window` | `list_windows`, `match`, `applescript_call` |
| `code` | `project_lookup`, `session_format` |
| `github` | `gh_subprocess` |

Cold vs warm: a run is "cold" if the cache/daemon/subprocess it depends
on is not yet initialized in this process's lifetime. `metrics.py`
exposes a `mark_cold_warm(label)` helper that flips a flag on first use.

**Exercise script — `eval/plan1_baseline.py`.**
Directly imports `handle_tool` and calls it with a fixed set of inputs,
twice per input (first = cold, second = warm). The inputs are chosen to
be safe to run offline and not mutate state:

1. `window` → `"list"`
2. `window` → `"move chrome left"` (only if Chrome is open; else skip)
3. `briefing` → `""`
4. `calendar` → `""`
5. `email` → `""`
6. `reminders` → `""`
7. `documents` → `"drone"` (or another known-hit term)
8. `github` → `""`
9. `browse` → `"what is the weather in barcelona"`
10. `code` → `"nexus"` (list sessions; do not stage handoff)

The script prints a table to stdout and also appends all events to the
JSONL log. Cold/warm columns are computed post-hoc from the JSONL.

**Produce `eval/plan1_baseline.md`.**
One-page report with: a latency table per action (cold, warm, p50/p95
if N>5), a sub-phase breakdown for the three slowest actions, a list of
anomalies (timeouts, errors, drops), and a ranked "biggest wins
available" list that drives the order of Phases 1B-1G.

**Acceptance criteria.**
- Every action in `handle_tool` emits at least one `log_event` per call.
- Baseline run completes without errors across all 10 inputs.
- `plan1_baseline.md` is committed and contains the numbers.
- Instrumentation overhead per call <5ms (spot-check by timing a no-op
  `log_event`).

**Risks.**
- Instrumentation that holds the GIL or blocks on I/O would itself
  distort the measurements. Mitigation: `log_event` opens, writes, and
  closes the file per call (millisecond-scale on SSD) OR buffers in
  memory and flushes on shutdown. Pick buffered; dump on SIGTERM/exit.
- The exercise script runs outside Gemini Live, which means the `query`
  strings aren't real Gemini outputs. That's fine — we're measuring the
  Python side only. Plan 2 covers end-to-end.

**Out of scope for 1A.** Any fix. This phase only measures.

---

## Phase 1B — Browser pre-warm on app start

**Goal.** Remove cold-start latency on the first `browse`/`search` call
of the day. Currently `ensure_browser()` is called synchronously inside
`handle_tool` on first use, which launches Playwright + Chromium +
profile — typically 2-5 seconds. Moving this to app startup turns the
first user query into a warm call.

**Files.**
- EDIT `voice/jarvis_slim.py` — start a background pre-warm task in
  `main()` before the Gemini Live session opens.
- EDIT `voice/browser.py` — expose an idempotent `ensure_browser()`
  that's safe to call repeatedly and returns instantly once started.
  (Already mostly there — confirm `_browser_ready.wait()` semantics.)

**Design.**
In `jarvis_slim.py:main()`, immediately after `print_budget()`, fire:

```python
async def _prewarm():
    await asyncio.to_thread(ensure_browser)
asyncio.create_task(_prewarm())
```

The task runs concurrently with Gemini session init. By the time the
user finishes their first sentence, the browser is up. If pre-warm
errors (no display, profile locked, etc.), log and continue — the first
browse call falls back to the existing synchronous path and reports the
error to Gemini as it does today.

**Acceptance criteria.**
- Cold first-call `browse` latency (measured via Phase 1A
  instrumentation) drops to the warm-call latency ± 100ms.
- App startup to "ready to talk" does not regress (pre-warm must not
  block the main loop).
- If browser pre-warm fails, the app still starts and voice still
  works; `browse` returns the same error it does today.

**Risks.**
- Browser window visible on screen at launch — this is actually desired
  per README ("ghost browsing"), so it's fine. Confirm with Ginés that
  the window appearing at launch is acceptable; if not, consider
  starting Chromium minimized and `raise_window`-ing on first use.

**Out of scope.** Any change to nav.py or the inner Claude agent.

---

## Phase 1C — Cache-first briefing with background sync

**Goal.** Make `briefing` / `calendar` / `email` / `reminders` return
instantly from the cached markdown, while the sync runs in the
background for the *next* call. Currently every call runs
`sync_all.py` synchronously before reading the file — 2-10 seconds of
dead air on every management query, even though the underlying data
rarely changes more than once per few minutes.

**Files.**
- EDIT `voice/jarvis_slim.py` — `_sync_management`, `handle_tool`
  branches for `calendar`/`email`/`reminders`/`briefing`.
- NEW tiny state file OR in-process dict: last-sync timestamp per
  source. In-process is fine — survives for the session, re-syncs on
  next app start.

**Design — cache-first pattern.**

```python
_LAST_SYNC: dict[str, float] = {}
_SYNC_TTL = 120  # seconds — if data is fresher than this, don't sync
_SYNC_LOCK: dict[str, asyncio.Lock] = {}

def _maybe_sync(source: str):
    """
    Return the cached file immediately; kick off a background sync if
    the cache is older than TTL and no sync is already running.
    """
    path = os.path.join(MANAGEMENT_ROOT, f"{source}.md")
    data = _read_file(path)
    age = time.time() - _LAST_SYNC.get(source, 0)

    if not data:
        # First-ever call on this machine — must sync synchronously,
        # there's nothing to return.
        _sync_management(source)
        _LAST_SYNC[source] = time.time()
        return _read_file(path)

    if age > _SYNC_TTL:
        asyncio.get_event_loop().run_in_executor(
            None, _sync_then_mark, source
        )
    return data
```

`_sync_then_mark` runs the sync and updates `_LAST_SYNC` on success.
Background syncs are coalesced per-source via a simple "already
running" flag to prevent pile-up.

**Important invariant.** Ginés explicitly wants "email today-filtered
and 400-char briefing cap" preserved — that logic lives in
`build_management.py` and is not touched by this phase. We are changing
*when* sync runs, not *what* sync produces.

**Acceptance criteria.**
- Second and later calls to any management action return in <100ms
  (file-read only).
- First call on a fresh machine still returns correct data (falls
  through to synchronous sync).
- If the background sync fails, the next call still returns the
  previous cached data. No empty string ever reaches Gemini when a
  cached copy exists.
- TTL tunable via a constant at the top of `jarvis_slim.py`.

**Risks.**
- Stale data feel: user asks "anything new?" and gets a 2-minute-old
  reply. Mitigation: 120s TTL is short; for critical freshness we can
  drop it to 30s. Flag for Ginés in `plan1_baseline.md`.
- Event-loop gotcha: `handle_tool` runs in `asyncio.to_thread`, which
  means `asyncio.get_event_loop()` inside it may not be the main loop.
  Fix: pass the loop in as a global set during `main()`, or use
  `asyncio.run_coroutine_threadsafe` explicitly.

**Out of scope.** Changing what the sync produces; rewriting
`build_management.py`.

---

## Phase 1D — Ack-before-await

**Goal.** Eliminate dead air on slow actions. The user currently gets
*zero* audible feedback between the end of their utterance and Gemini
speaking the tool result. For `browse`/`search`/`code`/`documents`,
that gap can be 3-10 seconds of silence. Unacceptable for "human feel."

**Constraint.** We cannot speak through Gemini before the tool returns
— the tool_response is single-shot and the prompt is frozen for Plan 1.
The only channel available is local macOS `say`, same as the existing
TTS-bypass pattern.

**Design.**
Introduce per-action ack lines, spoken locally *before* the handler
runs. The ack is kicked off as a subprocess (non-blocking) so the
handler starts immediately in parallel.

```python
ACK_LINES = {
    "browse":   "On it.",
    "search":   "Searching now.",
    "documents": "Looking.",
    "code":     "Getting your sessions.",
    "briefing": "Here's your briefing.",
    "calendar": "Checking your calendar.",
    "email":    "Checking email.",
    "reminders": "Checking reminders.",
}

def _speak_ack(action: str):
    line = ACK_LINES.get(action)
    if not line:
        return
    subprocess.Popen(["say", "-r", "200", line],
                     stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
```

Called at the very top of `handle_tool`, before any branch. Non-
blocking `Popen`, no `wait()`. The subprocess speaks while the handler
works. When the handler finishes and Gemini speaks the result (or the
TTS bypass speaks the long result), the ack has usually already
finished. If they overlap briefly, that's acceptable — it sounds like
an agent interrupting itself to give news, which is natural.

**Tuning.**
- Short actions (<800ms) should NOT ack: `window`, `github`, trivial
  `documents` hits. Adding an ack to a fast action makes it *slower*
  and chattier. Rule: only ack actions whose baseline warm latency
  (from Phase 1A) is ≥1s.
- Ack lines are 2-4 words each. Any longer and they overlap the
  result.
- No hardcoded app names in ack lines. Rule #5 from JARVIS_GUIDE.

**Acceptance criteria.**
- For every slow action, audible ack within ~400ms of tool_call
  receipt (measured end-to-end with a stopwatch; doesn't need JSONL).
- Ack never speaks twice for the same call.
- Ack never blocks `handle_tool`.
- No ack on fast actions.

**Risks.**
- macOS `say` cold start is ~150-300ms itself. Acceptable. If this
  bites, pre-warm `say` once at app start with an empty string.
- Ack audio competes with Gemini audio through the same speaker. macOS
  audio stack handles the mix; if there's a clash, Gemini's voice
  usually wins (louder). Monitor in Plan 3 when real audio is on.
- The background `say` subprocess could orphan on crash. Acceptable —
  it self-terminates in <2s.

**Out of scope.** Routing the ack through Gemini; varying ack per
query; adding ack to general-knowledge answers (that's Gemini's job).

---

## Phase 1E — Trigger-word hard gate (structural, in Python)

**Goal.** Stop Gemini from calling `do(…)` when the user did not say a
trigger word. This is Ginés's #1 stated annoyance and it cannot be
fixed reliably with prompt edits (already tried, per memory).

**Pre-requisite.** Gemini Live must surface the user's transcript so
Python can gate on it. This is not enabled today. It requires one
config-flag change:

```python
config = types.LiveConnectConfig(
    ...,
    input_audio_transcription=types.AudioTranscriptionConfig(),
    ...
)
```

After this, each `msg` in the receive loop carries
`msg.server_content.input_transcription.text` incrementally. We
accumulate the current turn's transcript in a rolling buffer keyed by
turn start.

**Design — the gate.**

```python
TRIGGER_TOKENS = {"nexus", "jarvis"}  # editable constant
ACTION_GATE = {
    "browse", "search", "documents", "window", "code",
    "briefing", "calendar", "email", "reminders", "github",
}
# 'sleep' is NOT gated — it's meta, user should be able to say "sleep"
# without a trigger word.

def _transcript_has_trigger(transcript: str) -> bool:
    t = (transcript or "").lower()
    return any(tok in t for tok in TRIGGER_TOKENS)

# In the receive loop, before dispatching:
if action in ACTION_GATE and not _transcript_has_trigger(current_turn_transcript):
    result = "No trigger word heard."
    is_long = False
    logger.warning(f"Gate blocked action={action} transcript={current_turn_transcript!r}")
else:
    result, is_long = await asyncio.to_thread(handle_tool, ...)
```

The gate sits in `jarvis_slim.py`'s receive loop, NOT in `handle_tool`
— we want `handle_tool` to stay callable from `eval/plan1_baseline.py`
and later eval harnesses without the gate interfering.

**Correctness notes.**
- The transcript buffer resets on turn boundary (Gemini signals end of
  turn via `msg.server_content.turn_complete`).
- If transcription is empty (e.g. Gemini didn't ship the transcript in
  time), fall back to **allow** the call. Blocking on missing
  transcripts would be worse than the current behavior.
- Trigger-word detection is substring-match, case-insensitive,
  whitespace-tolerant. "Hey Jarvis", "nexus please", "um, jarvis" all
  match. Fancier wake-word detection is out of scope (that's the
  openwakeword port mentioned in known limitations).
- `sleep` is allowed ungated so "go to sleep" still works without
  saying "jarvis, go to sleep."

**Acceptance criteria.**
- A transcript of "what's the capital of Bolivia" never triggers a
  tool call, even if Gemini tries to call one.
- A transcript of "jarvis, search for drone lidar" correctly dispatches
  `do(action="search", query="drone lidar")`.
- Blocked calls are logged at WARNING so Plan 2 can quantify the false-
  positive rate.
- Gate logic is self-contained in a helper function and unit-testable
  without Gemini (pure string check).

**Risks.**
- **If input transcription lags behind the tool_call**, the gate will
  see an empty transcript and fall through to "allow." This is the
  fallback-open policy and it's deliberate — we'd rather miss a block
  than block a legitimate call. Quantify in Plan 2.
- **Transcription itself has cost.** Gemini Live's input transcription
  adds a small amount of websocket traffic. Negligible but worth
  noting. Not a Plan 1 concern.
- **The phrase "nexus" appearing in the query itself** (e.g. "tell me
  about Nexus 7") correctly passes the gate. Intended.

**Out of scope.** Wake-word detection, per-action trigger tokens,
"trigger cooldown" timers. Keep the first version dumb and measurable.

---

## Phase 1F — Browser / nav robustness

**Goal.** Fix the specific Playwright failures Ginés named: "it
sometimes does not know where to find elements that are under
elements," and "a lot of time passes from browser loaded to the actual
search." Do NOT rewrite `nav.py` — make targeted fixes to
`browser.py`'s command handlers.

**Files.**
- EDIT `voice/browser.py` — `_execute_command` click + state handlers.
- Possibly NEW small helper in `browser.py` for "click by best
  strategy" (role → text → evaluate dispatch).

**Specific fixes.**

1. **`state` is slow.** The current `state` command runs a ~40-line JS
   payload via `page.evaluate`. Fine in isolation but fires on every
   nav step. Add a result cache keyed on `page.url + DOM mutation
   count` so repeated `state` calls inside one navigation return
   instantly. Invalidate on `goto`/`click`/`press`/`scroll`.

2. **`click` fallback ladder is too short.** Currently:
   `get_by_text → get_by_role(link) → get_by_role(button) → fail`.
   Add two more rungs:
   - `page.get_by_label(text)` for form labels / aria-labels.
   - `page.evaluate` dispatch click on the best-matching element when
     the locator can't reach it because it's covered (e.g. behind a
     cookie banner). The JS fallback uses
     `document.elementFromPoint` + `.click()` and bypasses overlay
     interception. This is the "elements under elements" fix.

3. **`goto` waits on `domcontentloaded`** which is correct, but does
   not wait for the search box to appear on sites like Google. Add an
   optional `selector` arg to `goto` that waits for a specific
   element; nav.py can pass it when it knows what it's going for.
   This is the "browser loaded but search hasn't started" fix.

4. **Pre-warm `about:blank`.** On browser start, navigate the single
   page to `about:blank` explicitly. Chromium's default "new tab page"
   does DNS prefetch and other chatter that delays the first `goto`.
   Already partly handled, verify.

**Acceptance criteria.**
- `click` succeeds on a page with a visible cookie banner (test on
  a real site with a consent banner).
- `state` second call on the same page returns in <20ms (measured).
- `goto` + first search interaction on google.com is ≥1s faster than
  baseline when the selector wait is provided.
- No existing browse test regresses.

**Risks.**
- JS click fallback can click the wrong element if elementFromPoint
  resolves to a different node than the locator. Mitigation: only use
  JS fallback when the locator fails with a "intercepts pointer
  events" error, not as the primary path.
- DOM mutation count for cache invalidation is non-trivial. Simpler:
  cache for 500ms, invalidate on any command that changes page state.

**Out of scope.** Rewriting nav.py's interface; adding new nav
commands; changing the inner Claude agent's prompt.

---

## Phase 1G — Long-result TTS correctness

**Goal.** Fix `tts_speak_long` silently truncating briefings at 500
chars. Currently if the briefing is 1800 chars, the user hears the
first ~500 and never knows the rest exists. That's a correctness bug,
not a performance one — and it's invisible until you notice.

**Files.**
- EDIT `voice/jarvis_slim.py` — `tts_speak_long` and callers.

**Design.**
Three small changes, in order of importance:

1. **Stop silently truncating.** Current:
   ```python
   short = text[:500]
   subprocess.run(["say", ...])
   ```
   Replace with: pipe the full text into `say` via stdin, not argv.
   macOS `say` has no fixed length limit when reading from stdin; the
   argv cap is the issue.
   ```python
   proc = subprocess.Popen(["say", "-r", "200"], stdin=subprocess.PIPE)
   proc.communicate(input=text.encode("utf-8"), timeout=180)
   ```

2. **Respect the existing truncation already done upstream.** The
   callers that set `is_long=True` already slice to 3000 chars
   (`data[:3000]`). That's the correct boundary because it's measured
   against `build_management.py`'s caps. `tts_speak_long` should trust
   its input and not double-slice.

3. **Interruptibility.** If the user starts talking mid-briefing,
   the `say` subprocess should die. Track the current `say` Popen in
   a module-level var; kill it on next tool_call or next Gemini audio
   chunk. First-pass: kill on any new tool_call. Audio-chunk-based
   interruption is a Plan 3 concern.

**Acceptance criteria.**
- A 2500-char briefing is read aloud in full, not truncated.
- A user-interrupting "stop" (followed by another tool_call) kills
  the in-flight `say` within 200ms.
- No regression on short tool results (they don't go through
  `tts_speak_long`).

**Risks.**
- Killing `say` mid-sentence leaves the audio stack in an awkward
  state for a few hundred ms. Acceptable.
- The `180s` timeout is long but intentional — some briefings are
  long. Document the cap in a comment.

**Out of scope.** Replacing `say` with Google Cloud TTS; summarizing
long results before speaking (that's a later hybridization question).

---

## Phase 1H — Re-baseline & exit report

**Goal.** Prove Plan 1 moved the numbers in the right direction, and
hand Plan 2 a clean baseline to measure AI routing against.

**Files.**
- EDIT `eval/plan1_baseline.py` — run it again unchanged.
- NEW `eval/plan1_final.md` — before/after comparison.

**Method.**
Re-run the exercise script. Produce `plan1_final.md` with:
- Per-action cold and warm latency, before vs after.
- Sub-phase breakdown for the three biggest wins.
- List of still-open latency items with rationale (e.g. "Claude
  subprocess cold start ~1.5s — cannot fix in Plan 1, lives in Plan
  2's hybridization work").
- Confirmation that the trigger gate fires on a known blocked case and
  passes a known good case.
- Sanity check that no action regressed by more than 10% warm latency.

**Acceptance criteria for Plan 1 as a whole.**
1. Every action emits instrumentation.
2. Cold first `browse` call ≤ warm `browse` call + 300ms. (Pre-warm
   works.)
3. Second and later management calls return in <100ms. (Cache-first
   works.)
4. Slow actions ack within 400ms of tool_call. (Ack-before-await
   works.)
5. Trigger gate blocks a scripted "capital of Bolivia" case and
   passes a scripted "jarvis, search…" case.
6. `tts_speak_long` reads a 2500-char briefing in full.
7. No action's warm latency regresses by >10%.
8. `plan1_final.md` committed with before/after numbers.

If all eight hold, Plan 1 is done and Plan 2 is unblocked.

---

## What Plan 1 is NOT doing (and why)

- **Not touching the system prompt.** The prompt is one of the most
  load-bearing and regression-prone surfaces in the repo. Prompt
  changes belong in Plan 2, measured against the Plan 1 baseline.
- **Not touching the `do` schema.** Same reason.
- **Not adding new actions.** News feed, screenshot helper, window Q&A
  — all post-Plan 3.
- **Not replacing `say` with Google Cloud TTS.** The voice-quality
  question is a Plan 3 concern.
- **Not optimizing the Claude subprocess cold start.** The `claude
  --print` invocation takes ~1.5s to spin up before it does any
  reasoning; this is a hybridization question (Haiku middle-tier?
  persistent Claude server?) and belongs in Plan 2.
- **Not unifying error handling.** Handlers all stringify errors
  differently. Cosmetic; fix later.

---

## Commit plan (one commit per phase)

1. `plan1: instrument handle_tool with metrics.py + baseline script`
2. `plan1: pre-warm browser on app start`
3. `plan1: cache-first management reads with background sync`
4. `plan1: ack-before-await for slow actions`
5. `plan1: trigger-word hard gate via input transcription`
6. `plan1: browser nav robustness (click fallback, state cache, goto selector)`
7. `plan1: fix tts_speak_long truncation and interrupt`
8. `plan1: re-baseline, plan1_final.md`

Each commit stands alone, reverts cleanly, and has numbers in the
message.
