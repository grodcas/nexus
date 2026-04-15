# Nexus — Evaluation & Hardening Plan

> How we move Nexus from "works, but feels cheap" to "few features, perfect
> execution." Three sequential plans, each one producing a clean substrate
> for the next. Written 2026-04-15 after a long design conversation; this
> file is the canonical record of that conversation.

---

## The problem this plan exists to solve

Nexus already does the things it's supposed to do: briefing (calendar /
email / reminders / todo), code handoff to Claude, web search, window
management, document search, and general-knowledge answers via Gemini
Live. None of them do it with flying colours. The failure modes cluster
into three categories that keep getting conflated:

1. **Speed** — slow searches, slow Claude handoff, slow briefing reads.
2. **Accuracy** — wrong tool called, wrong args, trigger word ignored,
   context bleed causing unwanted tool use.
3. **Human feel** — long silences, no pre-speech ack, robotic cadence,
   no "I'm on it" before a 5-second block.

Every fix currently lands anecdotally. We notice a thing, tweak a prompt
or a handler, ship, hope nothing else broke. The risk is the **tower of
spades**: one fix over-constrains the model, another breaks an unrelated
feature, and the daily-use failure rate trends sideways instead of down.

The way out is to stop iterating on vibes and start iterating against a
**baseline** — but a baseline only helps if the substrate under it is
already clean. Hence the three-plan sequencing below.

---

## Plan 1 — Tool hardening (plumbing pass, no AI involved)

**Goal:** make every handler in `handle_tool` fast, correct, and silent
about its own failures *before* any AI evaluation runs on top of it.

