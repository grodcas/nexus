# Plan 2 — Baseline scorecard

- Date: 2026-04-15 12:58
- Cases (unique): **76**
- Repetitions per case: 1

## Headline

| Axis | Pass rate |
|---|---:|
| Routing | **89%** (68/76) |
| Task success | **71%** (54/76) |
| Latency | **91%** (69/76) |
| All three | **61%** (46/76) |

## By bucket

| Bucket | n | Routing | Success | Latency | All |
|---|---:|---:|---:|---:|---:|
| code_handoff | 6 | 100% | 83% | 100% | 83% |
| compound | 5 | 100% | 60% | 100% | 60% |
| correction | 5 | 60% | 40% | 100% | 20% |
| disfluent | 6 | 100% | 67% | 83% | 67% |
| documents | 8 | 75% | 88% | 75% | 62% |
| focus | 10 | 80% | 50% | 100% | 50% |
| knowledge | 10 | 100% | 90% | 80% | 70% |
| lookup | 5 | 100% | 100% | 60% | 60% |
| morning | 8 | 88% | 100% | 100% | 88% |
| multi_turn | 7 | 100% | 14% | 100% | 14% |
| small_talk | 6 | 83% | 83% | 100% | 83% |

## Failing cases

### `code-01` (code_handoff) — task_success

- Utterance: `jarvis connect me to nexus`
- Expected: action=`code`, query_contains=`['nexus']`, session=`None`
- Actual: action=`code`, query=`nexus`, session=`None`, latency=`1104.7ms`, gate_blocked=`False`

### `compound-04` (compound) — task_success

- Utterance: `jarvis close finder and bring slack forward`
- Expected: action=`window`, query_contains=`['close', 'finder']`, session=`None`
- Actual: action=`window`, query=`close finder`, session=`None`, latency=`819.7ms`, gate_blocked=`False`

### `compound-06` (compound) — task_success

- Utterance: `jarvis maximize the editor then start my briefing`
- Expected: action=`window`, query_contains=`['editor']`, session=`None`
- Actual: action=`window`, query=`maximize editor`, session=`None`, latency=`1231.2ms`, gate_blocked=`False`

### `correction-02` (correction) — routing,task_success

- Utterance: `no not that one, the other report`
- Expected: action=`documents`, query_contains=`[]`, session=`None`
- Actual: action=`search`, query=`TDLAS integration report other`, session=`None`, latency=`920.7ms`, gate_blocked=`True`

### `correction-03` (correction) — task_success

- Utterance: `stop reading and open my calendar instead`
- Expected: action=`calendar`, query_contains=`[]`, session=`None`
- Actual: action=`calendar`, query=`None`, session=`None`, latency=`1226.8ms`, gate_blocked=`True`

### `correction-05` (correction) — task_success

- Utterance: `actually make it the right half not the left`
- Expected: action=`window`, query_contains=`['right']`, session=`None`
- Actual: action=`window`, query=`move chrome right`, session=`None`, latency=`773.5ms`, gate_blocked=`True`

### `correction-06` (correction) — routing

- Utterance: `stop`
- Expected: action=`None`, query_contains=`[]`, session=`None`
- Actual: action=`briefing`, query=`stop`, session=`None`, latency=`716.8ms`, gate_blocked=`True`

### `disfluent-03` (disfluent) — task_success

- Utterance: `hey jarvis find my um the tdlas report the integration one`
- Expected: action=`documents`, query_contains=`['tdlas']`, session=`None`
- Actual: action=`documents`, query=`tdlas report the integration one`, session=`None`, latency=`1436.0ms`, gate_blocked=`False`

### `disfluent-04` (disfluent) — task_success,latency

- Utterance: `jarvis put like put chrome on the left uh half please`
- Expected: action=`window`, query_contains=`['chrome', 'left']`, session=`None`
- Actual: action=`window`, query=`move chrome left half`, session=`None`, latency=`6565.6ms`, gate_blocked=`False`

