# NEXUS — Stage 1 Specification

## Project Vision

Nexus is a local desktop application that eliminates the overhead of finding, understanding, and organizing files across two OneDrive accounts (~1TB total) and multiple git repositories. It replaces the workflow of: `Find in Explorer → Read → Understand context → Do work → Figure out where to store` with a single intelligent interface: `Ask → Do → Done`.

The user (Ginés) works across 3 machines (work laptop, home workstation, travel laptop) with two OneDrive accounts (personal + Xer Technologies work) and two git accounts (personal + work). The core problem is that 90% of time is spent on logistics — locating files, understanding project context, creating boilerplate documents, filing things correctly — rather than on the actual intellectual work.

---

## Stage 1 Scope

Stage 1 delivers a **fully local application with no server component**. No EC2, no hosted backend. All intelligence runs locally via Claude API calls. The only external dependency is the Microsoft Graph API for OneDrive delta sync.

---

## Architecture

```
NEXUS (local app on each laptop)
│
├── Tauri UI (Rust + web frontend)
│   ├── Left panel: Chat interface (Claude Code session)
│   └── Right panel: File/folder browser (interactive, reacts to tool calls)
│
├── Claude Code SDK (@anthropic-ai/claude-code, embedded)
│   ├── Manages its own context window, conversation history, compression
│   ├── Manages tool calling orchestration, retries, error handling
│   ├── User logs into Claude once — no separate API key management
│   └── Calls custom Nexus tools registered by the app (see below)
│
├── Custom Tools (registered with Claude Code, implemented in Node.js/Python)
│   ├── navigate_index    — Read an index file from the .nexus tree
│   ├── read_document     — Extract content/structure from a Word file (python-docx)
│   ├── edit_document     — Apply changes to a Word file (python-docx)
│   ├── create_document   — Create new doc from company template (python-docx)
│   ├── read_live_document — Read live content from open Word instance (win32com/COM)
│   ├── open_file         — Launch file in native app (os shell)
│   ├── list_directory    — List folder contents (for browser panel drilling)
│   ├── move_file         — Relocate/rename a file
│   ├── show_in_browser   — Push file/folder results to the right panel UI
│   └── sync_index        — Trigger delta sync + Claude index update
│
├── Delta Sync Service (background, on app startup)
│   ├── Microsoft Graph API (delta endpoint for OneDrive changes)
│   ├── Claude processes changes following the fixed SOP
│   └── Writes updated index files to .nexus/ on OneDrive
│
└── OneDrive (synced across all machines)
    ├── [User's 1TB of actual files across both accounts]
    └── .nexus/ (index tree + config, syncs automatically)
        ├── root_index.md
        ├── {category}/index.md (hierarchical)
        ├── sync_state.json
        └── config.json
```

### Key Architectural Decisions

- **Embedded Claude Code, not a custom AI wrapper**: The chat panel IS a Claude Code session via the `@anthropic-ai/claude-code` SDK. Claude Code manages its own context window, conversation history, context compression, tool orchestration, streaming, and error handling. We do NOT build any of this ourselves. The user logs into Claude once and the app just works. This is the same Claude Code engine that runs in the terminal, but with a custom system prompt and custom tools tailored to Nexus.
- **Custom tools, not custom AI logic**: All Nexus-specific functionality (index navigation, Word editing, file operations) is exposed as tools that Claude Code can call natively — the same way Claude Code calls Read, Edit, Bash in the terminal. Claude decides when and how to use them. We build the tools, Claude does the thinking.
- **No EC2/server**: All processing happens locally. The hierarchical index files live on OneDrive and sync naturally across machines.
- **Local executor**: File operations (read, write, create, open) are local for speed. The embedded Claude Code handles all AI reasoning.
- **Hierarchical index, not flat database**: The 1TB filesystem is represented as a tree of small markdown summary files, not a single flat database. Claude navigates top-down through levels rather than loading everything into context.
- **Background sync on app startup**: Delta updates happen in the background when the app opens. The user can start working immediately with the last known index.

### What We Build vs. What Claude Code Provides

