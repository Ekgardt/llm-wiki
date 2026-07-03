# User Guide — LLM-Wiki Memory System

How to actually work with this system in **your** tools. **Zero manual steps after setup** — the system maintains itself.

---

## The mental model in one paragraph

This system **watches what you do** in your coding agent (OpenCode or Codex), **decides what's worth remembering** using an LLM, and **saves it as markdown pages** in `$LLM_WIKI_ROOT`. Next time you open a project, those pages are loaded back into the agent's context so it picks up where you stopped — no re-explaining, no lost decisions, no repeated mistakes. **All of this happens automatically** — capture on every action, classification on session end, compile in background, nightly deep maintenance via Windows Task Scheduler.

**The LLM part**: the system needs a "brain" to read transcripts and decide what to keep. That brain is **whichever tool you're already using** — OpenCode uses its own SDK, Codex uses `codex exec`. **No API keys, no Ollama, no extra subscriptions** beyond what you already pay for.

---

## One-time setup (3 commands, then forget)

### 1. OpenCode plugin — already installed and verified working

Heartbeat recorded at session start. Done.

### 2. Codex wrapper — add to PowerShell profile

```powershell
notepad $PROFILE
```
Add at the end:
```powershell
. "$LLM_WIKI_ROOT/scripts\codex-memory-wrapper.ps1"
```
If `$PROFILE` doesn't exist: `New-Item -Path $PROFILE -ItemType File -Force`. Restart terminal. Verify with `codex-memory-status`.

### 3. Windows Task Scheduler — automatic nightly + weekly maintenance

Already installed. Verify:
```powershell
$LLM_WIKI_ROOT/scripts\install-scheduled-tasks.ps1 -Status
```
Should show LLMWiki-Nightly (daily 03:00) and LLMWiki-Weekly (Sunday 04:00) both Ready.

**That's it. You never touch the system again.**

---

## What happens automatically — and when

```
REAL-TIME (while you work)
  Every Edit/Write/Bash → breadcrumb appended to today's daily log
  SessionStart → load project handoff + drain queue + background compile

END OF SESSION (agent idle or you close)
  OpenCode/Codex LLM classifies transcript → FLUSH_MAJOR/MINOR/OK
  MAJOR/MINOR content → structured summary appended to daily log
  MAJOR triggers background compile (detached, doesn't block you)

NIGHTLY 03:00 (Task Scheduler, even while you sleep)
  Drain deferred queue → compile all pending → structural lint → prune old reports

SUNDAY 04:00 (Task Scheduler)
  Everything nightly does + OKF conformance sweep + prune failed queue tasks
```

**You don't run any of these.** They fire on triggers.

---

## How agents "remember" across sessions

### Life of one decision (example: you decide "use JWT for auth")

1. **Monday 10:00** — you work in OpenCode. Plugin records every Edit/Write as breadcrumbs in `memory/daily/2026-07-06.md`.
2. **Monday 12:00** — session.idle fires. OpenCode LLM classifies: **FLUSH_MAJOR**. Structured summary appended to daily log.
3. **Monday 12:01** — plugin triggers detached compile. Background process:
   - Reads daily log
   - Asks LLM to extract lessons → returns JSON
   - Python verifies cited evidence exists in source (no hallucinations)
   - Writes `memory/knowledge/decisions/auth-jwt-migration.md` with full frontmatter
4. **Monday 12:05** — knowledge page exists. **Permanent memory.**

### Next session finds it automatically

Tuesday 09:00 — open <your-project> again. Plugin's `session.created`:
1. Loads `wiki/projects/<your-project>/state.md` (handoff: "JWT auth done, refresh tokens next")
2. Loads `memory/index.md` (catalog including new `auth-jwt-migration` page)
3. Loads today's heartbeat (proves session is fresh)
4. Injects all this into the agent's context

Agent reads: "Yesterday decided JWT. Stopped at: refresh tokens." Picks up without re-explaining.

### Long-term recall (3 months later)

You ask: **"why did we choose JWT over OAuth?"**

1. Agent doesn't have it in current context (memory window reset).
2. But agent sees `memory/index.md` in SessionStart context.
3. Uses Read/Grep to find `memory/knowledge/decisions/auth-jwt-migration.md` from July.
4. Reads it, sees Decision + Evidence pointing back to the July daily log.
5. Answers with original reasoning.

**The page is permanent memory.** Doesn't expire, doesn't compact out, doesn't depend on any session's window.

---

## What if something breaks?

System **fails silently and self-heals**:

| Failure | What happens |
|---|---|
| OpenCode LLM unavailable | Plugin skips classification, records heartbeat. Next session retries. |
| Codex CLI not on PATH | `codex exec` returns empty. Queue picks up at next OpenCode session. |
| Compile crashes | Lock remains; next `maybe_compile` detects stale lock after 30min, clears + retries. |
| Task Scheduler disabled | Nightly/weekly don't run, but session-end triggers still fire. No data loss. |
| Internet down | All LLM calls enqueue. Drains when internet returns. |
| Disk full | File writes fail silently. Hooks exit 0 (never break user's session). |

Only way to lose data: delete `$LLM_WIKI_ROOT` AND `$LLM_WIKI_STATE_ROOT` simultaneously.

---

## Inspecting the system (optional)

```powershell
codex-memory-status                                                    # current state
Get-Content $LLM_WIKI_STATE_ROOT/memory-reports\nightly-*.md | Select -Last 30   # last night's run
uv run python $LLM_WIKI_ROOT/scripts\memory_queue.py status               # deferred tasks
uv run python $LLM_WIKI_ROOT/scripts\maybe_compile.py --status            # compile lock state
Get-Content $LLM_WIKI_ROOT/memory\daily\$(Get-Date -Format 'yyyy-MM-dd').md   # today's raw capture
```

**None of these are required.** They're for curiosity.

---

## Common questions

**Do I need to do anything ever?** No. System is fully autonomous after setup.

**Do I need Ollama?** No. All LLM work through existing subscriptions.

**Disable auto-compile?** Comment out `triggerCompileIfIdle()` in plugin, or `install-scheduled-tasks.ps1 -Uninstall`.

**Enable weekly contradiction check?** `setx MEMORY_WEEKLY_CONTRADICTIONS "1"`

---

## Daily mental model

```
YOU
  Open OpenCode in any project, work normally, close when done. That's it.

SYSTEM (autonomous, invisible)
  Capture every action → breadcrumbs
  Classify each session → daily log
  Compile new content → knowledge pages (detached, background)
  Nightly 03:00 → deep consolidation (Task Scheduler)
  Sunday 04:00 → OKF + lint + cleanup (Task Scheduler)
  Next session start → load accumulated memory automatically

Zero commands. Zero spreadsheets. Zero rituals.
Just work — the system quietly remembers.
```