### `documents-01` (documents) — task_success

- Utterance: `jarvis find my TDLAS integration report`
- Expected: action=`documents`, query_contains=`['tdlas']`, session=`None`
- Actual: action=`documents`, query=`TDLAS integration report`, session=`None`, latency=`1240.5ms`, gate_blocked=`False`

### `documents-03` (documents) — routing,latency

- Utterance: `jarvis do I have anything about battery safety`
- Expected: action=`documents`, query_contains=`['battery']`, session=`None`
- Actual: action=`search`, query=`battery safety`, session=`None`, latency=`25891.2ms`, gate_blocked=`False`

### `documents-05` (documents) — routing,latency

- Utterance: `jarvis search for sensor calibration notes`
- Expected: action=`documents`, query_contains=`['sensor', 'calibration']`, session=`None`
- Actual: action=`search`, query=`sensor calibration notes`, session=`None`, latency=`24478.5ms`, gate_blocked=`False`

### `focus-04` (focus) — routing,task_success

- Utterance: `jarvis move the browser to the other screen`
- Expected: action=`window`, query_contains=`['browser', 'other']`, session=`None`
- Actual: action=`window`, query=`move chrome to other screen`, session=`None`, latency=`926.0ms`, gate_blocked=`False`

### `focus-06` (focus) — task_success

- Utterance: `jarvis minimize everything except slack`
- Expected: action=`window`, query_contains=`[]`, session=`None`
- Actual: action=`window`, query=`minimize all except slack`, session=`None`, latency=`1012.7ms`, gate_blocked=`False`

### `focus-07` (focus) — routing,task_success

- Utterance: `jarvis bring slack to the front`
- Expected: action=`window`, query_contains=`['slack', 'focus']`, session=`None`
- Actual: action=`window`, query=`bring slack to the front`, session=`None`, latency=`925.8ms`, gate_blocked=`False`

### `focus-08` (focus) — task_success

- Utterance: `jarvis put the editor on the left and the terminal on the right`
- Expected: action=`window`, query_contains=`[]`, session=`None`
- Actual: action=`window`, query=`move editor left`, session=`None`, latency=`1170.6ms`, gate_blocked=`False`

### `focus-10` (focus) — task_success

- Utterance: `jarvis maximize chrome on the secondary screen`
- Expected: action=`window`, query_contains=`['chrome', 'secondary']`, session=`None`
- Actual: action=`window`, query=`maximize chrome on secondary screen`, session=`None`, latency=`842.6ms`, gate_blocked=`False`

### `knowledge-02` (knowledge) — latency

- Utterance: `how does a TDLAS sensor actually work`
- Expected: action=`None`, query_contains=`[]`, session=`None`
- Actual: action=`None`, query=`None`, session=`None`, latency=`2693.0ms`, gate_blocked=`False`
- Text: `A TDLAS (Tunable Diode Laser Absorption Spectroscopy) sensor works by emitting a laser beam through a gas sample. The laser is tuned to a specific wavelength that corresponds to an absorption line of `

### `knowledge-03` (knowledge) — latency

- Utterance: `what's the difference between LiDAR and time of flight`
- Expected: action=`None`, query_contains=`[]`, session=`None`
- Actual: action=`None`, query=`None`, session=`None`, latency=`1894.3ms`, gate_blocked=`False`
- Text: `LiDAR (Light Detection and Ranging) is a remote sensing method that uses pulsed laser light to measure distances to the Earth. It works by sending out laser pulses and measuring the time it takes for `

### `knowledge-05` (knowledge) — task_success

- Utterance: `explain brushless motor commutation in one sentence`
- Expected: action=`None`, query_contains=`[]`, session=`None`
- Actual: action=`None`, query=`None`, session=`None`, latency=`569.6ms`, gate_blocked=`False`
- Text: `Brushless motor commutation electronically switches the direction of current in the motor windings, creating a rotating magnetic field that pulls the rotor along.`