| Concern | Who handles it |
|---------|---------------|
| Context window management | Claude Code SDK (automatic) |
| Conversation history & compression | Claude Code SDK (automatic) |
| Tool calling orchestration | Claude Code SDK (automatic) |
| Streaming responses to UI | Claude Code SDK (automatic) |
| Retry / error handling | Claude Code SDK (automatic) |
| Authentication with Claude | Claude Code SDK (user logs in once) |
| Index navigation logic | Custom tool (`navigate_index`) |
| Word document operations | Custom tools (`read_document`, `edit_document`, `create_document`) |
| Live Word access | Custom tool (`read_live_document`) via COM |
| File open/move/create | Custom tools (`open_file`, `move_file`) |
| UI rendering (chat + browser) | Tauri app |
| Delta sync + index updates | Background service + Claude (via SOP) |
| System prompt with Nexus instructions | Config (tells Claude about the index tree, available tools, navigation rules) |

---

## Component 1: Hierarchical Index System

### Purpose

Represent the entire 1TB OneDrive contents as a navigable tree of lightweight summary files. Claude reads only the levels it needs (typically 3-4 files, ~200-300 lines total) instead of the impossible full file listing.

### Structure

```
.nexus/
├── root_index.md              # Level 0: top-level overview (~50 lines)
├── personal/
│   └── index.md               # Level 1: personal OneDrive summary
├── work/
│   ├── index.md               # Level 1: work OneDrive summary
│   ├── design_org/
│   │   ├── index.md           # Level 2: Design Organisation contents
│   │   ├── sensors/
│   │   │   ├── index.md       # Level 3: sensor sub-projects
│   │   │   ├── tdlas.md       # Level 3: TDLAS project detail
│   │   │   └── isr_camera.md  # Level 3: ISR Camera detail
│   │   └── reports/
│   │       └── index.md       # Level 3: reports listing
│   ├── customer_projects/
│   │   └── index.md
│   ├── flight_org/
│   │   └── index.md
│   ├── production_org/
│   │   └── index.md
│   └── general/
│       └── index.md
├── git_repos/
│   └── index.md               # Summary of all git repositories
└── sync_state.json            # Delta sync tokens + last update timestamps
```

### Index File Format

Each index file is a markdown file containing human-readable summaries that Claude can parse:

```markdown
# Design Organisation > Sensors

Last updated: 2026-03-24T09:15:00
File count: 47 | Folder count: 6

## TDLAS/ (Active)
Methane detection TDLAS sensor. Contains integration reports, calibration data,
manufacturer datasheets, and field test results. Main deliverable is the
integration report (XER-DO-RPT-032). 3 Word docs, 1 Excel, 1 PDF.
→ Detail: .nexus/work/design_org/sensors/tdlas.md

## ISR Camera/ (New — 2026-03-23)
ISR thermal imaging camera. Recently added. Contains setup guide and
specifications document. Not yet reviewed or integrated into any report.
2 Word docs.
→ Detail: .nexus/work/design_org/sensors/isr_camera.md

## SIYI/ (Active)
SIYI A2 tilting camera. Integration notes, firmware update logs, gimbal
configuration files. Linked to Customer Project X for drone payload.
5 Word docs, 3 config files.
→ Detail: .nexus/work/design_org/sensors/siyi.md
```

### Detail File Format (Leaf Level)

```markdown
# TDLAS Sensor Project

Path: OneDrive Work/Design Org/Sensors/TDLAS/
Last updated: 2026-03-20
Status: Active

## Files
- TDLAS_Integration_Report_v3.docx — Main integration report covering
  hardware setup, software architecture, and test results. 24 pages.
  Sections: Introduction, System Overview, Hardware Integration,
  Software Architecture, Test Results.
- TDLAS_Calibration_Data.xlsx — Field calibration measurements from
  March 2026. Contains raw sensor readings and computed concentrations.
- TDLAS_Datasheet.pdf — Manufacturer specification sheet. Reference only.

## Context
Part of the methane detection payload project. Related Monday task:
"Finalize TDLAS integration report". Interfaces with the onboard computer
(see Onboard_Computer/ folder).
```

### Navigation Logic

When the user asks a question, Claude navigates top-down:

1. Read `root_index.md` (~50 lines) → identify which top-level area
2. Read the relevant Level 1 index → identify which sub-area
3. Read the relevant Level 2 index → identify which project/folder
4. Read the detail file → get actual file names and descriptions
5. Total context used: ~200-300 lines across 3-4 files

Claude NEVER loads the entire index. It follows the tree.

