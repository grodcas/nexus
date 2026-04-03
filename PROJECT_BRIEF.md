# NEXUS — Complete Project Brief

> This document captures the full design rationale, decisions, and specifications for the Nexus project.
> It is intended to be self-contained: any Claude instance reading this file should be able to
> understand the project fully and continue working on it without needing the original conversation.

---

## 1. The Problem

Ginés is a drone/UAV engineer at Xer Technologies who works across:
- **3 machines**: work laptop, home workstation, travel laptop
- **2 OneDrive accounts**: personal + Xer Technologies (work), totaling ~1TB of files
- **2 git accounts**: personal + work
- **File types**: primarily Word documents (.docx), PDFs, code, Excel spreadsheets
- **External tools**: Monday.com (task management), Outlook (email), Calendar

The core pain: **90% of work time is spent on logistics, not intellectual work.** The workflow looks like:

1. **Find** — Where is the file/project? Buried in 1TB of folders. Takes ages to locate.
2. **Read** — Open it, re-read to understand context from last time.
3. **Do the work** — Often just writing a few sentences or updating a section. The smallest part.
4. **Store** — Figure out the right place to save it, apply correct formatting, naming, etc.
5. **Format** — Ensure the document follows company style (header, footer, numbering, sections). Creating a new document from a template requires scrapping placeholder content, fixing title/header/footer/index/citations — all boilerplate before writing a single useful sentence.

This cycle repeats for every task. When returning to a task after weeks/months, the "find + understand" phase is especially painful.

---

## 2. The Vision

A single local desktop application called **Nexus** that replaces the entire cycle with: **Ask → Do → Done.**

You open the app, tell Claude what you need to work on, and it:
- Finds the relevant files instantly (no Explorer browsing)
- Shows them in a clickable panel (one click to open)
- Can describe, compare, or summarize documents for you
- Can edit Word documents without opening Word (add sections, fix formatting)
- Can create new documents from company templates with just your content input
- Automatically stores files in the correct location
- Keeps its knowledge of your filesystem up to date

---

## 3. Key Design Decisions (and Why)

These decisions were reached through iterative discussion. Each represents a deliberate choice over alternatives that were considered and rejected.

### 3.1 Embedded Claude Code, not a custom AI wrapper

**Decision**: The app embeds the Claude Code SDK (`@anthropic-ai/claude-code`) directly. The chat panel IS a Claude Code session.

**Why**: Building a custom AI wrapper with context management, tool orchestration, conversation history, compression, streaming, and error handling would take months and always be worse than the real thing. Claude Code already handles all of this. We just add custom tools (index navigation, Word editing, file operations) and a custom system prompt. The user logs into Claude with their existing account — no API key management.

**What this means**: We build the tools and the UI. Claude Code does the thinking.

### 3.2 Hierarchical index tree, not a flat database

**Decision**: The 1TB filesystem is represented as a tree of ~200 small markdown files, organized by depth. Claude navigates top-down, reading 3-4 files (~200-300 lines) per query.

**Why**: A flat file listing of 50,000+ files would be millions of tokens — impossible to fit in context. Keyword search on filenames misses semantic meaning. Vector/embedding search can't answer structural questions ("show me everything in Design Org"). The tree lets Claude *reason* its way to the answer, the same way a knowledgeable colleague would navigate: "sensors are in Design Org, under the Sensors folder, TDLAS is one of them."

**Key properties**:
- Bounded context: works for 1TB, would work for 10TB
- Human-readable: you can open the markdown files yourself
- Semantic: each entry has a description, not just a filename
- Self-maintaining: updates only touch affected branches

### 3.3 No server / No EC2

**Decision**: Everything runs locally. No hosted backend.

**Why**: Initially we considered an EC2 server for heavy indexing and always-on sync. Through discussion, we realized: (a) the index is lightweight enough to maintain locally, (b) file operations MUST be local for speed (reading/writing Word docs), (c) the delta sync can run on app startup on whichever laptop is used first that day, (d) the index files live on OneDrive and sync across machines automatically. An EC2 adds cost and complexity for no real benefit in Stage 1.

### 3.4 Claude for index updates, not Haiku

**Decision**: All AI — both user chat and index maintenance — goes through the same Claude model via Claude Code SDK.

**Why**: Initially we planned to use Haiku (cheaper, faster) for index classification. But the user prefers using Claude everywhere for consistent quality and a simpler architecture. The index update process follows a fixed Standard Operating Procedure (SOP) — a plan that is pasted into a Claude Code session to ensure reproducible results. This means index builds/updates can be supervised interactively. The cost difference is negligible for this use case.