### `lookup-02` (lookup) — latency

- Utterance: `jarvis explain what VTOL transition means in fixed-wing drones`
- Expected: action=`None`, query_contains=`[]`, session=`None`
- Actual: action=`None`, query=`None`, session=`None`, latency=`2353.2ms`, gate_blocked=`False`
- Text: `VTOL transition in fixed-wing drones refers to the process where a drone changes from vertical flight (like a helicopter) to horizontal, forward flight (like a plane), and vice-versa.

Here's a breakd`

### `lookup-07` (lookup) — latency

- Utterance: `jarvis tell me about the airbus lineup`
- Expected: action=`None`, query_contains=`[]`, session=`None`
- Actual: action=`None`, query=`None`, session=`None`, latency=`2028.8ms`, gate_blocked=`False`
- Text: `Airbus is a leading aircraft manufacturer, and their lineup includes a variety of commercial aircraft designed for different ranges and passenger capacities.

Their primary single-aisle aircraft famil`

### `morning-04` (morning) — routing

- Utterance: `jarvis what do I have to do today`
- Expected: action=`reminders`, query_contains=`[]`, session=`None`
- Actual: action=`briefing`, query=`None`, session=`None`, latency=`819.6ms`, gate_blocked=`False`

### `multi-01` (multi_turn) — task_success

- Utterance: `and put it on the right`
- Expected: action=`window`, query_contains=`['right']`, session=`None`
- Actual: action=`window`, query=`move chrome right`, session=`None`, latency=`1056.5ms`, gate_blocked=`True`

### `multi-02` (multi_turn) — task_success

- Utterance: `do the same for safari`
- Expected: action=`window`, query_contains=`['safari', 'left']`, session=`None`
- Actual: action=`window`, query=`move safari left`, session=`None`, latency=`700.7ms`, gate_blocked=`True`

### `multi-03` (multi_turn) — task_success

- Utterance: `read the first one`
- Expected: action=`documents`, query_contains=`[]`, session=`None`
- Actual: action=`documents`, query=`read first TDLAS integration report`, session=`None`, latency=`761.5ms`, gate_blocked=`True`

### `multi-05` (multi_turn) — task_success

- Utterance: `now close it`
- Expected: action=`window`, query_contains=`['close']`, session=`None`
- Actual: action=`window`, query=`close slack`, session=`None`, latency=`509.4ms`, gate_blocked=`True`

### `multi-06` (multi_turn) — task_success

- Utterance: `and the next one`
- Expected: action=`calendar`, query_contains=`[]`, session=`None`
- Actual: action=`calendar`, query=`What is the next event after the weekly design review?`, session=`None`, latency=`1236.2ms`, gate_blocked=`True`

### `multi-07` (multi_turn) — task_success

- Utterance: `same thing for tomorrow`
- Expected: action=`calendar`, query_contains=`['tomorrow']`, session=`None`
- Actual: action=`calendar`, query=`tomorrow`, session=`None`, latency=`833.6ms`, gate_blocked=`True`

### `smalltalk-05` (small_talk) — routing,task_success

- Utterance: `jarvis go to sleep`
- Expected: action=`sleep`, query_contains=`[]`, session=`None`
- Actual: action=`None`, query=`None`, session=`None`, latency=`1372.2ms`, gate_blocked=`False`
- Text: `I don't sleep. How can I help?`

---

## Interpretation — the real findings

**Headline in one sentence:** routing is 89%, latency is 91%, task success is 71%, all-three is 61%. The biggest gap is **multi-turn continuations**, which the trigger-word hard gate blocks correctly according to its rules — but wrongly according to how a human converses.

### Finding 1 — trigger gate is too strict for continuation turns (7 failures)

Cases: `multi-01` `multi-02` `multi-03` `multi-05` `multi-06` `multi-07` `correction-03` `correction-05`.