---

## Component 2: Delta Sync + Index Update

### Purpose

Keep the hierarchical index up to date with actual OneDrive contents. Updates are performed by Claude Code following a fixed, reproducible procedure (see INDEX_BUILD_SPEC.md for the full Standard Operating Procedure).

### Two Update Modes

**Mode A: On App Startup (Automatic)**
The app fetches the delta from Microsoft Graph and presents the list of changes to the embedded Claude Code session. Claude follows the Index Update Procedure (defined in INDEX_BUILD_SPEC.md) to update the affected index files. This happens in the background — the user can start chatting immediately with the last known index.

**Mode B: Manual Session (User-Driven)**
The user opens a Claude Code session (in terminal or in the app) and pastes the Index Build/Update Procedure. Claude walks through it step by step, the user can watch and intervene. Used for: initial full build, deep re-indexing of a specific area, or cleanup.

### Flow (Both Modes)

```
1. Fetch delta from Microsoft Graph
   GET /me/drive/root/delta?token={last_token}
   → Returns ONLY files/folders changed since last sync

2. If no changes → done (takes <1 second)

3. If changes detected → Claude follows the Index Update Procedure:
   a. Read the list of changes (new/modified/deleted/moved paths)
   b. For each change, identify which index files are affected
   c. Read those index files
   d. Apply the Standard Operating Procedure rules:
      - Classify new items (what is it, where does it fit)
      - Write descriptions consistent with existing format
      - Update parent indexes if needed
      - Create new index files for new sub-areas if needed
      - Flag ambiguous placements for user review
   e. Write the updated index files to .nexus/
   f. Update sync_state.json with new token + timestamp
```

### Sync Token Mechanism

```json
{
  "personal_onedrive": {
    "delta_token": "abc123xyz",
    "last_sync": "2026-03-24T09:15:00Z"
  },
  "work_onedrive": {
    "delta_token": "def456uvw",
    "last_sync": "2026-03-24T09:15:00Z"
  }
}
```

The delta token is opaque to us — Microsoft Graph uses it to track what we've already seen. If two laptops open the app within minutes, the second one will get an empty delta response and skip the update.

### Handling Coworker Chaos

Coworkers may place files in unexpected locations. Claude follows the SOP rules:
- File placed in a logical location → update the relevant index with a new entry
- File placed in a weird location → still index it, but flag it: "⚠ Potentially misplaced: `Budget_2026.xlsx` found in `Sensors/TDLAS/`"
- New top-level folder created → create a new index branch, update root_index.md
- When unsure → flag for user review rather than guessing

### Why Claude (Not Haiku)

All index building and updating is done by the same Claude model used for chat. This means:
- Consistent quality — the same intelligence that answers your questions also maintains the map
- The procedure is a fixed plan (SOP) pasted into a session, ensuring reproducible results regardless of when or where it runs
- You can supervise, correct, and refine the process interactively
- No separate API integration needed — everything goes through Claude Code

---

## Component 3: Tauri Desktop Application (UI)

### Layout

Split-panel interface, summoned via global hotkey (e.g., `Alt+Space`) or launched from taskbar/start menu.

```
┌──────────────────────────────────────────────────────────┐
│  NEXUS                                        [─] [□] [×] │
├────────────────────────┬─────────────────────────────────┤
│                        │                                  │
│  CHAT PANEL            │  FILE BROWSER PANEL              │
│                        │                                  │
│  Claude-powered chat   │  Shows search results as         │
│  session. Persistent   │  clickable file/folder tree.     │
│  within app session.   │                                  │
│                        │  Scrollable.                     │
│  User types queries,   │                                  │
│  instructions, or      │  Folders: double-click to        │
│  requests here.        │  drill into contents.            │
│                        │                                  │
│  Claude responds with  │  Files: single-click to open     │
│  text + populates the  │  in native app (Word, PDF        │
│  right panel with      │  viewer, VS Code, etc.)          │
│  relevant files.       │                                  │
│                        │  Back button to return to        │
│                        │  search results.                 │
│                        │                                  │
│                        │  Shows: file name, path,         │
│                        │  last modified date, size,       │
│                        │  brief description from index.   │
│                        │                                  │
├────────────────────────┴─────────────────────────────────┤
│  [Status: Index updated 2 min ago | 3 new files found]    │
└──────────────────────────────────────────────────────────┘
```

