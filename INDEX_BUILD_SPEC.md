# NEXUS — Hierarchical Index Build Process

## Why a Hierarchical Tree (Not a Flat DB)

### The Problem with Flat Approaches

| Approach | Why it fails for 1TB |
|----------|---------------------|
| **Flat file list in SQLite** | A 1TB OneDrive with ~50,000+ files produces a table that's millions of tokens. Claude can never see it all at once. You'd need to pre-filter with keyword search, which misses context ("find my sensor docs" won't match "SIYI_A2_config.xml"). |
| **Embedding/vector search** | Good for "find similar", bad for "where does this belong?" and "what's in this area?". Can't answer structural questions like "show me everything in Design Org". Requires a vector DB, adds complexity. |
| **Full-text search (Elasticsearch)** | Requires indexing file contents (1TB!). Overkill. Doesn't understand project context. Returns individual files, not navigable structure. |

### Why a Hierarchical Tree Works

Claude is a reasoning engine, not a search engine. The tree lets Claude **think** its way to the answer:

```
"Find my ISR camera docs"

Claude thinks: "ISR camera is hardware → probably in Design Org →
probably under Sensors or Equipment → let me check"

Reads 3 small files (root → design_org → sensors) → found it.
Total tokens: ~500. Time: ~2 seconds.
```

This mirrors how a human who knows the filesystem would navigate. We're giving Claude the same mental map a knowledgeable colleague would have.

### Key Properties

- **Bounded context per query**: Never more than 3-5 index files (~200-500 lines) regardless of total filesystem size. Works for 1TB, would work for 10TB.
- **Human-readable**: The index files are markdown. You can open them yourself and understand the structure. They're also debuggable — if Claude makes a wrong turn, you can see why.
- **Semantic, not just structural**: Each entry has a description, not just a file name. "TDLAS/ — methane detection sensor, integration reports and calibration data" is searchable by meaning, not just keywords.
- **Self-maintaining**: The delta sync + Claude update (via SOP) keeps descriptions current. New folders get classified and described automatically.
- **No extra infrastructure**: Just markdown files on OneDrive. No database server, no vector DB, no search engine.

---

## The Initial Build Process (One-Time)

### Overview

```
Phase A: Crawl          → Get full filesystem tree from Microsoft Graph
Phase B: Chunk          → Split tree into processable segments
Phase C: Classify       → Claude writes index files bottom-up (via SOP)
Phase D: Review         → User validates/corrects top-level summaries
Phase E: Baseline       → Save sync token, system is live
```

### Phase A: Crawl the OneDrive Trees

**Method**: Microsoft Graph API recursive listing (NOT local filesystem scan — the API is faster and doesn't require all files to be synced locally).

```python
# Pseudocode
def crawl_onedrive(drive_id):
    """Recursively list all items via Graph API"""
    queue = ["/"]       # start at root
    tree = {}

    while queue:
        path = queue.pop()
        children = graph_api.list_children(drive_id, path)

        for item in children:
            tree[item.path] = {
                "name": item.name,
                "type": "folder" if item.folder else "file",
                "size": item.size,
                "modified": item.last_modified,
                "modified_by": item.last_modified_by,
                "mime_type": item.mime_type,      # for files
                "child_count": item.child_count,  # for folders
            }
            if item.folder:
                queue.append(item.path)

    return tree
```

**Output**: A raw tree dict — every file and folder with metadata. No content, just structure. For 50,000 files this is ~10-20MB of JSON.

**Time**: ~5-15 minutes depending on API rate limits. Microsoft Graph allows ~10,000 requests/10 minutes. With batching and pagination, a 50K file tree takes ~5 minutes.

**Important**: We also get the initial delta sync token here, which becomes our baseline for future updates.

### Phase B: Chunk the Tree for Processing

Claude can't process 50,000 entries at once. We split the tree into chunks that map to natural boundaries.

**Strategy: Depth-based chunking**

```
Depth 0-1:  Root + top-level folders        → Chunk 0 (small, ~20-50 items)
Depth 2:    Each top-level folder's children → Chunk per folder (~10-100 items each)
Depth 3+:   Each subfolder's full subtree    → Chunk per subfolder
```

**Concretely for your OneDrive Work:**

```
Chunk 0: Root
  ├── Design Organisation/     (→ will be its own chunk)
  ├── Customer Projects/       (→ will be its own chunk)
  ├── Flight Organisation/     (→ will be its own chunk)
  ├── Production Organisation/ (→ will be its own chunk)
  └── General/                 (→ will be its own chunk)

Chunk 1: Design Organisation/
  ├── Sensors/                 (→ deeper chunk if >30 items)
  │   ├── TDLAS/
  │   ├── SIYI/
  │   └── ISR_Camera/
  ├── Reports/
  ├── PCB/
  └── ...

Chunk 2: Sensors/TDLAS/
  ├── TDLAS_Integration_Report_v3.docx
  ├── TDLAS_Calibration_Data.xlsx
  └── TDLAS_Datasheet.pdf
```

**Chunking Rules:**

```
MAX_ITEMS_PER_CHUNK = 100   # If a folder has more than this, split into sub-chunks
MIN_DEPTH_FOR_DETAIL = 2    # Folders at depth < 2 get summary indexes, not detail
DETAIL_THRESHOLD = 30       # Folders with < 30 items get a leaf-level detail file
```

### Phase C: Claude Classifies Bottom-Up

We process chunks **bottom-up** (deepest first, then parents). This way, when we write a parent index, the child summaries already exist.

```
Step 1: Process all leaf chunks (deepest folders)
        → Claude writes detail index files

Step 2: Process mid-level chunks
        → Claude writes summary indexes, referencing child detail files

Step 3: Process top-level chunks
        → Claude writes high-level summaries

Step 4: Process root
        → Claude writes root_index.md
```

**What Claude Receives Per Chunk:**

```
System prompt:
  "You are building a hierarchical file index. Write a concise markdown
   summary of this folder's contents. For each subfolder, write 1-2 lines
   describing what it contains. For files, include name, type, and a brief
   description inferred from the filename and metadata. Use the format
   specified below."

User message:
  Folder: OneDrive Work / Design Organisation / Sensors

  Contents:
  - TDLAS/ (folder, 3 files, last modified 2026-03-20)
    - TDLAS_Integration_Report_v3.docx (245KB, modified 2026-03-20 by Ginés)
    - TDLAS_Calibration_Data.xlsx (89KB, modified 2026-03-18 by Ginés)
    - TDLAS_Datasheet.pdf (1.2MB, modified 2026-01-10 by Ginés)
  - SIYI/ (folder, 8 files, last modified 2026-03-22)
    - ...
  - ISR_Camera/ (folder, 2 files, last modified 2026-03-23)
    - ISR_Camera_Setup_Guide.docx (180KB, modified 2026-03-23 by Ginés)
    - ISR_Camera_Specifications.pdf (3.1MB, modified 2026-03-23 by Ginés)
```

**What Claude Outputs:**

```markdown
# Sensors

Path: OneDrive Work / Design Organisation / Sensors
Last updated: 2026-03-23
Items: 3 folders, 0 loose files

## TDLAS/ (Active)
Methane detection TDLAS laser sensor. Integration reports covering hardware
and software setup, field calibration data, and manufacturer datasheet.
Main deliverable: integration report (v3, 24 pages).
→ Detail: .nexus/work/design_org/sensors/tdlas.md

## SIYI/ (Active)
SIYI A2 tilting camera for drone payload. Gimbal integration notes,
firmware update logs, and configuration files.
→ Detail: .nexus/work/design_org/sensors/siyi.md

## ISR Camera/ (New — 2026-03-23)
ISR thermal imaging camera. Recently added. Setup guide and hardware
specifications. Not yet integrated into any report.
→ Detail: .nexus/work/design_org/sensors/isr_camera.md
```

**Cost of Full Initial Build:**

```
Assuming: 50,000 files, ~500 folders → ~200 chunks
Each chunk: ~400 input tokens, ~200 output tokens
Total: ~80K input + 40K output tokens
Time: ~30-60 minutes (supervised Claude Code session)
```

### Phase D: User Review

After the automated build, the top 2 levels are presented to the user for validation:

```
NEXUS: Initial index built. Here's the top-level structure I created:

PERSONAL ONEDRIVE:
  Documents/ — personal documents, templates, TFM notes
  Desktop/ — desktop files and shortcuts
  Pictures/ — photos and screenshots

WORK ONEDRIVE (Xer Technologies):
  Design Organisation/ — hardware docs, sensors, PCB, reports, specs
  Customer Projects/ — per-client project folders and deliverables
  Flight Organisation/ — mission planning, flight logs, checklists
  Production Organisation/ — manufacturing, assembly, QC docs
  General/ — company-wide shared resources, meetings, templates

Does this look right? Any corrections?
```

The user can correct misclassifications ("No, Production Org also has test fixtures, not just manufacturing"). Claude updates the relevant index files.

### Phase E: Baseline

- Save the delta sync token from the crawl
- The system is now live — future updates are incremental via delta

---

## The Tree Structure In Detail

### Depth Rules

```
Depth 0: root_index.md
         One file. Lists OneDrive accounts and major areas.
         ~30-50 lines. Always fits in context.

Depth 1: {area}/index.md  (e.g., work/index.md, personal/index.md)
         One file per OneDrive account or major area.
         Lists departments/categories with 1-2 line summaries.
         ~50-100 lines each.

Depth 2: {area}/{department}/index.md  (e.g., work/design_org/index.md)
         One file per department or major folder.
         Lists projects/subfolders with descriptions.
         ~50-150 lines each.

Depth 3: {area}/{dept}/{project}/index.md  (e.g., work/design_org/sensors/index.md)
         One file per project area.
         Lists sub-projects or grouped files.
         May point to detail files for complex sub-areas.
         ~30-100 lines each.

Depth 4+: {leaf}.md  (e.g., work/design_org/sensors/tdlas.md)
          Detail file for a specific project/folder.
          Lists every file with description.
          ~20-50 lines each.
```

### Branching Rules

Not every folder gets its own index file. That would create thousands of tiny files.

```
RULE 1: Folders with ≤ 5 items → inline in parent index (no separate file)
RULE 2: Folders with 6-30 items → get a detail .md file (leaf level)
RULE 3: Folders with > 30 items or containing subfolders → get an index.md
        that summarizes children + points to child indexes
RULE 4: Maximum depth of 5 levels. Beyond that, flatten into the level 5 file.
RULE 5: Single-file folders → just a line in the parent index
```

**Example: A small folder gets inlined**

```markdown
# Customer Projects

## ClientA/ (Active, 12 files)
Drone survey project for ClientA. Contains flight plans,
deliverable reports, and raw survey data.
→ Detail: .nexus/work/customer_projects/clienta.md

## ClientB/ (Complete, 3 files)
Completed inspection job. Final report, invoice, and flight log.
Files: ClientB_Final_Report.pdf, ClientB_Invoice.xlsx, ClientB_FlightLog.csv
(No separate detail file — only 3 files, listed here)
```

### How Claude Decides Which Level to Read

Claude follows a simple algorithm encoded in its system prompt:

```
1. ALWAYS start by reading root_index.md
2. Based on the user's query, identify the most likely area
3. Read that area's index.md
4. If the answer is there (file names visible), respond
5. If the answer requires going deeper, follow the "→ Detail:" pointer
6. If uncertain between two areas, read both (still only ~200-300 lines)
7. NEVER read more than 5 index files in one query
8. If still not found after 5 files, tell the user:
   "I couldn't locate this. Can you give me more context about
    which area it might be in?"
```

### Example Navigation Traces

**Simple query: "Where is the TDLAS report?"**
```
Read root_index.md (50 lines)
  → "Design Organisation — hardware docs, sensors, reports"
Read work/design_org/index.md (100 lines)
  → "Sensors/ — TDLAS, SIYI, ISR Camera"
Read work/design_org/sensors/tdlas.md (30 lines)
  → "TDLAS_Integration_Report_v3.docx — main report"
DONE. 3 files, ~180 lines total.
```

**Ambiguous query: "Find the budget spreadsheet"**
```
Read root_index.md (50 lines)
  → Could be in General, Customer Projects, or Design Org
Read work/general/index.md (80 lines)
  → Sees "Finance/ — budgets, invoices, expense reports"
Read work/general/finance.md (40 lines)
  → "Budget_2026_Q1.xlsx, Budget_2025_Annual.xlsx"
DONE. 3 files, ~170 lines total.
```

**Hard query: "That document about camera mounting angles"**
```
Read root_index.md (50 lines)
  → Could be Design Org (hardware) or Flight Org (mission config)
Read work/design_org/index.md (100 lines)
  → "Sensors/ — camera-related docs"
Read work/design_org/sensors/index.md (50 lines)
  → SIYI mentions "gimbal configuration", ISR mentions "setup guide"
Read work/design_org/sensors/siyi.md (30 lines)
  → "SIYI_Gimbal_Angles_Config.docx — mounting angle presets for different payloads"
DONE. 4 files, ~230 lines total.
```

---

## Delta Update Process (Post-Build)

### What Happens When Files Change

The delta API returns a list of changes. Each change is classified and routed:

```
Delta response: 5 changes detected

Change 1: NEW FILE  ISR_Camera/ISR_Test_Results.docx
  → Affects: work/design_org/sensors/isr_camera.md (add file entry)
  → Affects: work/design_org/sensors/index.md (update ISR Camera summary)

Change 2: MODIFIED  TDLAS/TDLAS_Integration_Report_v3.docx
  → Affects: work/design_org/sensors/tdlas.md (update modified date)
  → May affect description if significant change (check file properties)

Change 3: NEW FOLDER  Design Org/LiDAR/
  → Affects: work/design_org/index.md (add new LiDAR entry)
  → Creates: work/design_org/lidar.md (new detail file)
  → Affects: work/design_org/sensors/index.md? (or is it a separate category?)
  → Claude decides: "LiDAR is a sensor → add to Sensors/ index"

Change 4: DELETED  Old_Reports/draft_v1.docx
  → Affects: wherever it was indexed → remove entry

Change 5: MOVED  Budget.xlsx from General/ to Finance/
  → Affects: old location index (remove) + new location index (add)
```

### The Update Call

For each affected index file, Claude processes the change:

```
System: "Update this index file to reflect the following filesystem changes.
         Maintain the existing format and style. Write concise descriptions
         for new items based on filename and metadata."

User:
  Current index file: [content of isr_camera.md]

  Changes:
  - NEW: ISR_Test_Results.docx (340KB, created 2026-03-24 by Ginés, Word document)

  Output the complete updated index file.
```

### Handling Ambiguous Placements (Coworker Chaos)

When Claude can't confidently classify something:

```markdown
## ⚠ Unclassified Items
- Random_Notes.txt — found in Design Org root. Unable to determine project
  association. Added 2026-03-24 by John.
  Suggested: Move to General/Misc/ or assign to a project.
```

These show up as notifications in the Nexus UI:
```
"1 new file couldn't be auto-classified. Review?"
```

The user resolves it in chat: "That's John's flight checklist, move it to Flight Org." Claude updates the index and optionally moves the file.

---

## File Structure on Disk

```
OneDrive (either account)
└── .nexus/
    ├── config.json                    # API keys, template paths, style rules
    ├── sync_state.json                # Delta tokens per account
    ├── root_index.md                  # Level 0
    ├── personal/
    │   ├── index.md                   # Personal OneDrive overview
    │   ├── documents.md               # If Documents/ is large enough
    │   └── ...
    ├── work/
    │   ├── index.md                   # Work OneDrive overview
    │   ├── design_org/
    │   │   ├── index.md               # Design Org departments/areas
    │   │   ├── sensors/
    │   │   │   ├── index.md           # Sensor sub-projects
    │   │   │   ├── tdlas.md           # TDLAS detail
    │   │   │   ├── siyi.md            # SIYI detail
    │   │   │   └── isr_camera.md      # ISR Camera detail
    │   │   ├── reports/
    │   │   │   └── index.md           # Reports listing
    │   │   └── pcb/
    │   │       └── index.md
    │   ├── customer_projects/
    │   │   ├── index.md
    │   │   ├── clienta.md
    │   │   └── clientb.md
    │   ├── flight_org/
    │   │   └── index.md
    │   ├── production_org/
    │   │   └── index.md
    │   └── general/
    │       ├── index.md
    │       └── finance.md
    └── git_repos/
        └── index.md                   # Summary of local git repositories
```

Total size estimate: **2-10MB** for the entire index of a 1TB filesystem.
Syncs across machines via OneDrive like any other file.