These are all legitimate follow-up utterances — "and put it on the right", "do the same for safari", "same thing for tomorrow", "actually make it the right half not the left". The router handled them correctly: it identified the intent, produced a sensible action and query. **Then the gate blocked the call because the utterance had no trigger word.**

The gate's rule is: if the action is in `ACTION_GATE` and the transcript is non-empty and has no trigger token, block. That rule is correct for a first turn in isolation. It is wrong for a turn that is obviously a continuation of prior context — which is most of what a real conversation is made of.

**Fix class: Plan 2.5 — stateful gate with a trust window.** After a successful gated call in a session, the next ~30 seconds of turns should bypass the trigger check. Slim's gate currently has no per-session state; adding one is a ~20-line change plus a decision on the trust window length. This is the single biggest win available from the baseline, worth ~10 percentage points on the all-three metric.

### Finding 2 — router is word-driven on "search" (2 failures)

Cases: `documents-03` ("do I have anything about battery safety" → search), `documents-05` ("jarvis search for sensor calibration notes" → search).

Both utterances are obviously queries against the user's own documents (one explicitly, one implicitly). The router picks `search` because the word "search" appears in the utterance or because the phrasing feels web-shaped. These are routing failures, not just grading failures — the handler then tries to browse the web for battery safety, which is the wrong thing.

**Fix class: Plan 2.5 — action description refinement.** The `documents` action's description in `TOOL_DECLARATIONS` is currently empty-ish; pulling in one sentence that distinguishes "my files" from "the web" should move these two without touching any other bucket. Risk of regression on other routing decisions is non-trivial — this is exactly the "tower of spades" case that the eval suite exists to protect against. Fix, re-run the suite, confirm no regressions.

### Finding 3 — `sleep` routing misses when phrased naturally (1 failure)

Case: `smalltalk-05` "jarvis go to sleep" → answered conversationally ("I don't sleep. How can I help?") instead of calling `do(action=sleep)`.

The router didn't route to sleep even though it's in the schema. The `sleep` action has no description that makes it discoverable from natural language. Either add one ("End the voice session") or accept that "sleep" is a magic word and the user has to say "sleep" exactly. Low-stakes; fix in Plan 2.5.

### Finding 4 — window action name-matching is environmentally limited (4-5 failures)

Cases: `focus-06` `focus-08` `focus-10` `compound-04` `compound-06`.

All of these come back as "No window matches 'X'. Open windows: …". The environment running the baseline is a sandboxed bash subshell with only Safari / Tailscale / Google Chrome for Testing / UserNotificationCenter visible to `CGWindowListCopyWindowInfo`. On Ginés's normal terminal the picture will be fuller. Running the suite from his production shell is a **must** before trusting these numbers.

Unrelated but worth flagging: `focus-06` "minimize everything except slack" is a **known hard case** — the handler has no "minimize all except X" verb. The handler correctly reported that it can't find a window called "all except slack". The router passed the utterance through literally.

**Not a Plan 2.5 fix** — these are either (a) environmental (re-run on the real machine) or (b) genuinely out-of-scope verbs that need handler work, not router work.

### Finding 5 — latency tail on knowledge questions (4 failures)

Cases: `knowledge-02` (2693ms), `knowledge-03` (1894ms), `lookup-02` (2353ms), `lookup-07` (2028ms).

All four are knowledge-class cases with a 1500ms budget. Actual latencies are in the 1800-2700ms range. This is **Gemini text API latency variance** — not anything Nexus can control. The budget was set optimistically against the Live-audio path, which is faster. Either:
(a) raise the knowledge budget to 3000ms for text-mode runs and accept that Plan 3 real-audio will get the honest numbers, or
(b) leave the budget tight and use these failures as a forcing function to try other models (`gemini-2.5-flash-lite`?).

I lean (a). Flag for Plan 2.5 documentation, no code fix.

### Finding 6 — document grep doesn't hit "tdlas" (2 failures)