### Chat Panel Capabilities

The chat panel is an embedded Claude Code session. The user interacts with it exactly like they would with Claude Code in a terminal, but the UI is graphical and the tools are Nexus-specific. Claude Code manages its own context, tool calls, and conversation history — the app just renders the output and reacts to tool calls (e.g., when Claude calls `show_in_browser`, the right panel updates).

The user can:

1. **Search for files/projects**
   - "Find my ISR camera docs"
   - "Where is the TDLAS integration report?"
   - "Show me everything related to drone payload integration"
   - → Claude navigates the index tree, populates the right panel with results

2. **Ask for file descriptions**
   - "Describe the TDLAS integration report"
   - "What sections does this document have?"
   - → Claude reads the actual file via python-docx (locally) and provides a summary

3. **Compare files**
   - "Compare these two reports — which one is more recent and what's different?"
   - → Claude reads both files and provides a comparison

4. **Write/modify Word documents**
   - "Add a section after Hardware Integration about TDLAS field operation. Here is the content: [text]"
   - → Claude determines the edit plan, python-docx applies it without opening Word
   - → Shows diff/preview in chat: what was added, where, section renumbering
   - → User can then click the file in the right panel to open and verify

5. **Create new documents**
   - "Create a test report for the ISR camera field trial, put it in Design Org > Reports"
   - → Loads company template (.dotx), populates title/header/footer/date/author/doc number
   - → Creates standard section structure
   - → Places user's content in appropriate sections
   - → Saves to correct OneDrive path
   - → Updates the index
   - → Shows the new file in the right panel, clickable to open

6. **Auto-file documents**
   - "I just created this report, where should it go?"
   - → Claude analyzes content + index structure, suggests the correct folder
   - → User confirms, file is moved automatically

7. **Ask questions about open documents (real-time)**
   - If a Word document is currently open, Claude can read its live state via COM automation (win32com)
   - "What does section 4 say about calibration?"
   - → Reads live document from Word process memory, no need to wait for save
   - When Word is not open, falls back to python-docx reading the saved file

### File Browser Panel Capabilities

- **Search results view**: After a chat query, shows the files/folders Claude found as a list
- **Folder navigation**: Double-click a folder to see its contents (reads from actual filesystem, not just index)
- **File opening**: Single-click a file to open it in its native application via Windows shell (`os.startfile()`)
- **Back navigation**: Return to the previous search results or parent folder
- **File metadata display**: Each item shows name, path, last modified date, size, and a brief description (from the index if available)
- **Post-creation view**: After Claude creates or modifies a file, it appears in this panel immediately, clickable to open and verify

### Interaction Between Panels

The two panels are linked:
- Chat query results populate the file browser
- Clicking a file in the browser can trigger Claude context ("you selected TDLAS_Report_v3.docx")
- After Claude creates/modifies a file, it appears in the browser
- Claude can reference items in the browser: "the first result is the one you want"

---

## Component 4: Document Engine (python-docx)

### Purpose

Read, write, create, and modify Word documents programmatically without opening Word. All operations happen locally on the machine's filesystem.

### Capabilities

| Operation | Implementation |
|-----------|---------------|
| **Read structure** | Extract headings, sections, paragraph count, styles from .docx |
| **Read content** | Extract full text content for Claude to analyze |
| **Add section** | Insert heading + paragraphs at correct position, renumber subsequent sections |
| **Modify section** | Find section by heading, replace/append content |
| **Create from template** | Load company .dotx, populate metadata + sections |
| **Fix formatting** | Scan paragraphs, enforce correct named styles |
| **Move/copy sections** | Between documents |
| **Update header/footer** | Doc number, date, revision, author |
| **Batch format check** | Scan folder of docs, flag style violations |

### Document Edit Flow

