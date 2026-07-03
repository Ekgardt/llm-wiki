---
type: pattern
title: "B-Sim: Emulate Hook Lifecycle Without a Live Claude Code Window"
description: "The full session-start → edit → session-end → reopen lifecycle for project-state hooks can be exercised entirely via direct script invocation with `CLAUDE_PROJECT_DIR=<path>`, covering all automated b"
timestamp: 2026-07-03T05:41:37
---
# B-Sim: Emulate Hook Lifecycle Without a Live Claude Code Window

One-sentence summary: The full session-start → edit → session-end → reopen lifecycle for project-state hooks can be exercised entirely via direct script invocation with `CLAUDE_PROJECT_DIR=<path>`, covering all automated behaviors without opening a new Claude Code window.

## Lesson

Hook integration tests normally require a real Claude Code UI session, which is slow and hard to script. For `session_start_project_state.py` and `session_end_project_tag.py`, the entire automated lifecycle (slug detection, state.md creation, context injection, daily-log append, vault-skip logic, cross-project isolation) can be emulated in a terminal:

```bash
# 1. Session start for a project
CLAUDE_PROJECT_DIR=<your-projects-dir>/my-app python scripts/session_start_project_state.py

# 2. Inspect the injected output (stdout is JSON with additionalContext)
CLAUDE_PROJECT_DIR=<your-projects-dir>/my-app python scripts/session_start_project_state.py \
  | python -c "import json,sys; print(json.load(sys.stdin)['hookSpecificOutput']['additionalContext'])"

# 3. Simulate a session end
echo '{"session_id":"test-123","transcript_path":""}' \
  | CLAUDE_PROJECT_DIR=<your-projects-dir>/my-app python scripts/session_end_project_tag.py

# 4. Switch projects and verify isolation
CLAUDE_PROJECT_DIR=<your-projects-dir>/other-app python scripts/session_start_project_state.py
```

**What B-sim covers:**
- State.md auto-creation from template
- Slug computation (including collision scenarios via two projects with the same parent-folder name)
- Vault-skip (cwd = `$LLM_WIKI_ROOT` → SessionEnd must not double-append)
- `$HOME` skip (home dir must not be treated as a project)
- Cross-project isolation in `memory/daily/`

**What B-sim cannot cover (requires live Claude Code UI):**
- `/compact` re-injection (B6) — only fires on a real compact operation
- Real-project slug readability (B7) — UX judgment on a real user project
- Actual `additionalContext` injection behavior — only verifiable from within a Claude session

## Slug edge-case smoke test
```bash
python -c "
import sys; sys.path.insert(0,'scripts')
from session_start_project_state import _compute_slug
from pathlib import Path
for n in ['.','..','','normal']:
    class P: name=n
    print(f'{n!r:10} → {_compute_slug(P())!r}')
"
```

## Evidence
- `memory/daily/2026-04-19.md` [17:24:33] — B-sim technique confirmed; 51 automated scenarios run + 5 manual end-to-end

## Related
- [[memory/knowledge/debugging/hook-errors-silent-without-state-root]] — debugging entry for a hook failure B-sim surfaced
- [[memory/knowledge/debugging/case-sensitive-grep-injected-context]] — gotcha specific to B-sim verification scripts
- [[memory/knowledge/decisions/hook-scripts-defense-in-depth]] — decisions made partly as a result of B-sim findings
