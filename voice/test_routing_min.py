#!/usr/bin/env python3
"""
Schema minimization — shrink the action description to the smallest
text that still routes at 100% on the in-session scenarios.

User's flow: say wake word → issue commands → say sleep. So every
utterance we care about is a command. Ambient chat is not evaluated
here (the wake-word gate handles it at the session boundary).

Sleep is weighted heavily — it must fire on every reasonable phrasing
because missing it leaves the Gemini session hot.

Run: source venv/bin/activate && python voice/test_routing_min.py
"""

import asyncio
import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv
from google import genai
from google.genai import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)

from jarvis_slim import SYSTEM_PROMPT, PROJECTS


MODEL = "gemini-2.5-flash"


SCENARIOS = [
    # tell — no-tool, Gemini answers (3)
    ("tell.capital",      "tell me the capital of France",         {"no_tool": True}),
    ("tell.hamlet",       "tell me who wrote hamlet",              {"no_tool": True}),
    ("tell.jets",         "tell me how jet engines work",          {"no_tool": True}),

    # search — web (4)
    ("search.iphone",     "search for the latest iphone news",     {"action": "search"}),
    ("search.weather",    "search the weather in Madrid",          {"action": "search"}),
    ("search.google",     "search google for drone regulations",   {"action": "search"}),
    ("search.game",       "search who won the game last night",    {"action": "search"}),

    # documents — personal files (4)
    ("docs.my_thesis",    "find my thesis on aerodynamics",        {"action": "documents"}),
    ("docs.my_notes",     "find my notes from last week",          {"action": "documents"}),
    ("docs.my_pdf",       "look up the pdf I wrote on gyroscopes", {"action": "documents"}),
    ("docs.my_docs",      "search my documents for drone regs",    {"action": "documents"}),

    # browse — navigate a site (3)
    ("browse.gmail",      "open gmail",                             {"action": "browse"}),
    ("browse.figma",      "go to figma",                            {"action": "browse"}),
    ("browse.fb_ads",     "open my facebook ads account",           {"action": "browse"}),

    # reads (3)
    ("read.calendar",     "what's on my calendar today",            {"action": "calendar"}),
    ("read.email",        "read me the latest emails",              {"action": "email"}),
    ("read.briefing",     "give me the morning briefing",           {"action": "briefing"}),

    # window (2)
    ("window.list",       "list me all the windows open",           {"action": "window"}),
    ("window.move",       "move chrome to the left",                {"action": "window"}),

    # sleep — SHARP, multiple phrasings (4)
    ("sleep.go_to",       "go to sleep",                            {"action": "sleep"}),
    ("sleep.goodbye",     "goodbye",                                {"action": "sleep"}),
    ("sleep.shut_down",   "shut down",                              {"action": "sleep"}),
    ("sleep.bye",         "bye jarvis",                             {"action": "sleep"}),

    # adversarial (2)
    ("adv.search_my",     "search for my thesis on aerodynamics",
        {"action_any": ["documents", "search"]}),
    ("adv.find_news",     "find the latest news",
        {"action_any": ["search"]}),
]


@dataclass
class Cfg:
    name: str
    action_desc: str


# ── Configurations — progressively smaller ─────────────────────────
#
# Baseline (C5) was ~400 chars. Goal: shrink without breaking 100%.