```
User: "Add a section about field operation after Hardware Integration"
    │
    ▼
Python agent reads .docx with python-docx (~50ms)
    → Extracts document skeleton (headings, structure)
    │
    ▼
Claude receives:
    - Document skeleton (headings + positions)
    - User instruction
    - User content
    - Company style rules (from config)
    │
    ▼
Claude returns structured edit plan:
    {
      "operations": [
        {"type": "insert_heading", "after": "3. Hardware Integration",
         "text": "4. TDLAS Field Operation", "style": "Heading 1"},
        {"type": "insert_paragraphs", "content": ["..."],
         "style": "Xer Body Text"},
        {"type": "renumber_headings", "from": "Software Architecture", "shift": 1}
      ]
    }
    │
    ▼
Python agent applies operations via python-docx (~100ms)
    │
    ▼
Chat panel shows diff preview:
    "+ NEW Section 4. TDLAS Field Operation (3 paragraphs, ~150 words)"
    "~ RENUMBERED Software Architecture → 5"
    "~ RENUMBERED Test Results → 6"
    "⚠ TOC will need refresh (open in Word → F9)"
    │
    ▼
File appears in browser panel, clickable to open and verify
```

### New Document Creation Flow

```
User: "Create a test report for ISR camera field trial, Design Org > Reports"
    │
    ▼
Claude determines:
    - Template: company test report .dotx
    - Title: "ISR Camera Field Trial Report"
    - Doc number: XER-DO-RPT-048 (auto-incremented from index)
    - Date, author: auto-populated
    - Save path: OneDrive Work/Design Org/Reports/XER-DO-RPT-048.docx
    │
    ▼
Python agent:
    - Loads template
    - Populates metadata fields (title, header, footer, date, author, doc number)
    - Creates standard section structure
    - Inserts user content into appropriate sections
    - Saves to determined path
    │
    ▼
Index updated (new file added to relevant index files)
File shown in browser panel, clickable to open
```

### Live Document Interaction (COM Automation)

When a Word document is open and the user asks questions about it:

```
Python agent connects to running Word process via win32com:
    word = win32com.client.GetActiveObject("Word.Application")
    doc = word.ActiveDocument

    # Read live content (sub-second, no save needed)
    content = doc.Content.Text
    sections = [p.Range.Text for p in doc.Paragraphs if p.Style.NameLocal.startswith("Heading")]
```

This enables real-time Q&A about the document currently being viewed/edited in Word.

Fallback: If Word is not running or COM fails, read the last saved version via python-docx.

### Style Registry (Configuration)

```json
{
  "company_template": "path/to/xer_template.dotx",
  "doc_numbering": {
    "pattern": "XER-{dept}-{type}-{number:03d}",
    "departments": {"DO": "Design Org", "FO": "Flight Org", "CO": "Customer"},
    "types": {"RPT": "Report", "MOM": "Minutes", "SPC": "Specification"}
  },
  "required_styles": {
    "headings": "Heading 1, Heading 2, Heading 3",
    "body": "Xer Body Text",
    "table": "Xer Table"
  },
  "folder_rules": {
    "test_reports": "Design Org/Reports/",
    "meeting_notes": "General/Meetings/",
    "specifications": "Design Org/Specifications/"
  }
}
```

### Known Limitations

- **Table of Contents**: python-docx cannot recalculate TOC. Workaround: auto-open Word via COM, trigger F9 refresh, save, close (~3 seconds).
- **Embedded charts/SmartArt**: Preserved but cannot be modified programmatically.
- **Track changes**: Cannot add tracked changes via python-docx. Version history maintained in the index instead.
- **Complex table formatting**: Basic tables work, heavily merged/styled tables may need manual review.

---

## Component 5: Custom Tools (Claude Code Integration)

### Purpose

These are the tools registered with the embedded Claude Code SDK. Claude Code calls them as needed during conversation — exactly like it calls Read, Edit, Bash in the terminal. We implement the tools; Claude decides when and how to use them.

### Tool Definitions

Each tool is registered with Claude Code via the SDK's tool interface. Claude sees a name, description, and parameter schema. It calls them autonomously during conversation.

