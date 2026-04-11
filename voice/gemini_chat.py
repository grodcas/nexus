#!/usr/bin/env python3
"""
Quick Gemini chat — no pipecat, no voice, just text in/out.
For testing tool routing and prompt behavior.

Usage:
    cd ~/nexus && source venv/bin/activate
    python voice/gemini_chat.py
"""

import os
import sys
import json

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)

from google import genai

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from session_manager import load_projects

PROJECTS = load_projects()

SYSTEM_PROMPT = """\
You are Jarvis, a personal assistant. Sharp, friendly, concise.

Rules:
- ALWAYS say something before calling a tool. Never go silent.
- When a tool returns data, summarize the key points in 2-4 spoken sentences. Do not read raw data.
- Keep ALL responses under 25 seconds of speech. Be brief. If there is too much data, give the highlights and ask if the user wants more detail.
- For briefings: top 3 items only, one sentence each.

Browser — the keyword "browser" means use navigate_browser:
- "browser, search for X" → navigate_browser (destination=google, goal=search query)
- "browser, search images of X" → navigate_browser (destination=google images, goal=search query)
- "browser, go to Gmail" → navigate_browser (destination=gmail, goal=open)
- "browser, open Shopify settings" → navigate_browser (destination=shopify, goal=settings)
- Any request starting with "browser" → navigate_browser. Always.
- Pass the user's search query as they said it. Do not rephrase or substitute search terms.
- When the result says "Login required", tell the user to enter credentials in the browser and say "done" when ready, then call navigate_browser again.

Document search — only when user says "search my documents" or "find in my files":
- "search my documents for X" → search_documents

Available projects: {projects}
"""

TOOLS = [
    {
        "name": "navigate_browser",
        "description": "Open a website, navigate to a page, or search the web. Use for: 'search for X on Google', 'search Google Images for X', 'open Gmail spam', 'go to Shopify settings'. Handles Google Search, Google Images, Google News, Google Maps, and any website navigation.",
        "parameters": {
            "type": "object",
            "properties": {
                "destination": {
                    "type": "string",
                    "description": "The website or search engine (e.g. 'gmail', 'google', 'google images', 'google news', 'shopify', 'figma')",
                },
                "goal": {
                    "type": "string",
                    "description": "What to do (e.g. 'search for drone lidar', 'spam folder', 'settings page')",
                },
            },
            "required": ["destination", "goal"],
        },
    },
    {
        "name": "search_documents",
        "description": "Search the user's LOCAL document archive (OneDrive files) by keywords. NOT for web search — use navigate_browser for Google.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "management",
        "description": "Access calendar, reminders, or email.",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": ["all", "calendar", "reminders", "email"]},
                "query": {"type": "string"},
            },
            "required": ["source"],
        },
    },
    {
        "name": "sleep",
        "description": "Go to sleep. Say 'sleep' or 'goodbye'.",
        "parameters": {"type": "object", "properties": {}},
    },
]


def main():
    projects_list = ", ".join(f"{k} ({v})" for k, v in PROJECTS.items())
    system = SYSTEM_PROMPT.format(projects=projects_list)

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    # Build tool declarations in the format Gemini expects
    from google.genai.types import (
        Content, Part, Tool, FunctionDeclaration, Schema, Type,
        GenerateContentConfig,
    )

    def _to_schema(props: dict, required: list) -> dict:
        """Convert our tool params to Gemini FunctionDeclaration format."""
        schema_props = {}
        for name, p in props.items():
            s = {"type": Type[p.get("type", "string").upper()]}
            if "description" in p:
                s["description"] = p["description"]
            if "enum" in p:
                s["enum"] = p["enum"]
            schema_props[name] = Schema(**s)
        return Schema(
            type=Type.OBJECT,
            properties=schema_props,
            required=required,
        )

    func_decls = []
    for t in TOOLS:
        params = t.get("parameters", {})
        schema = _to_schema(
            params.get("properties", {}),
            params.get("required", []),
        )
        func_decls.append(FunctionDeclaration(
            name=t["name"],
            description=t["description"],
            parameters=schema,
        ))

    tools = [Tool(function_declarations=func_decls)]
    config = GenerateContentConfig(
        system_instruction=system,
        tools=tools,
        temperature=0.7,
    )

    history = []

    print("\n  Gemini Chat (no pipecat). Type your message. Ctrl+C to quit.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nBye.")
            break

        if not user_input:
            continue

        history.append(Content(role="user", parts=[Part(text=user_input)]))

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=history,
            config=config,
        )

        # Process response — handle tool calls
        for candidate in response.candidates:
            for part in candidate.content.parts:
                if part.text:
                    print(f"Jarvis: {part.text}")
                if part.function_call:
                    fc = part.function_call
                    args = dict(fc.args) if fc.args else {}
                    print(f"  [TOOL] {fc.name}({json.dumps(args)})")

        # Add assistant response to history
        if response.candidates:
            history.append(response.candidates[0].content)

        # Keep history short
        if len(history) > 10:
            history = history[-6:]

        print()


if __name__ == "__main__":
    main()