### 3.5 Tauri for the desktop app, not Electron

**Decision**: The UI is built with Tauri (Rust backend + web frontend).

**Why**: Tauri produces a ~5MB binary vs Electron's ~150MB+. It has native file access, low memory footprint, and runs on all 3 machines. The UI is a split panel: chat on the left, file browser on the right.

### 3.6 OneDrive delta API via Microsoft Graph, not local filesystem watching

**Decision**: Filesystem changes are detected via the Microsoft Graph delta endpoint, not by watching the local filesystem with watchdog.

**Why**: The delta endpoint returns exactly what changed since the last sync token, across all machines. It works even if the laptop was off for a week — the token catches up. Local filesystem watching would miss changes made on other machines until OneDrive syncs them, and would require the watcher to run continuously.

### 3.7 Index files live on OneDrive in a .nexus/ folder

**Decision**: The hierarchical index, config, and sync state are stored as files in a `.nexus/` folder on OneDrive.

**Why**: This gives automatic cross-machine sync for free. No separate sync mechanism needed. The index is just markdown files + JSON config — lightweight, human-readable, and OneDrive handles versioning/conflict resolution. Total size: ~2-10MB for a 1TB filesystem.

---

## 4. Architecture

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
│   └── Calls custom Nexus tools registered by the app
│
├── Custom Tools (registered with Claude Code)
│   ├── navigate_index    — Read an index file from the .nexus tree
│   ├── read_document     — Extract content/structure from a Word file (python-docx)
│   ├── edit_document     — Apply changes to a Word file (python-docx)
│   ├── create_document   — Create new doc from company template (python-docx)
│   ├── read_live_document — Read live content from open Word instance (win32com/COM)
│   ├── open_file         — Launch file in native app (os shell)
│   ├── list_directory    — List folder contents (for browser panel drilling)
│   ├── move_file         — Relocate/rename a file
│   ├── show_in_browser   — Push file/folder results to the right panel UI
│   └── sync_index        — Trigger delta sync + index update
│
├── Delta Sync Service (background, on app startup)
│   ├── Microsoft Graph API (delta endpoint for OneDrive changes)
│   ├── Claude processes changes following the fixed SOP
│   └── Writes updated index files to .nexus/ on OneDrive
│
└── OneDrive (synced across all machines)
    ├── [User's 1TB of actual files across both accounts]
    └── .nexus/ (index tree + config, syncs automatically)
```

### What Claude Code SDK provides (we do NOT build):
- Context window management & compression
- Conversation history
- Tool calling orchestration
- Streaming responses
- Retry / error handling
- Authentication

### What we build:
- Tauri UI (chat panel + file browser panel)
- Custom tools (Node.js/Python implementations)
- Delta sync background service
- Index build/update SOP
- Company style registry config

---

## 5. The Hierarchical Index System

### Why not flat?

| Approach | Why it fails |
|----------|-------------|
| Flat SQLite with 50K rows | Too many tokens for context. Keyword search misses semantics. |
| Vector/embedding search | Can't answer structural questions. Needs vector DB infrastructure. |
| Full-text search (Elasticsearch) | Requires indexing 1TB of content. Overkill. No project context. |

### How the tree works

The index is a tree of small markdown files. Each file describes one area of the filesystem in 30-150 lines.

```
Level 0: root_index.md (~50 lines)
  → "Design Org has hardware docs, sensors, reports..."

Level 1: work/index.md (~100 lines)
  → Lists departments with 1-2 line summaries

Level 2: work/design_org/index.md (~100 lines)
  → Lists sub-areas: Sensors, Reports, PCB...

Level 3: work/design_org/sensors/index.md (~50 lines)
  → Lists sensor projects: TDLAS, SIYI, ISR Camera

Level 4: work/design_org/sensors/tdlas.md (~30 lines)
  → Lists actual files with descriptions
```

When the user asks "find my TDLAS report", Claude reads root → design_org → sensors → tdlas. Four files, ~200 lines total. Works instantly regardless of filesystem size.

### Branching rules

- Folders with ≤ 5 items → inline in parent index (no separate file)
- Folders with 6-30 items → get their own detail .md file
- Folders with > 30 items or subfolders → get an index.md that summarizes + points to children
- Max depth: 5 levels (flatten beyond that)
- Single-file folders → just a line in the parent

### Index file format

```markdown
# Design Organisation > Sensors

Last updated: 2026-03-24T09:15:00
File count: 47 | Folder count: 6

## TDLAS/ (Active)
Methane detection TDLAS laser sensor. Integration reports covering hardware
and software setup, field calibration data, and manufacturer datasheet.
Main deliverable: integration report (v3, 24 pages).
→ Detail: .nexus/work/design_org/sensors/tdlas.md

## ISR Camera/ (New — 2026-03-23)
ISR thermal imaging camera. Recently added. Setup guide and specifications.
→ Detail: .nexus/work/design_org/sensors/isr_camera.md
```

### Navigation algorithm (encoded in Claude's system prompt)

1. ALWAYS start by reading root_index.md
2. Identify the most likely area based on the query
3. Read that area's index.md
4. If the answer is visible, respond
5. If deeper, follow the "→ Detail:" pointer
6. If uncertain between areas, read both (still ~200-300 lines)
7. NEVER read more than 5 index files per query
8. If not found after 5 files, ask the user for more context

---

## 6. Index Build Process

### Initial build (one-time, supervised)

1. **Crawl**: Microsoft Graph API recursively lists all files/folders in both OneDrives. Metadata only (names, paths, sizes, dates). Outputs raw JSON tree. ~5-15 minutes.

2. **Chunk**: Split the tree by natural folder boundaries. Each major folder becomes a chunk. Chunking rules: max 100 items per chunk, depth-based splitting.

3. **Classify (bottom-up)**: The user pastes the Index Build SOP into a Claude Code session. Claude processes chunks from deepest to shallowest:
   - Leaf folders first → writes detail files
   - Mid-level folders → writes summary indexes referencing child details
   - Top-level → writes high-level summaries
   - Root → writes root_index.md

4. **Review**: User validates the top 2 levels, corrects any misclassifications.

5. **Baseline**: Save delta sync token. System is live for incremental updates.

Total time: ~30-60 minutes supervised. Happens once.

### Incremental updates (on every app startup)

1. App calls Microsoft Graph delta endpoint: "what changed since last token?"
2. If nothing → done in <1 second
3. If changes → Claude follows the Index Update SOP:
   - Reads the list of changes (new/modified/deleted/moved)
   - Identifies which index files are affected
   - Reads those index files
   - Classifies new items, updates descriptions, creates new index files if needed
   - Flags ambiguous placements for user review
   - Writes updated index files
   - Saves new sync token

This runs in the background. The user can start chatting immediately with the last known index.

### The SOP approach

Both the initial build and incremental updates use a fixed, written procedure (Standard Operating Procedure). This ensures:
- Reproducible results regardless of which machine or session runs it
- The user can supervise and correct during the process
- The plan can be refined over time based on experience
- No magic — everything Claude does is explicit and auditable

---

## 7. User Interface

### Layout

```
┌──────────────────────────────────────────────────────────┐
│  NEXUS                                        [─] [□] [×] │
├────────────────────────┬─────────────────────────────────┤
│                        │                                  │
│  CHAT PANEL            │  FILE BROWSER PANEL              │
│                        │                                  │
│  Embedded Claude Code  │  Clickable file/folder tree      │
│  session. Type queries │  Scrollable                      │
│  and instructions.     │  Double-click folder → drill in  │
│                        │  Click file → opens in native app│
│                        │  Back button → previous results  │
│                        │  Shows: name, path, date, size,  │
│                        │  description from index           │
│                        │                                  │
├────────────────────────┴─────────────────────────────────┤
│  [Status: Index updated 2 min ago | 3 new files found]    │
└──────────────────────────────────────────────────────────┘
```

### What the user can do

1. **Search for files**: "Find my ISR camera docs" → Claude navigates index, shows results in browser panel
2. **Describe files**: "What sections does this report have?" → Claude reads the file via python-docx
3. **Compare files**: "Compare these two reports" → Claude reads both and summarizes differences
4. **Edit Word docs without opening them**: "Add a section about field operation after section 3" → python-docx applies the edit, shows diff preview
5. **Create new documents**: "Create a test report for ISR camera, put it in Design Org > Reports" → loads company template, populates metadata, creates sections, saves to correct path
6. **Auto-file**: "Where should this report go?" → Claude analyzes content + index, suggests folder, moves on confirmation
7. **Ask about open documents**: If Word is open, Claude reads live content via COM automation (win32com) — no save needed

### Panel interaction

- Chat results populate the file browser
- Clicking a file in the browser gives Claude context about the selection
- After creating/modifying a file, it appears in the browser immediately
- Claude uses the `show_in_browser` tool to push results to the right panel

---

## 8. Document Engine

### Technology: python-docx + win32com

- **python-docx**: Reads/writes .docx files at the XML level. Handles headings, paragraphs, styles, tables, headers, footers. Works without Word installed. Instant (~50-100ms per operation).
- **win32com (COM automation)**: Connects to a running Word instance to read live document content in real-time. Sub-second, no save needed.

### Edit flow

```
User: "Add a section about field operation after Hardware Integration"
  → python-docx extracts document skeleton (headings, structure) [~50ms]
  → Claude receives skeleton + user instruction + company style rules
  → Claude returns structured edit plan (JSON)
  → python-docx applies: insert heading, insert paragraphs, renumber sections [~100ms]
  → Chat shows diff preview
  → File appears in browser, clickable to open and verify
```

### New document flow

```
User: "Create a test report for ISR camera field trial, Design Org > Reports"
  → Claude determines: template, title, doc number (auto-incremented), date, author, save path
  → python-docx: loads template, populates metadata, creates sections, inserts content
  → Saves to OneDrive path
  → Index updated
  → File shown in browser panel
```

### Style Registry

A config file defining:
- Company Word template (.dotx path)
- Document numbering scheme (e.g., XER-DO-RPT-048)
- Required styles (Heading 1, Xer Body Text, etc.)
- Folder placement rules (test reports → Design Org/Reports/)
- Standard section structures per document type

### Known limitations

- **TOC**: python-docx can't recalculate Table of Contents. Workaround: COM opens Word, triggers F9, saves, closes (~3s).
- **Embedded charts/SmartArt**: Preserved but not modifiable.
- **Track changes**: Not supported. Version history via index instead.

---

## 9. Microsoft Graph Integration

### Authentication
- Register Azure AD app (one-time)
- OAuth2 flow for each OneDrive account (personal + work)
- Refresh tokens stored in .nexus/config.json or OS keychain
- MSAL library handles token refresh

### Delta endpoint
```
GET https://graph.microsoft.com/v1.0/me/drive/root/delta?token={last_token}
```
Returns only items changed since last sync. The new token is saved for next time. If two laptops check within minutes, the second gets an empty response and skips the update.

### What we extract (metadata only)
- File/folder name, full path, size, last modified date
- Created by / modified by
- MIME type
- Whether created, modified, or deleted
- We do NOT download file contents — only on-demand when Claude needs to analyze a specific file locally

---

## 10. Technology Stack

| Component | Technology |
|-----------|-----------|
| Desktop UI | Tauri (Rust + HTML/CSS/JS) |
| AI engine | Claude Code SDK (`@anthropic-ai/claude-code`) |
| Custom tools | Node.js + Python child processes |
| Word manipulation | python-docx |
| Live Word access | pywin32 (win32com/COM) |
| OneDrive sync | Microsoft Graph API |
| MS Auth | MSAL |
| Claude Auth | Claude Code built-in |
| Index storage | Markdown files on OneDrive (.nexus/) |
| Config/state | JSON files on OneDrive (.nexus/) |

---

## 11. Staging

### Stage 1 (current scope)
Everything described in this document. Local app, index system, file search, Word editing, document creation, delta sync.

### Future stages (explicitly out of scope for Stage 1)
- Monday.com integration (task sync, auto-link tasks to files)
- Outlook/Calendar integration (email parsing, task extraction)
- Voice agent (speech-to-text input, text-to-speech responses)
- Increased Claude autonomy (auto-executing multi-step workflows)
- PDF editing (read-only in Stage 1)
- Excel editing (read-only in Stage 1)
- Git deep integration (repos indexed by structure only, no commit analysis)
- Multi-user collaboration
- Mobile access

---

## 12. Success Criteria

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

---

## 13. File Map

```
nexus/
├── PROJECT_BRIEF.md       ← This file. Complete project context for any Claude instance.
├── STAGE1_SPEC.md          ← Detailed technical specification with component-level detail.
└── INDEX_BUILD_SPEC.md     ← Deep dive on the hierarchical index: why, how, build process,
                              tree structure, branching rules, navigation logic, update process.
```

To continue work on this project in a new Claude session, start by reading `PROJECT_BRIEF.md` for full context, then reference `STAGE1_SPEC.md` and `INDEX_BUILD_SPEC.md` for implementation details.