```
navigate_index(path)
  → Reads a .nexus index file and returns its content
  → Claude uses this to walk the hierarchical tree top-down
  → Example: navigate_index("work/design_org/sensors/index.md")

read_document(file_path)
  → Uses python-docx to extract structure + content from a .docx file
  → Returns: headings, sections, paragraph text, styles, metadata
  → Used when Claude needs to describe, summarize, or analyze a document

edit_document(file_path, operations)
  → Applies a list of edit operations to a .docx file via python-docx
  → Operations: insert_heading, insert_paragraphs, modify_section,
    renumber_headings, apply_style
  → Returns: diff summary of what changed

create_document(template, title, save_path, metadata, content)
  → Creates a new .docx from company template via python-docx
  → Populates: title, header, footer, date, author, doc number, sections
  → Saves to specified OneDrive path
  → Returns: path of created file

read_live_document()
  → Connects to running Word instance via win32com COM automation
  → Returns live document content without requiring save
  → Fallback: if Word not running, returns error suggesting read_document instead

open_file(file_path)
  → Opens a file in its native application via OS shell (os.startfile)
  → Returns: confirmation

move_file(source, destination)
  → Moves/renames a file on the filesystem
  → Returns: new path

list_directory(path)
  → Lists contents of a directory (files + folders with metadata)
  → Used for the browser panel drilling

show_in_browser(items)
  → Pushes a list of file/folder results to the right panel UI
  → Items include: path, name, type (file/folder), size, modified date, description
  → This is how Claude populates the file browser panel

sync_index()
  → Triggers delta sync + Haiku index update manually
  → Returns: summary of changes found and applied
```

### System Prompt (Nexus Instructions for Claude Code)

When the Claude Code session starts, it receives a system prompt that tells it:
- What Nexus is and what tools are available
- Where the .nexus index tree is located
- The navigation rules (start at root_index.md, go top-down, max 5 files per query)
- The company style rules and template paths
- To use `show_in_browser` whenever it finds files the user might want to click
- To always confirm before modifying or creating documents

### Startup Sequence

```
1. Tauri app launches
2. Load config from .nexus/config.json (OneDrive path)
3. Background: Start delta sync (Microsoft Graph + Claude index update via SOP)
4. Initialize Claude Code SDK session with:
   - Custom system prompt (Nexus instructions)
   - Custom tools (all tools listed above)
   - Streaming enabled (responses stream to chat panel)
5. User can start chatting immediately (uses last known index)
6. When delta sync completes: subtle notification in status bar
```

### Dependencies

```
@anthropic-ai/claude-code  — Claude Code SDK (embedded, handles ALL AI: chat, index builds, index updates)
python-docx                — Word document manipulation (called from tools)
pywin32 (win32com)         — COM automation for live Word access
httpx / requests           — Microsoft Graph API calls (delta sync)
msal                       — Microsoft authentication library for Graph API tokens
```

Note: ALL AI goes through the Claude Code SDK — both user chat and index maintenance. No separate `anthropic` package needed. Claude Code manages its own API connection via the user's logged-in session. Index updates use the same Claude model, following a fixed SOP to ensure consistent results.

---

## Component 6: Microsoft Graph Integration

### Purpose

Access both OneDrive accounts (personal + Xer Technologies work) to perform delta sync of filesystem structure.

### Authentication

- Register an Azure AD app (one-time setup)
- OAuth2 device code flow or authorization code flow for initial auth
- Refresh tokens stored securely in .nexus/config.json (encrypted or OS keychain)
- Supports both personal Microsoft account and work/school account (Xer)

### Delta Sync API

```
GET https://graph.microsoft.com/v1.0/me/drive/root/delta
Authorization: Bearer {access_token}

Response: {
  "value": [
    {
      "id": "...",
      "name": "ISR_Camera_Setup.docx",
      "parentReference": {"path": "/drive/root:/Design Org/Sensors/ISR Camera"},
      "file": {"mimeType": "application/vnd.openxmlformats-officedocument..."},
      "size": 245760,
      "lastModifiedDateTime": "2026-03-23T14:30:00Z",
      "createdBy": {"user": {"displayName": "Ginés Rodríguez"}}
    },
    ...
  ],
  "@odata.deltaLink": "...?token=new_token_here"
}
```

The delta endpoint returns only items that changed since the last token. The new token from `@odata.deltaLink` is saved for the next sync.

### What We Extract (Metadata Only)

- File/folder name
- Full path
- Size
- Last modified date
- Created by / modified by
- MIME type
- Whether it was created, modified, or deleted

We do NOT download file contents during sync. Content is only read on-demand when Claude needs to analyze a specific file (and that happens locally since OneDrive syncs the actual files to the machine).

---

## Initial Setup (One-Time)

### Step 1: First Index Build

Before the app is usable, the full filesystem structure needs to be indexed once. This is done by running a Claude Code session with the **Index Build SOP** (Standard Operating Procedure — see INDEX_BUILD_SPEC.md for the full procedure).