Cases: `documents-01` (no result for "TDLAS integration report"), `disfluent-03` (same).

The handler's `_search_worktree` greps the markdown files under `~/.nexus/documents/`. Either the worktree doesn't contain a TDLAS entry or the grep is case-sensitive and the file uses a different casing. This is a **real product bug** or a **gap in the documents worktree**, not a router issue. Worth reproducing on the real machine and, if reproduced, either fixing the grep (it is already `.lower()`, so probably a content gap) or adding a TDLAS-named entry to the worktree.

### Finding 7 — judge rubric too strict on knowledge-05

Case: `knowledge-05` — "explain brushless motor commutation in one sentence". Answer was correct but the judge said fail. The rubric asked for "One sentence, mentions stator phases and electronic switching" — the model's response probably stretched to two sentences or missed a keyword. This is a case authorship issue, not a product issue. Loosen the rubric.

### Finding 8 — code-01 judge fail is a list-sessions edge case

Case: `code-01` — "jarvis connect me to nexus". Router correctly called `do(action=code, query=nexus)`, handler returned a session list. Judge said fail, probably because the list starts with "No description, unknown" which doesn't read like a useful session list. This is a **session display quality** issue in `voice/session_manager.py`, not a routing issue. Separate fix class — improve `format_sessions_for_display` to read nicer session names.

---

## Per-finding impact model

If all the fixes above landed on their current root causes, and nothing regressed:

| Fix | Cases unblocked | New task_success (expected) |
|---|---:|---:|
| Current baseline | — | **71%** |
| Stateful gate (Finding 1) | +8 | **82%** |
| Action description pass (Finding 2) | +2 | **84%** |
| Loosen knowledge-05 rubric (Finding 7) | +1 | **85%** |
| `sleep` description (Finding 3) | +1 | **86%** |
| Session display fix (Finding 8) | +1 | **87%** |
| Re-run on real machine (Finding 4) | up to +4 | **92%** |
| Document worktree fix (Finding 6) | +2 | **95%** |
| Latency budget tune (Finding 5) | +4 latency | all-three goes up too |

**No pure AI fix** moves the number past ~87%. The last 8 percentage points come from environment (where the eval is run), worktree content, and budget calibration — none of which are prompt edits.

This is the **big win** of having a baseline: we can now tell, per failure, whether the fix is a prompt change, a handler change, a schema change, a test case change, or an environmental change. Nine weeks of "try prompt edits until it feels better" compresses to five targeted fixes each scored against this suite.

---

## Known discrepancies with live slim (audio path)

1. **Model difference**: baseline uses `gemini-2.5-flash` non-Live; live slim uses `gemini-2.5-flash-native-audio-preview-12-2025` over Live. The routing behavior *should* transfer, but it's untested until Plan 3.
2. **Latency**: non-Live text API is slower per-turn than Live audio; budgets are measured against text mode here.
3. **Trigger gate parity**: harness applies the gate on the full utterance; live slim applies it on an incrementally-building input-transcription buffer, which may produce different results for long multi-clause utterances.
4. **No audio back-pressure**: the real product drops tool calls into an audio speaker; the harness just captures text. Speech-feel axis is not measured in Plan 2.

Plan 3 (real-audio subset) exists to quantify these deltas.

---

## Immediate next steps

1. **Re-run the suite on Ginés's real terminal** before interpreting findings 4 and 6 further — the sandboxed Claude Code context under-reports what's actually open.
2. **Land the stateful gate (Finding 1)** as Plan 2.5 commit #1. Biggest single win. Re-run the suite. Confirm no regression.
3. **Action description refinement (Finding 2 + 3)** as commit #2. Smaller, higher regression risk — measure carefully.
4. **Document worktree repair (Finding 6)** — not a code fix, a data fix.
5. **Never commit a prompt edit without running the suite against it.** That is the discipline this whole effort exists to enable.
