# Jarvis Test Results — 2026-04-08 22:05


## State Machine

- **PASS**: connect nexus → CODING — `state=coding, project=nexus`
- **PASS**: connect nexus → has branch info — `result=Connected to 'nexus'. Branch: master.
Recent commits:
b7e0a3e Add wake word detection, GitHub tool, `
- **PASS**: connect unknown → error, stays IDLE — `result=Unknown project 'nonexistent'. Available: nexus, cloud_work_for_calls, jim_app, wine-pwa`
- **PASS**: connect while connected → error — `result=Already connected to 'nexus'. Disconnect first.`
- **PASS**: disconnect → IDLE — `state=idle, project=None`
- **PASS**: disconnect while IDLE → error — `result=Not connected to a project.`

## Claude Session

- **PASS**: fresh session is idle — `status=idle`
- **PASS**: kill() on idle → no crash — `no exception`
- **PASS**: get_progress() idle → 'No task running' — `progress=No task running.`
- **PASS**: zombie detection → error status — `progress=Error: process died (exit 1)`
- **PASS**: kill() → idle, proc=None, monitor cancelled — `status=idle, monitor_cancelled=True`

## Coding Task

- **PASS**: coding_task while IDLE → error — `result=Not connected to a project. Say 'connect to <project>' first.`
- **PASS**: coding_task while CODING → started — `result=Started. Working on: List all Python files in the project. Be concise.
Current: starting...
Elapsed: 1s, 0 operations`
- **PASS**: Claude process is running — `status=working, proc=True`

## Management

- **PASS**: management(calendar) → calendar data — `result_len=262, snippet=User asked: 'any meetings today?'
Answer concisely based on this data.

# Calendar

Last synced: 202`
- **PASS**: management(email) → email data — `result_len=16003, snippet=User asked: 'any new emails?'
Answer concisely based on this data.

# Email (gines.rodriguez.castro@`
- **PASS**: management(reminders) → reminders data — `result_len=187, snippet=User asked: 'any tasks?'
Answer concisely based on this data.

# Reminders

Last synced: 2026-04-08T`
- **PASS**: management(all) → briefing prefix + root.md — `has_prefix=True, result_len=6300`

## Search Documents

- **PASS**: search 'drone' → finds results — `result=[root.md] Personal and academic engineering projects: thermal imaging / rPPG research, drone autopilot hardware, algorithmic trading with MATLAB, Unit`
- **PASS**: search nonsense → 'Nothing found' — `result=Nothing found for 'xyzzyflurble99'.`
- **PASS**: search_worktree('education') → has content — `len=515, snippet=[root.md] ## Education (1,978 files)
[root.md] > Detail: documents/education/index.md
[index.md] # E`

## GitHub

- **PASS**: github('recent activity') → repo list — `result=User asked: 'recent activity'
Answer concisely.

Recent repos:
wine_pwa — 2026-04-04T23:23:16Z — no desc
nexus — 2026-04-03T13:45:12Z — no desc
Hedge-`
- **PASS**: github('commits on nexus') → includes nexus commits — `result=User asked: 'last commits on nexus'
Answer concisely.

Recent repos:
wine_pwa — 2026-04-04T23:23:16Z — no desc
nexus — 2026-04-03T13:45:12Z — no desc
`

## Check Progress

- **PASS**: check_progress while IDLE → available projects — `result=Not connected to a project. Available: nexus, cloud_work_for_calls, jim_app, wine-pwa`
- **PASS**: check_progress while CODING → project + branch — `result=Project: nexus (/Users/gines/nexus)
Branch: master
No task running.`

## Sleep

- **PASS**: sleep → goodbye message — `result=Going to sleep. Say 'hey jarvis' when you need me.`
- **PASS**: sleep → sleep_requested=True — `sleep_requested=True`

---

**Total: 27 passed, 0 failed**