```
1. Authenticate with Microsoft Graph for both OneDrive accounts
2. Run the crawl script to recursively list all files/folders
   (Graph API, not local filesystem) — outputs raw tree as JSON
3. Open a Claude Code session and paste the Index Build SOP
4. Claude walks through the raw tree area by area, following the SOP:
   — Creates root_index.md
   — Creates Level 1 indexes for each major area
   — Creates Level 2+ indexes for sub-areas
   — Creates detail files for leaf-level project folders
   — Asks the user for clarification on ambiguous areas
5. User reviews top-level summaries and corrects if needed
6. Save all index files to .nexus/ on OneDrive
7. Save initial sync tokens to sync_state.json
```

This initial build may take 30-60 minutes as a supervised Claude Code session. It only happens once. The user is present and can correct/guide Claude during the process.

### Step 2: Company Style Configuration

- Provide the company Word template (.dotx file)
- Define document numbering conventions
- Define folder placement rules
- Define standard section structures per document type
- All stored in .nexus/config.json

### Step 3: App Installation

- Install Tauri app (single .exe or .msi installer, bundles Claude Code SDK)
- Install Python dependencies for custom tools (pip install python-docx pywin32 msal httpx anthropic)
- Log into Claude (Claude Code handles authentication — same login as terminal Claude Code)
- Configure Microsoft Graph credentials (one-time OAuth2 flow for each OneDrive account)
- App creates a startup shortcut (optional)

---

## Technology Stack Summary

| Component | Technology | Reason |
|-----------|-----------|--------|
| Desktop UI | Tauri (Rust + HTML/CSS/JS) | Lightweight (~5MB), native file access, cross-platform |
| AI engine (all) | Claude Code SDK (`@anthropic-ai/claude-code`) | Embedded Claude Code — manages context, tools, conversation, streaming. Handles both chat AND index updates. No separate API needed. User logs in with existing Claude account. |
| Custom tools | Node.js + Python (child processes) | Tools registered with Claude Code SDK. Node.js for orchestration, Python for python-docx and win32com |
| Word manipulation | python-docx | Full .docx read/write without Word |
| Live Word access | pywin32 (COM) | Real-time document reading from running Word |
| OneDrive sync | Microsoft Graph API | Delta endpoint for efficient change detection |
| Auth (Microsoft) | MSAL | OAuth2 for Graph API access |
| Auth (Claude) | Claude Code built-in | User logs in once, SDK handles tokens |
| Index storage | Markdown files on OneDrive | Human-readable, syncs across machines, Claude-native |
| Config/state | JSON files on OneDrive (.nexus/) | Simple, syncs across machines |

---

## What Stage 1 Does NOT Include

These are explicitly deferred to future stages:

- **Monday.com integration** — Task sync, auto-linking tasks to files
- **Outlook/Calendar integration** — Email parsing, calendar-aware scheduling
- **Voice agent** — Speech-to-text input, text-to-speech responses
- **Increased Claude autonomy** — Auto-executing multi-step workflows without confirmation
- **EC2 server** — All processing is local in Stage 1
- **Embedding-based semantic search** — Index navigation uses Claude's reasoning, not vector search
- **PDF manipulation** — Read-only for PDFs in Stage 1 (display content, no editing)
- **Excel manipulation** — Read-only for spreadsheets in Stage 1
- **Git repository deep integration** — Git repos are indexed by structure but no commit analysis
- **Multi-user collaboration features** — Single-user tool, coworker files are indexed but no shared editing
- **Mobile access** — Desktop only

---

## Success Criteria

Stage 1 is complete when:

1. The app opens on any of the 3 laptops and the index is current within 24 hours
2. The user can ask "find my [X] files" and get correct, clickable results in under 5 seconds
3. The user can click any file in the results and it opens in its native app
4. The user can navigate folders in the right panel without touching Windows Explorer
5. The user can ask Claude to describe/summarize any Word document's contents
6. The user can ask Claude to compare two documents
7. The user can ask Claude to add/modify sections in a Word document without opening it
8. The user can ask Claude to create a new document from the company template with minimal input
9. After creation/modification, the file is stored in the correct OneDrive location
10. The hierarchical index updates automatically on app startup with new/changed/deleted files
11. The entire 1TB structure is navigable without ever exceeding Claude's context window