CONFIGS = [
    # C5 (reference) — 100% on full battery, all hedges present
    Cfg("C5_ref",
        "search=web lookup when user says 'search X'. "
        "documents=user's personal files when user says 'my X' or 'find my'. "
        "browse=navigate a specific site ('open X', 'go to X'). "
        "calendar/email/reminders/briefing=only when user explicitly asks "
        "to see their own data right now. "
        "window=app windows. code=project. github=repos. "
        "sleep=end the voice assistant session when user says "
        "'go to sleep' or 'goodbye'."),

    # C6 — drop the ambient hedges (user says wake-word gates this)
    Cfg("C6_noHedge",
        "search=web lookup. 'search X'. "
        "documents=user's files. 'my X', 'find my'. "
        "browse=open a site. 'open X', 'go to X'. "
        "calendar/email/reminders/briefing=user's own data. "
        "window=app windows. code=project. github=repos. "
        "sleep='go to sleep' or 'goodbye'."),

    # C7 — remove verbose anchors, keep verb→lane pairs
    Cfg("C7_pairs",
        "search=web ('search X'). "
        "documents=user's files ('my X', 'find my'). "
        "browse=open a site ('open X', 'go to X'). "
        "calendar/email/reminders/briefing=user's data. "
        "window=app windows. code, github. "
        "sleep='go to sleep', 'goodbye'."),

    # C8 — ultra-compressed
    Cfg("C8_ultra",
        "search=web. documents='my'/'find my'. browse='open'/'go to'. "
        "calendar, email, reminders, briefing=own data. "
        "window=apps. code, github. sleep='go to sleep'/'goodbye'."),

    # C9 — bare minimum verbs
    Cfg("C9_bare",
        "search=google. documents=my files. browse=open site. "
        "calendar, email, reminders, briefing, window, code, github. "
        "sleep='go to sleep'."),

    # C10 — absolute minimum; just the three disambiguating hints
    Cfg("C10_min",
        "search=google; documents=my files; sleep='go to sleep'. "
        "Also: browse, calendar, email, reminders, briefing, window, code, github."),

    # C11-C14 — swap `=` for `:`. The `=X` pattern in compressed form
    # was making Gemini emit action='search=web' (the description
    # string as the value). Colons are schema-legal punctuation and
    # don't look like key=value.
    Cfg("C11_colon",
        "search: web ('search X'). "
        "documents: user's files ('my X', 'find my'). "
        "browse: open a site ('open X', 'go to X'). "
        "calendar/email/reminders/briefing: user's data. "
        "window: app windows. code, github. "
        "sleep: 'go to sleep', 'goodbye'."),

    Cfg("C12_colon_tight",
        "search: web. documents: 'my' / 'find my'. "
        "browse: 'open' / 'go to'. "
        "calendar, email, reminders, briefing: user's data. "
        "window: apps. code, github. "
        "sleep: 'go to sleep' / 'goodbye'."),

    Cfg("C13_colon_bare",
        "search: google. documents: my files. browse: open a site. "
        "window: apps. sleep: 'go to sleep' / 'goodbye'. "
        "Also: calendar, email, reminders, briefing, code, github."),

    # C14 — the four critical disambiguators only, colons, rest bare
    Cfg("C14_four",
        "search: google. documents: my files. "
        "browse: open a site. sleep: 'go to sleep' / 'goodbye'. "
        "Others: calendar, email, reminders, briefing, window, code, github."),

    # C15 — drop "Others:" prefix and "a"
    Cfg("C15_noOthers",
        "search: google. documents: my files. "
        "browse: open site. sleep: 'go to sleep' / 'goodbye'. "
        "calendar, email, reminders, briefing, window, code, github."),

    # C16 — drop 'goodbye' hint on sleep (does Gemini infer?)
    Cfg("C16_noGoodbye",
        "search: google. documents: my files. "
        "browse: open site. sleep: 'go to sleep'. "
        "calendar, email, reminders, briefing, window, code, github."),

    # C17 — single-word anchors
    Cfg("C17_singleWord",
        "search: google. documents: my. browse: open. sleep: end session. "
        "calendar, email, reminders, briefing, window, code, github."),

    # C18 — even tighter
    Cfg("C18_tightest",
        "search: web. documents: my files. browse: open site. sleep. "
        "calendar, email, reminders, briefing, window, code, github."),

    # C19 — try richer sleep triggers but compact ("sleep", "bye",
    # "goodbye" all in one short list). Swap "go to sleep" for
    # just "sleep" to save chars.
    Cfg("C19_sleepList",
        "search: google. documents: my files. browse: open site. "
        "sleep: 'sleep' / 'goodbye' / 'bye'. "
        "calendar, email, reminders, briefing, window, code, github."),

    # C20 — replace 'google' with 'web' (same meaning, 3 chars less)
    Cfg("C20_web",
        "search: web. documents: my files. browse: open site. "
        "sleep: 'go to sleep' / 'goodbye'. "
        "calendar, email, reminders, briefing, window, code, github."),

    # C21 — combine C19 + C20
    Cfg("C21_both",
        "search: web. documents: my files. browse: open site. "
        "sleep: 'sleep' / 'goodbye' / 'bye'. "
        "calendar, email, reminders, briefing, window, code, github."),
]