**Why first:** the AI is currently being blamed for failures that are
actually plumbing — cold browser, re-sync-every-call briefing, Playwright
clicks on hidden elements, dead air during long blocks. If we test the
AI on top of broken tools, every routing failure is ambiguous ("was that
Gemini's fault or the tool's fault?"). Fixing tools first means every
failure in Plan 2 is unambiguously an AI-layer issue. It is also the
phase with **zero tower-of-spades risk** — none of these changes touch
the prompt or the schema, so they cannot regress routing.

**Scope discipline (non-negotiable):** Plan 1 is a performance and
correctness pass, not a cleanup pass. A fix belongs in Plan 1 only if it:

1. cuts measured latency, or
2. fixes a reproducible correctness bug, or
3. removes dead air from the user's perspective.

Refactor-creep, "this function is ugly," or "we could abstract this" —
defer to after Plan 3. The "few but perfect" rule applies here too.

**Method:**

1. Instrument every `handle_tool` branch with start/end timestamps and
   log to JSONL. Run each handler cold and warm. Stare at where the
   seconds go.
2. Fix only what the numbers justify. Suspected hot spots, to be
   confirmed by measurement:
   - **Cold browser** — pre-warm Chromium on app start, not on first
     `browse` call.
   - **Briefing re-sync** — cache-first, sync in background so the first
     call is instant.
   - **Dead air during long tools** — `asyncio.create_task(handler)`,
     speak an ack line ("searching now") before awaiting the handler.
     No prompt change; structural.
   - **Playwright click-under-element** — stop clicking. Use
     `get_by_role()` + keyboard navigation, or `page.evaluate()` for
     direct event dispatch.
   - **Trigger-word bleed** — hard gate in Python: if `action` is in
     the tool-using set and the transcript has no trigger token, return
     `"No trigger word heard."` Enforced structurally, not begged-for in
     the prompt. This is the single most important reliability fix and
     it costs zero prompt chars.
3. Re-measure after each fix. Keep the before/after numbers in the
   commit message so future-us can see what moved.

**Definition of done for Plan 1:** every handler has a measured latency
budget, every handler speaks an ack within 800ms when the work will
take >2s, and the trigger gate is enforced in Python.

---

## Plan 2 — Text-mode evaluation harness (AI layer, no voice)

**Goal:** measure routing correctness, task success, and latency against
a fixed suite of test cases, with zero voice involvement.

**Why text-mode:** audio quality is not what we're evaluating — latency
and content are. Gemini Live accepts text turns on the same session path
(`send_client_content`) using the exact same schema and prompt as the
voice path. Running the suite in text mode gives us routing correctness,
task success, tool-call latency, trigger discipline, and stability — all
the things we care about — at a fraction of the cost of audio. STT and
TTS latency are added back **analytically**: measured once per length
bucket, applied as a fixed offset. Wrong by ~200ms in absolute terms,
which we don't care about; catches regressions just fine.

### Success metrics (six axes, each independently measurable)

| Metric | Definition | How to score |
|---|---|---|
| **Routing correctness** | Given an utterance, does Gemini call the right `action` with the right `query`? | Binary per case. Ground-truth label per test case. |
| **Task success** | Given the right routing, does the handler achieve the goal? | Binary, sometimes 0 / 0.5 / 1. Haiku judge for open-ended outputs, hard asserts for structured ones. |
| **Latency budget adherence** | Was end-to-end time within the class-specific ceiling? | % in budget per class. Starting budgets: general answer <1.5s TTFW, window <2s, briefing <3s TTFW, search <8s TTFW with ack <1s, code handoff <4s. |
| **Speech feel** | Ack within 800ms? Any dead-air gap >2s? Response length appropriate? | Measured from timestamps. No human grading. |
| **Trigger discipline** | False-positive (tool used without trigger) and false-negative (trigger said, no / wrong tool) rates. | Binary per case. The #1 current annoyance. |
| **Stability** | Run each case N=5 times. % of runs that produce the same routing + success outcome. | Catches "sometimes it does the wrong thing" bugs that point-tests miss. |

**The discipline that makes this work:** every change must be scored
against the **full suite**, not just the case being fixed. A prompt
tweak that improves case 12 but drops stability from 95% to 80% is a
regression. That's the regression protection we currently don't have.

### Test taxonomy (~80 cases, 10 buckets)

1. **Trivial knowledge** (10) — "capital of Bolivia", "what's a TDLAS
   sensor". **Must not call a tool.** Primary test for trigger discipline.
2. **Briefing** (8) — "what's on today", "anything pending", "what's my
   day look like".
3. **Web search** (10) — includes a couple where `browse` is correct and
   `search` is not.
4. **Window management** (10) — against whatever is actually open via
   `list_windows()`. No hardcoded app names in the tests either.
5. **Document lookup** (8) — real queries against `~/.nexus/documents/`
   whose correct answers are known.
6. **Code handoff** (6) — "connect me to nexus", "resume the last
   session", "new session on nexus". Tests the two-step handoff.
7. **Compound** (8) — "search for X and put the browser on the right".
   Tests sequencing.
8. **Ambiguous / pronoun** (6) — "do it again", "open that". Tests
   context carry.
9. **Correction mid-turn** (6) — "no not that, search for Y instead".
   Tests interrupt recovery.
10. **Disfluent speech** (8) — "uhh can you like, um…". These are the
    only ones that must run through real audio (Plan 3).

Cases live in `eval/cases.yaml`, one record per case:
`(utterance, expected_action, expected_args_shape, success_predicate,
latency_class)`. The suite grows whenever real use surfaces a new
failure — it is never frozen, it is versioned.

**Case authoring bias to avoid:** it is tempting to write the cases
Nexus already handles well. Force the suite to include the failures
actually observed, even the embarrassing ones, even the ones where the
grading is unclear. The point of the suite is to be honest, not to pass.

### What Plan 2 will produce

1. `eval/cases.yaml` — the labeled test cases.
2. `eval/run.py` — harness that opens a Gemini Live text session with
   slim's exact schema + prompt, replays each case, captures
   `{routing, args, python_latency, handler_result, success}`, writes
   JSONL + summary table.
3. `eval/judge.py` — Haiku-backed grader for open-ended cases.
4. **Baseline sweep** — first honest picture of where Nexus stands. All
   future experiments are measured against this baseline in <5 min.

---

## Plan 3 — Voice recordings (real audio, subjective feel)

**Goal:** add real STT + TTS + interrupt behaviour to the suite, without
re-running the full 80 cases through audio.

**Method:** Ginés records ~15-20 utterances covering the cases most
sensitive to disfluency, interrupts, and the "feels cheap" subjective
layer. These WAV files are pre-fed into the Gemini Live audio channel by
`eval/audio_subset.py`; we measure ack time and end-to-end wall clock.

**Discipline:** **do not re-record.** Each recording is ground truth
forever. Re-recording when the app changes destroys the regression
baseline. Record once, live with the exact phrasing captured. If a case
turns out to be poorly chosen, add a new one; don't replace the old one.

**What Plan 3 catches that Plan 2 cannot:** STT accuracy on disfluent
speech, interrupt handling, real ack timing, audio backlog behaviour
during long tools (the `1011` risk window).

---

## After Plan 3 — real-use iteration

Once Plans 1-3 are in place, Ginés uses Nexus daily. Every interaction
is appended to a JSONL log (transcript, tool called, args, latency
breakdown, handler result, verbal correction if any). New failure modes
become new eval cases. Every fix is measured against the baseline in
under five minutes. **This is the end of the tower of spades.**

The realistic target is not "perfect Jarvis." It is:
- **no regressions when a fix ships**, and
- **the failure rate trending down week over week instead of sideways.**

That is what Anthropic-style evals give a single-user voice agent, and
it is what Nexus currently lacks.

---

## Hybridization model (how the three AIs fit together)

This is the architectural bet underneath the whole plan.

| Model | Role | Why it fits |
|---|---|---|
| **Gemini Live** | Router + voice front-end. Hears the user, decides whether to answer from its own knowledge or call `do(…)`. | Real-time native audio, sub-second latency, natural cadence. Weak at long reasoning and long waits. |
| **Claude (Sonnet / Opus)** | Worker behind `browse` / `search` / `code`. The smart one. | Strong reasoning, strong tool use, handles multi-step recovery. Slow — too slow for the turn itself, fine behind an ack. |
| **Haiku** | Middle tier. Grading, summarizing, filtering a briefing, judging eval outputs. | Fast enough for the turn, smart enough to be useful, cheap enough to run on every call. Currently unused; the plan introduces it. |

The user-facing illusion is **one superpowered assistant**. The reality
is three models handing off through Python, each doing what it's best
at. The seams are hidden by ack lines and the TTS bypass pattern.

---

## What we are explicitly NOT doing

- **Not building a generic eval framework.** `eval/run.py` is 150 lines
  and specific to Nexus. No plugins, no abstractions, no config DSL.
- **Not doing human preference evals at scale.** Ginés is the preference
  eval. The suite covers the objective axes; the subjective layer is the
  real-use log after Plan 3.
- **Not designing the methodology from the feature list.** The eval
  suite must be grounded in what Ginés actually does with Nexus, which
  is captured before `cases.yaml` is written.
- **Not touching the prompt during Plan 1.** Plan 1 is plumbing only.
  Prompt changes belong in Plan 2 or later, measured against baseline.
- **Not adding new features** (news feed, screenshot-of-current-window
  helper, etc.) until Plans 1-3 are complete. New features on a shaky
  base make the base shakier.

---

## Status

- **Plan 1** — not started. First step: instrument `handle_tool` with
  per-branch timing and write cold/warm measurements for each action.
- **Plan 2** — blocked on Plan 1. First step: Ginés lists the ten most
  common real queries, three most annoying failures, and one wished-for
  capability. That grounds `cases.yaml`.
- **Plan 3** — blocked on Plan 2.
