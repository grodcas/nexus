#!/usr/bin/env python3
"""
Three-lane routing experiment.

The user's proposed convention (what we're teaching Gemini):

    "tell me X"    → NO tool. Gemini answers from its own knowledge.
    "search X"     → action=search. Google web search.
    "find my X"    → action=documents. User's personal file archive.
    "open/go X"    → action=browse. Navigate to a specific site.
    "what's on my calendar / email / reminders" → those actions.
    (ambient chat) → no tool.

We are NOT allowed to add words to the system prompt. The only
degree of freedom is the TOOL SCHEMA descriptions (which Gemini
weighs when picking an action). This script compares several
schema configurations on the same scenarios and prints a
per-bucket accuracy table so we can pick the wiring that works
best for the convention.

Run: source venv/bin/activate && python voice/test_routing.py
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

from jarvis_slim import SYSTEM_PROMPT  # the slim prompt — untouched


MODEL = "gemini-2.5-flash"


# ── Scenarios ────────────────────────────────────────────────────────
# (name, user_turn, expected)
# expected:
#   {"action": "X"}             must call do(action=X)
#   {"action_any": [...]}       any of these actions pass
#   {"no_tool": True}           no tool call
#   {"allow_no_tool": True}     adds no-tool as an acceptable outcome

SCENARIOS = [
    # ── TELL lane — user wants Gemini's own answer, no tool ─────
    ("tell.capital_france",      "tell me the capital of France",
        {"no_tool": True}),
    ("tell.api_def",             "tell me what an API is",
        {"no_tool": True}),
    ("tell.jet_engine",          "tell me how jet engines work",
        {"no_tool": True}),
    ("tell.drone_industry",      "tell me about the drone industry",
        {"no_tool": True}),
    ("tell.joke",                "tell me a joke",
        {"no_tool": True}),
    ("tell.hamlet",              "tell me who wrote hamlet",
        {"no_tool": True}),
    ("tell.weather_nolive",      "tell me the weather in Madrid",
        {"no_tool": True, "allow_tool": True}),  # edge; either OK

    # ── SEARCH lane — explicit web lookup ────────────────────────
    ("search.iphone_news",       "search for the latest iphone news",
        {"action": "search"}),
    ("search.google_drones",     "search google for drone regulations",
        {"action": "search"}),
    ("search.madrid_weather",    "search the weather in Madrid",
        {"action": "search"}),
    ("search.lastnite_game",     "search who won the game last night",
        {"action": "search"}),
    ("search.restaurants",       "search for restaurants nearby",
        {"action": "search"}),
    ("search.iphone17_specs",    "search iphone 17 release date",
        {"action": "search"}),

    # ── DOCUMENTS lane — user's own files, "my" anchor ───────────
    ("docs.my_thesis",           "find my thesis on aerodynamics",
        {"action": "documents"}),
    ("docs.my_drone_paper",      "find my drone paper",
        {"action": "documents"}),
    ("docs.my_notes",            "find my notes from last week",
        {"action": "documents"}),
    ("docs.pdf_I_wrote",         "look up the PDF I wrote on gyroscopes",
        {"action": "documents"}),
    ("docs.search_my_docs",      "search my documents for the drone regs",
        {"action": "documents"}),

    # ── BROWSE lane — open a specific site ───────────────────────
    ("browse.gmail",             "open gmail",
        {"action": "browse"}),
    ("browse.figma",             "go to figma",
        {"action": "browse"}),
    ("browse.fb_ads",            "open my facebook ads account",
        {"action_any": ["browse"]}),
    ("browse.shopify",           "navigate to shopify",
        {"action": "browse"}),
    ("browse.university",        "open the university website",
        {"action": "browse"}),

    # ── User-data reads — calendar / email / briefing / reminders ─
    ("read.calendar",            "what's on my calendar today",
        {"action": "calendar"}),
    ("read.email",               "read me the latest emails",
        {"action": "email"}),
    ("read.reminders",           "what are my reminders",
        {"action": "reminders"}),
    ("read.briefing",            "give me the morning briefing",
        {"action": "briefing"}),

    # ── Window management ────────────────────────────────────────
    ("window.list",              "list me all the windows open",
        {"action": "window"}),
    ("window.move_chrome",       "move chrome to the left",
        {"action": "window"}),
    ("window.least",             "least me all the windows open",  # homophone
        {"action": "window"}),

    # ── Sleep ────────────────────────────────────────────────────
    ("sleep.direct",             "go to sleep",
        {"action": "sleep"}),
    ("sleep.goodbye",            "goodbye, shut down",
        {"action": "sleep"}),

    # ── Ambient chat — MUST NOT fire a tool ──────────────────────
    ("ambient.check_cal_later",  "I need to check my calendar later today",
        {"no_tool": True}),
    ("ambient.weather_chat",     "the weather is terrible today",
        {"no_tool": True}),
    ("ambient.search_meta",      "I always search google before I ask anyone",
        {"no_tool": True}),
    ("ambient.email_story",      "my boss sent me the weirdest email",
        {"no_tool": True}),
    ("ambient.coffee",           "let me take a break and grab some coffee",
        {"no_tool": True}),
    ("ambient.sleep_meta",       "I barely got any sleep last night",
        {"no_tool": True}),
    ("ambient.window_figurative","there's a window of opportunity here",
        {"no_tool": True}),

    # ── Cross-contamination / adversarial ────────────────────────
    ("adv.search_for_my_thesis", "search for my thesis on aerodynamics",
        {"action_any": ["documents", "search"]}),  # hard — "my" should win
    ("adv.find_the_news",        "find the latest news",
        {"action_any": ["search"]}),
    ("adv.find_restaurants",     "find me restaurants nearby",
        {"action_any": ["search", "browse"]}),
    ("adv.look_up_ml_papers",    "look up machine learning papers",
        {"action_any": ["documents", "search"]}),
]


# ── Schema configurations to compare ─────────────────────────────────

@dataclass
class SchemaConfig:
    name: str
    description: str
    tool: types.Tool


def _make_tool(action_desc: str, query_desc: str | None = None) -> types.Tool:
    """Build a `do` tool with the given per-field descriptions."""
    from jarvis_slim import PROJECTS
    default_query = (
        "Details in user's words. For code: project name "
        f"(one of: {', '.join(PROJECTS.keys())}). "
        "For window: a verb-led command like 'move chrome left', "
        "'move chrome to other screen', 'move chrome left on secondary screen', "
        "'maximize iterm on main', 'close finder', 'list'."
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
                        description=query_desc or default_query,
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


CONFIGS: list[SchemaConfig] = [
    # C0 — current baseline (what's in jarvis_slim right now)
    SchemaConfig(
        name="C0_baseline",
        description="flat enum, no per-action hints",
        tool=_make_tool(
            "One of: browse, search, calendar, email, reminders, briefing, "
            "documents, code, github, window, sleep."
        ),
    ),

    # C1 — terse per-action anchor. Same mass, richer routing signal.
    SchemaConfig(
        name="C1_anchors",
        description="per-action one-word anchors (search=web, documents=my files)",
        tool=_make_tool(
            "search=google web. documents=user's own files. "
            "browse=open a specific site. calendar/email/reminders/briefing=user's own data. "
            "window=app windows. code=project handoff. github=repos. sleep=end session."
        ),
    ),

    # C2 — verb-convention anchors. Teaches the "tell/search/find my"
    # convention without touching the system prompt. This is the one
    # we actually want to validate.
    SchemaConfig(
        name="C2_verbs",
        description="verb convention: 'tell X'=no tool, 'search X'=web, 'find my X'=docs",
        tool=_make_tool(
            "search=web lookup when user says 'search X'. "
            "documents=user's personal files when user says 'my X' or 'find my'. "
            "browse=navigate a specific site ('open X', 'go to X'). "
            "calendar/email/reminders/briefing=user's own data. "
            "window=app windows. code=project. github=repos. sleep=end."
        ),
    ),

    # C3 — minimal. Two critical hints only, everything else enum.
    SchemaConfig(
        name="C3_minimal",
        description="minimal — only search/documents get a hint, rest enum",
        tool=_make_tool(
            "browse, search (web lookup), documents (user's own files), "
            "calendar, email, reminders, briefing, code, github, window, sleep."
        ),
    ),

    # C4 — C2 with tighter "explicit request" language on the reads
    # and the exact trigger phrase for sleep. Aimed at C2's three
    # ambient false-positives (calendar, email, sleep).
    SchemaConfig(
        name="C4_tight",
        description="C2 + explicit-request on reads + exact phrase for sleep",
        tool=_make_tool(
            "search=web lookup when user says 'search X'. "
            "documents=user's personal files when user says 'my X' or 'find my'. "
            "browse=navigate a specific site ('open X', 'go to X'). "
            "calendar/email/reminders/briefing=only when user explicitly asks "
            "to see their own data right now. "
            "window=app windows. code=project. github=repos. "
            "sleep=only when user says 'go to sleep' or 'goodbye'."
        ),
    ),

    # C5 — C4 with sleep anchored to "voice session" concept so
    # "break" / "coffee" don't pattern-match. Testing whether one
    # more word on sleep closes the last ambient false-positive.
    SchemaConfig(
        name="C5_session",
        description="C4 + sleep anchored to 'end the voice session' concept",
        tool=_make_tool(
            "search=web lookup when user says 'search X'. "
            "documents=user's personal files when user says 'my X' or 'find my'. "
            "browse=navigate a specific site ('open X', 'go to X'). "
            "calendar/email/reminders/briefing=only when user explicitly asks "
            "to see their own data right now. "
            "window=app windows. code=project. github=repos. "
            "sleep=end the voice assistant session when user says "
            "'go to sleep' or 'goodbye'."
        ),
    ),
]


# ── Runner ───────────────────────────────────────────────────────────

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


async def run_one(turn: str, tool: types.Tool) -> tuple[list, str]:
    resp = await client.aio.models.generate_content(
        model=MODEL,
        contents=[types.Content(role="user", parts=[types.Part(text=turn)])],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[tool],
            temperature=0.0,
        ),
    )
    tool_calls, text_parts = [], []
    if resp.candidates and resp.candidates[0].content and resp.candidates[0].content.parts:
        for p in resp.candidates[0].content.parts:
            if p.function_call:
                tool_calls.append({
                    "name": p.function_call.name,
                    "args": dict(p.function_call.args) if p.function_call.args else {},
                })
            if p.text:
                text_parts.append(p.text)
    return tool_calls, " ".join(text_parts).strip()


def verdict(expected: dict, tool_calls: list) -> tuple[bool, str]:
    if expected.get("no_tool"):
        if not tool_calls:
            return True, "no-tool"
        if expected.get("allow_tool"):
            return True, f"tool={tool_calls[0]['args'].get('action','?')} (allowed)"
        return False, f"UNWANTED tool={tool_calls[0]['args'].get('action','?')}"

    if not tool_calls:
        if expected.get("allow_no_tool"):
            return True, "no-tool (allowed)"
        return False, "missing tool"

    got = tool_calls[0]["args"].get("action", "").lower().strip()
    if "action" in expected:
        return (got == expected["action"], got)
    if "action_any" in expected:
        return (got in expected["action_any"], got)
    return False, "unspecified expectation"


def bucket_of(name: str) -> str:
    return name.split(".", 1)[0]


async def run_config(cfg: SchemaConfig) -> dict:
    buckets: dict[str, list[int]] = {}  # bucket → [ok, total]
    failures: list[tuple[str, str]] = []
    for name, turn, expected in SCENARIOS:
        b = bucket_of(name)
        buckets.setdefault(b, [0, 0])
        buckets[b][1] += 1
        try:
            calls, _ = await run_one(turn, cfg.tool)
        except Exception as e:
            failures.append((name, f"API error: {e}"))
            continue
        ok, detail = verdict(expected, calls)
        if ok:
            buckets[b][0] += 1
        else:
            failures.append((name, detail))
        await asyncio.sleep(0.15)
    return {"buckets": buckets, "failures": failures}


async def main():
    print("=" * 88)
    print(f"Three-lane routing experiment  |  model={MODEL}")
    print(f"System prompt ({len(SYSTEM_PROMPT)} chars, UNCHANGED): {SYSTEM_PROMPT!r}")
    print("=" * 88)
    print(f"{len(SCENARIOS)} scenarios × {len(CONFIGS)} configs = "
          f"{len(SCENARIOS) * len(CONFIGS)} calls\n")

    results: dict[str, dict] = {}
    for cfg in CONFIGS:
        print(f"→ running {cfg.name}: {cfg.description}")
        results[cfg.name] = await run_config(cfg)

    # Comparison table
    buckets = sorted({bucket_of(n) for n, _, _ in SCENARIOS})
    col_w = max(14, max(len(c.name) for c in CONFIGS) + 2)

    print("\n" + "=" * 88)
    header = f"{'bucket':<14}" + "".join(f"{c.name:<{col_w}}" for c in CONFIGS)
    print(header)
    print("-" * len(header))
    for b in buckets:
        row = f"{b:<14}"
        for cfg in CONFIGS:
            ok, tot = results[cfg.name]["buckets"].get(b, [0, 0])
            pct = (100 * ok / tot) if tot else 0
            row += f"{ok}/{tot} ({pct:.0f}%)".ljust(col_w)
        print(row)
    print("-" * len(header))
    totals_row = f"{'TOTAL':<14}"
    for cfg in CONFIGS:
        ok = sum(v[0] for v in results[cfg.name]["buckets"].values())
        tot = sum(v[1] for v in results[cfg.name]["buckets"].values())
        pct = (100 * ok / tot) if tot else 0
        totals_row += f"{ok}/{tot} ({pct:.0f}%)".ljust(col_w)
    print(totals_row)
    print("=" * 88)

    for cfg in CONFIGS:
        fails = results[cfg.name]["failures"]
        if fails:
            print(f"\n{cfg.name} failures ({len(fails)}):")
            for name, d in fails:
                print(f"  - {name:30s} {d}")
    print("=" * 88)


if __name__ == "__main__":
    asyncio.run(main())