def _tool(action_desc: str) -> types.Tool:
    default_query = (
        "Details in user's words. For code: project name "
        f"(one of: {', '.join(PROJECTS.keys())}). "
        "For window: a verb-led command ('move chrome left', 'list')."
    )
    return types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="do",
            description="Execute an actionable request.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "action": types.Schema(
                        type=types.Type.STRING,
                        description=action_desc,
                    ),
                    "query": types.Schema(
                        type=types.Type.STRING,
                        description=default_query,
                    ),
                    "session": types.Schema(
                        type=types.Type.STRING,
                        description="For code only: 'last', 'previous', or 'new'.",
                    ),
                },
                required=["action"],
            ),
        ),
    ])


client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


async def run_one(turn: str, tool: types.Tool):
    resp = await client.aio.models.generate_content(
        model=MODEL,
        contents=[types.Content(role="user", parts=[types.Part(text=turn)])],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[tool],
            temperature=0.0,
        ),
    )
    for p in (resp.candidates[0].content.parts if resp.candidates and resp.candidates[0].content and resp.candidates[0].content.parts else []):
        if p.function_call:
            return {"name": p.function_call.name,
                    "args": dict(p.function_call.args) if p.function_call.args else {}}
    return None


def verdict(expected: dict, call) -> tuple[bool, str]:
    if expected.get("no_tool"):
        return (call is None, "no-tool" if call is None else f"UNWANTED={call['args'].get('action','?')}")
    if call is None:
        return (False, "missing")
    got = call["args"].get("action", "").lower().strip()
    if "action" in expected:
        return (got == expected["action"], got)
    if "action_any" in expected:
        return (got in expected["action_any"], got)
    return (False, "?")


async def run_cfg(cfg: Cfg):
    tool = _tool(cfg.action_desc)
    ok = 0
    fails = []
    for name, turn, exp in SCENARIOS:
        try:
            call = await run_one(turn, tool)
        except Exception as e:
            fails.append((name, f"err: {e}"))
            continue
        passed, detail = verdict(exp, call)
        if passed:
            ok += 1
        else:
            fails.append((name, detail))
        await asyncio.sleep(0.1)
    return ok, len(SCENARIOS), fails


async def main():
    print("=" * 90)
    print(f"Schema minimization — {len(SCENARIOS)} scenarios × {len(CONFIGS)} configs")
    print(f"Focus: in-session routing (no ambient chat — handled by wake-word gate)")
    print("=" * 90)

    results = []
    for cfg in CONFIGS:
        chars = len(cfg.action_desc)
        print(f"\n→ {cfg.name} ({chars} chars)")
        ok, total, fails = await run_cfg(cfg)
        results.append((cfg, chars, ok, total, fails))
        status = "✓ 100%" if ok == total else f"{ok}/{total}"
        print(f"   {status}" + (f"  failures: {fails}" if fails else ""))

    print("\n" + "=" * 90)
    print(f"{'config':<14}{'chars':>7}  {'score':>9}   {'sleep ok?':>10}")
    print("-" * 90)
    for cfg, chars, ok, total, fails in results:
        sleep_fails = [n for n, _ in fails if n.startswith("sleep.")]
        sleep_status = "all pass" if not sleep_fails else f"FAIL: {','.join(s.split('.',1)[1] for s in sleep_fails)}"
        print(f"{cfg.name:<14}{chars:>7}  {ok}/{total:>3} ({100*ok/total:>3.0f}%)   {sleep_status}")
    print("=" * 90)

    # Winners — smallest at 100%
    perfect = [(cfg, chars) for cfg, chars, ok, total, _ in results if ok == total]
    if perfect:
        smallest = min(perfect, key=lambda x: x[1])
        print(f"\nSMALLEST 100%: {smallest[0].name} ({smallest[1]} chars)")
        print(f"\n{smallest[0].action_desc}\n")
    else:
        print("\nNo config hit 100%. Best scores:")
        for cfg, chars, ok, total, _ in sorted(results, key=lambda r: (-r[2], r[1])):
            print(f"  {cfg.name:<14} {chars:>4}c  {ok}/{total}")


if __name__ == "__main__":
    asyncio.run(main())
