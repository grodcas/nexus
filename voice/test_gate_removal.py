#!/usr/bin/env python3
"""
Does Gemini alone need the trigger-word gate?

Sends text scenarios with the ACTUAL jarvis_slim SYSTEM_PROMPT and
TOOL_DECLARATIONS (no modifications — the user explicitly asked not
to saturate the prompt). Three buckets:

  1. Clean commands          — should call `do` with the right action
  2. Homophone-mangled STT   — should still call `do` (gate's
                               failure mode: STT heard "least" for
                               "list", etc). This is what the user
                               described: the LLM has context, the
                               gate does not, so the gate blocks
                               when the LLM would succeed.
  3. Ambient chat            — should NOT call `do`. This is the
                               gate's original reason-to-exist: stop
                               Gemini from firing tools on
                               operational-sounding chatter.

Run: source venv/bin/activate && python voice/test_gate_removal.py

Model: gemini-2.5-flash (text-mode proxy for the native-audio Live
model jarvis uses — same tool-calling decision layer).
"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv
from google import genai
from google.genai import types

# Load jarvis's SYSTEM_PROMPT and TOOL_DECLARATIONS unchanged.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)

from jarvis_slim import SYSTEM_PROMPT, TOOL_DECLARATIONS  # noqa: E402


# (name, user_turn, expected)
# expected:
#   {"action": "..."}      → must call `do` with this action
#   {"action_any": [...]}  → must call `do` with one of these actions
#   {"no_tool": True}      → must NOT call any tool
SCENARIOS = [
    # ── Clean, no trigger word ──────────────────────────────────────
    ("clean.window_list",       "list me all the windows open",
        {"action": "window"}),
    ("clean.window_move",       "move chrome to the left",
        {"action": "window"}),
    ("clean.search",            "search for the latest iphone news",
        {"action": "search"}),
    ("clean.browse",            "open gmail",
        {"action": "browse"}),
    ("clean.calendar",          "what's on my calendar today",
        {"action": "calendar"}),
    ("clean.email",             "read me the latest emails",
        {"action": "email"}),
    ("clean.briefing",          "give me the morning briefing",
        {"action": "briefing"}),
    ("clean.documents",         "find my thesis on aerodynamics",
        {"action": "documents"}),
    ("clean.sleep",             "go to sleep",
        {"action": "sleep"}),

    # ── Homophone / STT-mangled (gate's failure mode) ───────────────
    ("mangled.list_as_least",   "least me all the windows open",
        {"action": "window"}),
    ("mangled.list_as_list_it", "list it all the windows open",
        {"action": "window"}),
    ("mangled.move_as_moove",   "moove chrome to the left",
        {"action": "window"}),
    ("mangled.search_as_surge", "surge for the latest iphone news",
        {"action": "search"}),
    ("mangled.browse_as_brows", "brows my gmail",
        {"action": "browse"}),
    ("mangled.calendar_partial","what's on my calender today",
        {"action": "calendar"}),
    ("mangled.sleep_as_sheep",  "go to sheep",
        {"action_any": ["sleep"], "allow_no_tool": True}),

    # ── Ambient chat (no tool expected) ─────────────────────────────
    ("ambient.will_check_cal",  "I need to check my calendar later today",
        {"no_tool": True}),
    ("ambient.weather",         "can you believe this weather?",
        {"no_tool": True}),
    ("ambient.will_browse",     "yeah I was gonna browse the web earlier",
        {"no_tool": True}),
    ("ambient.coffee",          "let me take a break and grab some coffee",
        {"no_tool": True}),
    ("ambient.email_story",     "my boss sent me the weirdest email this morning",
        {"no_tool": True}),
    ("ambient.search_meta",     "I always search google before I ask anyone",
        {"no_tool": True}),
    ("ambient.sleep_meta",      "I barely got any sleep last night",
        {"no_tool": True}),
    ("ambient.window_figure",   "there's a window of opportunity here",
        {"no_tool": True}),

    # ── Borderline: user thinks aloud, may or may not want action ──
    ("borderline.i_want_search","I want to search for iphone news",
        {"action_any": ["search"], "allow_no_tool": True}),
    ("borderline.can_you_list", "can you list my windows",
        {"action": "window"}),

    # ── Disambiguation: web search vs document search vs Gemini's
    #    own knowledge. This is the real reason the trigger-word
    #    approach existed — distinguishing three lanes that all
    #    sound like "find/search/look up X". No hints in the prompt;
    #    Gemini decides from the query alone.
    # ────────────────────────────────────────────────────────────

    # Clearly personal files — should go to `documents`
    ("disambig.my_thesis",      "find my thesis on aerodynamics",
        {"action": "documents"}),
    ("disambig.my_pdf",         "look up the drone PDF I wrote",
        {"action": "documents"}),
    ("disambig.search_my_docs", "search my documents for the drone regs file",
        {"action": "documents"}),
    ("disambig.find_my_notes",  "find my notes from last semester",
        {"action": "documents"}),

    # Clearly live/volatile — should go to `search`
    ("disambig.iphone_news",    "what's the latest iphone news",
        {"action": "search"}),
    ("disambig.weather_now",    "what's the weather in Madrid right now",
        {"action": "search"}),
    ("disambig.search_google",  "search google for drone regulations",
        {"action": "search"}),
    ("disambig.stock_price",    "what's nvidia trading at today",
        {"action": "search"}),
    ("disambig.who_won_lastnite","who won the game last night",
        {"action": "search"}),

    # Clearly timeless / general knowledge — Gemini should answer
    # from its own head, no tool
    ("disambig.capital_france", "what's the capital of France",
        {"no_tool": True}),
    ("disambig.who_wrote_hamlet","who wrote hamlet",
        {"no_tool": True}),
    ("disambig.define_word",    "what does 'ephemeral' mean",
        {"no_tool": True}),
    ("disambig.explain_concept","explain how a jet engine works briefly",
        {"no_tool": True}),

    # The ambiguous middle — "find X" / "search X" with no anchor
    # word. These are where trigger words historically helped. Accept
    # multiple reasonable interpretations.
    ("disambig.amb_find_drones","find something about drones",
        {"action_any": ["documents", "search"], "allow_no_tool": True}),
    ("disambig.amb_search_iphone","search for iphone 17 specs",
        {"action_any": ["search"], "allow_no_tool": True}),
    ("disambig.amb_look_up_ml", "look up machine learning papers",
        {"action_any": ["documents", "search"], "allow_no_tool": True}),

    # Cross-contamination: does Gemini route correctly when the
    # wording tries to send it to the wrong lane?
    ("disambig.mine_but_weather","find my weather report for today",
        {"action_any": ["search", "documents"]}),
    ("disambig.my_thesis_slashgoogle","search for my thesis on aerodynamics",
        {"action_any": ["documents", "search"]}),  # "my" should win → documents
]


MODEL = "gemini-2.5-flash"
TOOL = types.Tool(function_declarations=TOOL_DECLARATIONS)

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


async def run_one(user_turn: str):
    resp = await client.aio.models.generate_content(
        model=MODEL,
        contents=[types.Content(role="user", parts=[types.Part(text=user_turn)])],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[TOOL],
            temperature=0.0,
        ),
    )
    tool_calls = []
    text_parts = []
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
            return True, "no tool (as expected)"
        return False, f"UNEXPECTED tool: {tool_calls[0]['name']}({tool_calls[0]['args']})"

    if not tool_calls:
        if expected.get("allow_no_tool"):
            return True, "no tool (allowed)"
        return False, "expected tool, got none"

    call = tool_calls[0]
    if call["name"] != "do":
        return False, f"expected `do`, got `{call['name']}`"
    got_action = call["args"].get("action", "").lower().strip()

    if "action" in expected:
        if got_action == expected["action"]:
            return True, f"action={got_action}"
        return False, f"expected action={expected['action']}, got {got_action}"
    if "action_any" in expected:
        if got_action in expected["action_any"]:
            return True, f"action={got_action}"
        return False, f"expected one of {expected['action_any']}, got {got_action}"
    return False, "no expectation set"


async def main():
    print("=" * 72)
    print(f"Gate-removal test — model={MODEL}")
    print(f"System prompt ({len(SYSTEM_PROMPT)} chars): {SYSTEM_PROMPT!r}")
    print("=" * 72)

    results = {
        "clean": [0, 0], "mangled": [0, 0], "ambient": [0, 0],
        "borderline": [0, 0], "disambig": [0, 0],
    }
    failures = []

    for name, turn, expected in SCENARIOS:
        bucket = name.split(".", 1)[0]
        try:
            tool_calls, text = await run_one(turn)
        except Exception as e:
            print(f"✗ {name} — API error: {e}")
            results[bucket][1] += 1
            failures.append((name, f"API error: {e}"))
            continue

        ok, detail = verdict(expected, tool_calls)
        results[bucket][1] += 1
        if ok:
            results[bucket][0] += 1
            mark = "✓"
        else:
            mark = "✗"
            failures.append((name, f"{detail} | said: {text[:80]!r}"))

        call_repr = (
            f"do({tool_calls[0]['args']})" if tool_calls else f"<text: {text[:40]!r}>"
        )
        print(f"{mark} {name:36s} → {call_repr[:70]:70s}  [{detail}]")
        await asyncio.sleep(0.3)

    print("=" * 72)
    for bucket, (ok, total) in results.items():
        pct = 100 * ok / total if total else 0
        print(f"  {bucket:12s}: {ok}/{total}  ({pct:.0f}%)")
    total_ok = sum(v[0] for v in results.values())
    total_n = sum(v[1] for v in results.values())
    print(f"  {'TOTAL':12s}: {total_ok}/{total_n}  ({100 * total_ok / total_n:.0f}%)")

    if failures:
        print("\nFailures:")
        for name, detail in failures:
            print(f"  - {name}: {detail}")
    print("=" * 72)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
