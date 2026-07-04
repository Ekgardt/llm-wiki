# Agent Configuration Guide

**Where to install and store plugins, frameworks, skills, scripts, methodologies, and technical decisions for high-quality agent-assisted development.**

This document is the single source of truth for any AI agent (OpenCode, Codex, Claude Code, Cursor, Antigravity) working on any project that uses the LLM-Wiki memory vault.

---

## 1. The Four-Zone Principle

Everything an agent touches lives in one of four zones. Each zone has different ownership, lifecycle, and git tracking rules.

```
┌─────────────────────────────────────────────────────────────────┐
│  ZONE 1: VAULT (D:\LLM-wiki or ~/LLM-wiki)                     │
│  Git-tracked. PUBLIC repo. Portable. The "brain".               │
│                                                                  │
│  → Memory system scripts (compile, search, lint, hooks impl)    │
│  → PUBLIC skills (knowledge-compile, knowledge-lookup)          │
│  → Cross-project methodologies (wiki/concepts/)                 │
│  → Rules (file-handling policies)                               │
│  → Project state pages                                           │
│                                                                  │
│  Rule: anything here is shared with the world. No paths, no     │
│  secrets, no personal preferences. Use $LLM_WIKI_ROOT.          │
├─────────────────────────────────────────────────────────────────┤
│  ZONE 2: DOTFILES (D:\dev\dotfiles or ~/dotfiles)              │
│  Git-tracked. PRIVATE repo. The "identity".                     │
│                                                                  │
│  → User-level configs (~/.claude/settings.json, CLAUDE.md)      │
│  → Hook shims (~/.claude/hooks/*.sh)                            │
│  → Shell profiles (PowerShell, bash, zsh)                       │
│  → Tool configs (opencode.json, .gitconfig)                     │
│  → ACTIVE plugin installations (opencode plugins)               │
│  → PERSONAL skills (your TDD/review/deploy workflows)           │
│  → Personal cross-project scripts                               │
│  → Encrypted secrets (age-encrypted .env.age)                   │
│                                                                  │
│  Rule: this is YOU — your machine, your taste, your paths.      │
│  PRIVATE repo only. Installed via install.ps1 / install.sh.     │
├─────────────────────────────────────────────────────────────────┤
│  ZONE 3: USER RUNTIME (~/.config/, ~/.claude/, ~/Documents/)   │
│  NOT in any git repo. Machine-generated. The "hands".           │
│                                                                  │
│  → Live copies installed FROM Zone 2 by the installer           │
│  → Tool caches, sessions, history (runtime, ephemeral)          │
│  → age private key (~/.config/age/keys.txt — NEVER committed)   │
│                                                                  │
│  Rule: regenerable from Zone 2 + Zone 1. Lose the disk → run    │
│  `install.ps1` and you're back.                                 │
├─────────────────────────────────────────────────────────────────┤
│  ZONE 4: PROJECT (D:\dev\<project> or ~/projects/<project>)    │
│  Git-tracked in the PROJECT repo. The "workspace".              │
│                                                                  │
│  → Project-specific code                                         │
│  → Project-specific scripts                                      │
│  → Project-specific AGENTS.md / .cursorrules                     │
│  → Project-specific tests, configs, docs                         │
└─────────────────────────────────────────────────────────────────┘
```

**Golden rule** (decide in this order):
1. Reusable by *anyone* and contains no personal data → **Zone 1 (public vault)**
2. Reusable by *you* across projects/machines → **Zone 2 (private dotfiles)**
3. Regenerable runtime state → **Zone 3 (live home dir)**
4. Specific to *one* project → **Zone 4 (project repo)**

**Why Zone 2 exists separately from Zone 3**: Zone 2 is the *source of truth* (versioned, backed up to a private GitHub repo). Zone 3 is the *live installation* that tools read at runtime. The installer copies Zone 2 → Zone 3. If your disk dies, you `git clone dotfiles && ./install.ps1` and every tool is reconfigured exactly as before.

**Public vs private split for skills**:
- `LLM-wiki/.claude/skills/` — public skills that ship with the memory system (knowledge-compile, knowledge-lookup, bridge-promote-insight, contradict-check). Anyone who clones the vault gets these.
- `dotfiles/claude/skills/` — your personal skills (your TDD flavor, your deploy rituals, your review checklist). Only you.

---

## 2. Where Each Type of Thing Lives

### 2.1 Plugins (OpenCode, Claude Code, Codex)

| What | Zone | Path | Why |
|---|---|---|---|
| **Plugin source code** (template, portable) | Zone 1 | `LLM-wiki/scripts/llm-wiki-memory-opencode.js` | Versioned, public, clonable — uses `$LLM_WIKI_ROOT` |
| **Active plugin installation** (your copy) | Zone 2 → 3 | source: `dotfiles/opencode/plugins/` → live: `~/.config/opencode/plugins/` | Your machine's version, may have local paths |
| **Plugin deps** | Zone 3 | `~/.config/opencode/package.json` | Regenerated, gitignored |

**Rule**: The *template* lives in Zone 1 (public). Your *active copy* lives in Zone 2 (dotfiles, private) and gets installed to Zone 3 (`~/.config/opencode/plugins/`) by `install.ps1`. Never install a plugin into a project directory (Zone 4).

### 2.2 Hooks (Claude Code lifecycle hooks)

| What | Zone | Path | Why |
|---|---|---|---|
| **Hook implementation** (the real logic) | Zone 1 | `LLM-wiki/scripts/session_start_project_state.py` etc. | Versioned, public, testable |
| **Hook shims** (thin wrappers that call impl) | Zone 2 → 3 | `dotfiles/claude/hooks/*.sh` → `~/.claude/hooks/` | Machine-specific registration |
| **Hook registration** (settings.json) | Zone 2 → 3 | `dotfiles/claude/settings.json` → `~/.claude/settings.json` | Your env vars + which hooks fire |
| **Vault-level hooks** | Zone 1 | `LLM-wiki/.claude/settings.json` | Fire only when working IN the vault |
| **Project-level hooks** | Zone 4 | `<project>/.claude/settings.json` | Fire only for this project (rare) |

**Rule**: Memory hooks (SessionStart, SessionEnd, PostToolUse, UserPromptSubmit) registration belongs in **Zone 2** (dotfiles) because it carries your env vars (`LLM_WIKI_ROOT=D:\...`) and must survive disk loss. The implementation they call lives in **Zone 1** (public, versioned with tests).

### 2.3 Skills (procedural workflows)

| What | Zone | Path | When to use |
|---|---|---|---|
| **Memory system skills** (built-in) | Zone 1 | `LLM-wiki/.claude/skills/` | knowledge-compile, knowledge-lookup, bridge-promote-insight, contradict-check, crystallize-playbook |
| **Personal cross-project skills** | Zone 2 → 3 | `dotfiles/claude/skills/` → `~/.claude/skills/` | Your TDD workflow, code-review checklist, deploy ritual, security audit |
| **Project-specific skills** | Zone 4 | `<project>/.claude/skills/` | Only make sense in one project |

**Public vs private skill test**: "Would a stranger who installed LLM-wiki benefit from this skill?" → YES = Zone 1. "Is this *my* personal taste/ritual?" → Zone 2.

**Skill categories** (split across zones):

```
Zone 1 — LLM-wiki/.claude/skills/        (PUBLIC, ship with the system)
├── knowledge-compile/         ← how to run compile
├── knowledge-lookup/          ← three-tier retrieval
├── bridge-promote-insight/   ← promote project lesson → concept
├── crystallize-playbook/     ← concept → executable skill
└── contradict-check/         ← real-time contradiction scan

Zone 2 — dotfiles/claude/skills/        (PRIVATE, your personal workflows)
├── requirements-analysis/    ← YOUR requirements workflow
├── system-design/            ← YOUR architecture method
├── ux-review/                ← YOUR UX audit checklist
├── code-review/              ← YOUR review methodology
├── qa-strategy/              ← YOUR test planning
├── deploy-checklist/         ← YOUR deployment steps
└── security-audit/           ← YOUR security review
```

**Rule**: Skills are procedures ("how to do X, step by step"). They are NOT knowledge pages. Skills execute; knowledge pages inform.

### 2.4 Frameworks and Methodologies

| What | Zone | Path | Format |
|---|---|---|---|
| **Cross-project methodology** | Zone 1 | `wiki/concepts/` | Markdown page, third-person, citeable |
| **Tool-specific knowledge** | Zone 1 | `wiki/entities/` | "How OpenCode plugin system works" |
| **Technical decision** | Zone 1 | `memory/knowledge/decisions/` | "We chose X over Y because Z" |
| **Reusable pattern** | Zone 1 | `memory/knowledge/patterns/` | "When X, do Y because Z" |
| **Project-specific architecture** | Zone 3 | project docs | In the project repo |

**Examples**:

```
wiki/concepts/
├── context-engineering.md      "Anthropic's 7 principles of context engineering"
├── testing-trophy.md           "Integration over unit; behavior over implementation"
├── security-first.md           "Input validation on every entry point"
├── tdd-workflow.md             "Red-Green-Refactor adapted for AI agents"
└── okf-knowledge-format.md     "Open Knowledge Format v0.1 spec"

wiki/entities/
├── opencode.md                 "Plugin system: events, SDK, $ shell API"
├── codex-cli.md                "exec mode, hooks, MCP, --output-last-message"
├── claude-code.md              "settings.json hooks, SessionStart/End, PreCompact"
├── cursor.md                   "Rules files (.mdc), MCP servers, @ mentions"
└── obsidian.md                 "Graph view, backlinks, web clipper"
```

### 2.5 Scripts

| Type | Zone | Path | Examples |
|---|---|---|---|
| **Memory system scripts** | Zone 1 | `LLM-wiki/scripts/` | compile_memory.py, search_memory.py, lint_memory.py |
| **Personal cross-project scripts** | Zone 2 → 3 | `dotfiles/tools-scripts/` → `D:\dev\_tools\scripts\` | deploy.ps1, backup-vault.sh, db-seed.py |
| **Project-specific scripts** | Zone 4 | `<project>/scripts/` | seed-data.py, migrate-users.sh |

**Rule**: Never put personal scripts in `LLM-wiki/scripts/` (they'll end up in the public repo). Personal scripts live in `dotfiles/tools-scripts/` (versioned, private, backed up) and are installed to `D:\dev\_tools\scripts\` (Zone 3, on PATH). If a script needs to call the memory system, use the env var:

```bash
uv run python $LLM_WIKI_ROOT/scripts/compile_memory.py --all
```

### 2.6 Rules (file-handling policies)

| What | Zone | Path |
|---|---|---|
| **Vault rules** | Zone 1 | `LLM-wiki/.claude/rules/` |
| **Project rules** | Zone 3 | `<project>/.claude/rules/` or `<project>/AGENTS.md` |

**Built-in vault rules**:
- `raw-files.md` — raw/ is immutable, never modify
- `wiki-files.md` — wiki/ is written via scripts, not directly
- `output-files.md` — outputs/ are derived, safe to regenerate

### 2.7 Environment Variables

| Variable | Scope | Purpose |
|---|---|---|
| `LLM_WIKI_ROOT` | User | Path to the vault (required) |
| `LLM_WIKI_STATE_ROOT` | User | Path to runtime state (optional, default: sibling dir) |
| `MEMORY_LLM_PROVIDER` | User | Force LLM backend: codex / opencode / claude / openai / ollama |
| `MEMORY_CODEX_REASONING` | User | Codex reasoning level: low (default) / minimal / high |
| `MEMORY_COMPILE_AFTER_HOUR` | User | Auto-compile cutoff hour (default: 18 = 6PM) |
| `MEMORY_COMPILE_COOLDOWN_SECONDS` | User | Compile spawn cooldown (default: 900 = 15 min) |
| `OLLAMA_MODELS` | User | Ollama model storage path (for offline embeddings) |
| `COGNEE_DATA_DIR` | User | Cognee graph data path (optional) |

### 2.8 Secrets (API keys, tokens, credentials)

| What | Zone | Path | Format |
|---|---|---|---|
| **Encrypted secrets** | Zone 2 | `dotfiles/secrets/*.age` | age-encrypted, safe to push to private repo |
| **Decrypted secrets** | Zone 3 | `~/.config/dotfiles-env/.env` | plaintext, gitignored, NEVER committed |
| **age private key** | Zone 3 | `~/.config/age/keys.txt` | NEVER committed, regenerated per machine |
| **age public key** | Zone 2 | `dotfiles/secrets/README.md` | safe to share — can only encrypt, not decrypt |
| **Secret template** | Zone 2 | `dotfiles/secrets/.env.template` | placeholder keys with empty values |

**Workflow** (using [age](https://age-encryption.org), industry standard for age-encrypted secrets):

```bash
# ONE-TIME: generate your keypair (per machine)
age-keygen -o ~/.config/age/keys.txt
# Copy the "Public key: age1..." line into dotfiles/secrets/README.md

# ADD/EDIT A SECRET:
# 1. Decrypt current secrets to a temp plaintext file
age -d -i ~/.config/age/keys.txt -o secrets/.env.tmp secrets/secrets.env.age
# 2. Edit secrets/.env.tmp (add your API key)
# 3. Re-encrypt
age -e -r "age1YOURPUBLICKEY" -o secrets/secrets.env.age secrets/.env.tmp
# 4. Shred the plaintext
shred -u secrets/.env.tmp   # or on Windows: Remove-Item secrets/.env.tmp

# USE SECRETS: shell profile sources the decrypted file
# In dotfiles/powershell/Microsoft.PowerShell_profile.ps1:
$envFile = "$env:USERPROFILE\.config\dotfiles-env\.env"
if (Test-Path $envFile) { Get-Content $envFile | ForEach-Object { ... } }
```

**Why age (not .env in repo, not vault, not lastpass-cli)?**
- `.env` in git repo → **NO**. Even private repos get leaked (token scope creep, accidental public flip).
- Plaintext in profile → **NO**. One `cat` screenshot leaks everything.
- age-encrypted `.env.age` in private repo → **YES**. Industry standard (used by sops, nixos, chezmoi). Cryptographically sealed even if repo leaks. Public key is safe to share; private key never leaves your machine.
- 1Password CLI / `op` → also acceptable for cloud-managed secrets. age wins for offline/git-native.

**Rule**: The ONLY acceptable locations for a secret are (a) age-encrypted in `dotfiles/secrets/*.age`, or (b) decrypted in `~/.config/dotfiles-env/.env` (gitignored). Nowhere else. Never in vault, never in a project repo, never hardcoded, never in a commit.

### 2.9 Backups (the point of Zone 2)

| What | Backed up where | Restore how |
|---|---|---|
| **Zone 1 (vault)** | GitHub public repo | `git clone git@github.com:Ekgardt/llm-wiki.git` |
| **Zone 2 (dotfiles)** | GitHub private repo | `git clone git@github.com:Ekgardt/dotfiles.git && ./install.ps1` |
| **Zone 4 (projects)** | Each project's GitHub repo | `git clone <project>` |
| **Vault runtime data** (`memory/daily/`, `wiki/projects/<slug>/`) | gitignored (private) | **NOT backed up** — accept loss, or add to a private backup target |
| **age private key** | NOT backed up (by design) | Regenerate per machine; re-encrypt secrets if lost |

**Disaster recovery drill** (run once a year): wipe a test machine, `git clone dotfiles`, `./install.ps1`, `git clone llm-wiki`, verify every tool works. If anything is missing → it wasn't in Zone 2.

---

## 3. Development Lifecycle — Where Things Go

### 3.1 Requirements Analysis

| Artifact | Zone | Path |
|---|---|---|
| Requirement docs | Zone 4 | `<project>/docs/requirements/` |
| Requirement decisions | Zone 1 | `memory/knowledge/decisions/` |
| Requirement methodology | Zone 1 | `wiki/concepts/requirements-analysis.md` |
| Requirement skill | Zone 2 | `dotfiles/claude/skills/requirements-analysis/SKILL.md` |

**Agent instruction** (in CLAUDE.md or AGENTS.md):
> "Before implementing, check if requirements exist in `<project>/docs/requirements/`. If not, create them using the `/requirements-analysis` skill. Record non-trivial requirement decisions in the memory vault."

### 3.2 System Analysis & Architecture

| Artifact | Zone | Path |
|---|---|---|
| Architecture docs | Zone 4 | `<project>/docs/architecture/` |
| Architecture decisions (ADRs) | Zone 1 | `memory/knowledge/decisions/` |
| Architecture patterns | Zone 1 | `memory/knowledge/patterns/` |
| Architecture methodology | Zone 1 | `wiki/concepts/system-design.md` |

**Agent instruction**:
> "Before coding, search the vault for past architecture decisions: `search_memory.py 'architecture decision' --project <slug>`. Record new ADRs as `memory/knowledge/decisions/adr-<topic>.md`."

### 3.3 UX/UI Design

| Artifact | Zone | Path |
|---|---|---|
| Design specs | Zone 4 | `<project>/docs/design/` |
| UX principles | Zone 1 | `wiki/concepts/ux-principles.md` |
| Component patterns | Zone 1 | `memory/knowledge/patterns/` |
| Design review checklist | Zone 2 | `dotfiles/claude/skills/ux-review/SKILL.md` |

### 3.4 Code Implementation

| Artifact | Zone | Path |
|---|---|---|
| Source code | Zone 4 | `<project>/src/` |
| Coding standards | Zone 4 | `<project>/AGENTS.md` or `<project>/.claude/rules/` |
| Reusable patterns | Zone 1 | `memory/knowledge/patterns/` |
| Gotchas/bugs | Zone 1 | `memory/knowledge/debugging/` |
| Guard rails | Zone 1 | auto-generated at SessionStart (not stored) |

**Agent instruction**:
> "Before implementing, search the vault for known gotchas: `search_memory.py 'gotcha <topic>' --project <slug>`. Read guard rails at session start. Record non-trivial bugs as `memory/knowledge/debugging/<symptom>.md`."

### 3.5 Code Review

| Artifact | Zone | Path |
|---|---|---|
| Review checklist | Zone 2 | `dotfiles/claude/skills/code-review/SKILL.md` |
| Review decisions | Zone 1 | `memory/knowledge/decisions/` |
| Review patterns | Zone 1 | `memory/knowledge/patterns/` |

**Agent instruction**:
> "Use `/code-review` skill. Search vault for project-specific review criteria: `search_memory.py 'code review <project>'`. Record findings that will recur."

### 3.6 QA Testing

| Artifact | Zone | Path |
|---|---|---|
| Test plans | Zone 4 | `<project>/docs/test-plans/` |
| Test code | Zone 4 | `<project>/tests/` |
| Testing methodology | Zone 1 | `wiki/concepts/testing-trophy.md` |
| QA strategy skill | Zone 2 | `dotfiles/claude/skills/qa-strategy/SKILL.md` |
| Bug learnings | Zone 1 | `memory/knowledge/debugging/` |

**Agent instruction**:
> "Follow Testing Trophy: favor integration tests over unit tests, test behavior not implementation. Use `/qa-strategy` skill. Record bugs in `memory/knowledge/debugging/` with symptom→cause→fix."

### 3.7 Deployment

| Artifact | Zone | Path |
|---|---|---|
| Deploy scripts | Zone 2 → 3 | `dotfiles/tools-scripts/` (cross-project) or Zone 4 (project-specific) |
| Deploy checklist | Zone 2 | `dotfiles/claude/skills/deploy-checklist/SKILL.md` |
| Deploy decisions | Zone 1 | `memory/knowledge/decisions/` |
| Deploy gotchas | Zone 1 | `memory/knowledge/debugging/` |

---

## 4. Cross-Project Knowledge Hierarchy

Knowledge promotes upward through the vault. The system handles this automatically via `/bridge-promote-insight`:

```
Level 0: Session capture (memory/daily/)
  "I fixed a bug in bank-list auth middleware"
         ↓ compile
Level 1: Project knowledge (memory/knowledge/)
  "Auth middleware must validate JWT before rate limiting"
         ↓ promote (if reusable)
Level 2: Cross-project wiki (wiki/concepts/)
  "Always validate auth tokens before applying rate limits"
         ↓ crystallize (if procedural)
Level 3: Skill (.claude/skills/)
  "/security-audit: Step 1. Check token validation. Step 2. Check rate limiting..."
```

**Promotion criteria** (two questions):
1. Would this be useful to someone who has never seen this project? → If YES → promote to `wiki/`
2. Is this a repeatable procedure? → If YES → crystallize to `.claude/skills/`

---

## 5. Agent-Specific Configuration

All configs below live in **Zone 2 (dotfiles)** as source of truth and are installed to **Zone 3 (live home dir)** by the installer. Paths shown as `live:` are where the tool reads them at runtime.

### OpenCode
```
dotfiles (source)            →    live (installed)
─────────────────────────────────────────────────────
dotfiles/opencode/
├── opencode.json            →    ~/.config/opencode/opencode.json
├── package.json             →    ~/.config/opencode/package.json
└── plugins/
    └── llm-wiki-memory.js   →    ~/.config/opencode/plugins/llm-wiki-memory.js
```

### Codex CLI
```
dotfiles/powershell/
└── Microsoft.PowerShell_profile.ps1
                             →    ~/Documents/PowerShell/Microsoft.PowerShell_profile.ps1
                                  (sources codex-memory-wrapper.ps1 from $LLM_WIKI_ROOT)
```

### Claude Code
```
dotfiles/claude/
├── settings.json            →    ~/.claude/settings.json        (hooks + env)
├── CLAUDE.md                →    ~/.claude/CLAUDE.md            (global user rules)
├── hooks/
│   ├── session_start.sh     →    ~/.claude/hooks/session_start.sh
│   └── session_end.sh       →    ~/.claude/hooks/session_end.sh
└── skills/                  →    ~/.claude/skills/              (personal skills)
```

### Cursor
```
<project>/.cursor/rules/
└── llm-wiki.mdc             (Zone 4 — per-project, in the project repo)
```

### Antigravity
```
<project>/AGENTS.md          (Zone 4 — per-project)
```

### Git
```
dotfiles/git/
└── .gitconfig               →    ~/.gitconfig
```

---

## 6. What NOT to Do

1. **Never** put personal scripts, configs, or skills in `LLM-wiki/` (public repo — they'll leak).
2. **Never** put secrets/tokens in CLAUDE.md, AGENTS.md, state.md, or any repo (even private). Use age-encrypted `secrets/*.age`.
3. **Never** commit the age private key (`~/.config/age/keys.txt`). It is regenerable per machine only.
4. **Never** commit plaintext `.env` files. The dotfiles `.gitignore` blocks them.
5. **Never** edit `wiki/` pages directly — use the scripts (`compile_memory.py`, `bridge-promote-insight`).
6. **Never** put project-specific configs in the vault or dotfiles (use the project repo, Zone 4).
7. **Never** install plugins or personal configs into project directories (Zone 4). Plugins are Zone 2 → 3.
8. **Never** hardcode absolute paths in Zone 1 (public vault) files. Use `$LLM_WIKI_ROOT`. Hardcoded paths are OK in Zone 2 (your machine).
9. **Never** rely on Zone 3 (runtime) as source of truth — it is regenerable from Zones 1 + 2. If it's only in Zone 3, it's lost on disk failure.

---

## 7. Quick Reference Card

```
┌──────────────────────┬───────────────────────────────────────┬────────────────┐
│ Artifact             │ Where                                  │ Git?           │
├──────────────────────┼───────────────────────────────────────┼────────────────┤
│ VAULT (Zone 1) — public, shared, clonable                                                          │
│ Memory scripts       │ LLM-wiki/scripts/                      │ Vault (public) │
│ Public skills        │ LLM-wiki/.claude/skills/               │ Vault (public) │
│ Memory rules         │ LLM-wiki/.claude/rules/                │ Vault (public) │
│ Methodologies        │ LLM-wiki/wiki/concepts/                │ Vault (public) │
│ Tool knowledge       │ LLM-wiki/wiki/entities/                │ Vault (public) │
│ Lessons learned      │ LLM-wiki/memory/knowledge/             │ Vault (public) │
│ Decisions (ADR)      │ LLM-wiki/memory/knowledge/decisions/   │ Vault (public) │
│ Gotchas/bugs         │ LLM-wiki/memory/knowledge/debugging/   │ Vault (public) │
│ Patterns             │ LLM-wiki/memory/knowledge/patterns/    │ Vault (public) │
│ Project state        │ LLM-wiki/wiki/projects/<slug>/         │ Vault (public)*│
│ Guard rails          │ auto-generated, injected at start      │ Not stored     │
├──────────────────────┼───────────────────────────────────────┼────────────────┤
│ DOTFILES (Zone 2) — private, your identity, backed up to private repo                              │
│ Active plugins       │ dotfiles/opencode/plugins/             │ Dotfiles(priv) │
│ Claude settings.json │ dotfiles/claude/settings.json          │ Dotfiles(priv) │
│ Global CLAUDE.md     │ dotfiles/claude/CLAUDE.md              │ Dotfiles(priv) │
│ Hook shims           │ dotfiles/claude/hooks/*.sh             │ Dotfiles(priv) │
│ Personal skills      │ dotfiles/claude/skills/                │ Dotfiles(priv) │
│ Shell profile        │ dotfiles/powershell/profile.ps1        │ Dotfiles(priv) │
│ opencode.json        │ dotfiles/opencode/opencode.json        │ Dotfiles(priv) │
│ .gitconfig           │ dotfiles/git/.gitconfig                │ Dotfiles(priv) │
│ Personal scripts     │ dotfiles/tools-scripts/                │ Dotfiles(priv) │
│ Encrypted secrets    │ dotfiles/secrets/*.age                 │ Dotfiles(priv) │
│ age public key       │ dotfiles/secrets/README.md             │ Dotfiles(priv) │
├──────────────────────┼───────────────────────────────────────┼────────────────┤
│ USER RUNTIME (Zone 3) — live installation, regenerable from Zones 1+2                             │
│ Live plugins         │ ~/.config/opencode/plugins/            │ NOT in git     │
│ Live settings.json   │ ~/.claude/settings.json                │ NOT in git     │
│ Live hooks           │ ~/.claude/hooks/                       │ NOT in git     │
│ Live profile         │ ~/Documents/PowerShell/profile.ps1     │ NOT in git     │
│ Live .gitconfig      │ ~/.gitconfig                           │ NOT in git     │
│ Live scripts         │ D:\dev\_tools\scripts\                 │ NOT in git     │
│ Decrypted secrets    │ ~/.config/dotfiles-env/.env            │ NOT in git     │
│ age private key      │ ~/.config/age/keys.txt                 │ NOT in git     │
│ Sessions/cache       │ ~/.claude/sessions/, cache/, etc.      │ NOT in git     │
├──────────────────────┼───────────────────────────────────────┼────────────────┤
│ PROJECT (Zone 4) — one project, in that project's repo                                             │
│ Project code         │ D:\dev\<project>\src\                  │ Project git    │
│ Project scripts      │ D:\dev\<project>\scripts\              │ Project git    │
│ Project rules        │ D:\dev\<project>\AGENTS.md             │ Project git    │
│ Project tests        │ D:\dev\<project>\tests\                │ Project git    │
│ Cursor rules         │ D:\dev\<project>\.cursor\rules\        │ Project git    │
├──────────────────────┼───────────────────────────────────────┼────────────────┤
│ RUNTIME STATE (not git-tracked anywhere)                                                           │
│ Search index         │ LLM-wiki-state/search/                 │ NOT in git     │
│ Vector cache         │ LLM-wiki-state/search/vectors.npz      │ NOT in git     │
│ Task queue           │ LLM-wiki-state/pipeline/queue/         │ NOT in git     │
│ Hook error log       │ LLM-wiki-state/hook-errors.log         │ NOT in git     │
│ Vault daily logs     │ LLM-wiki/memory/daily/*.md             │ gitignored**   │
└──────────────────────┴───────────────────────────────────────┴────────────────┘

*  wiki/projects/<slug>/state.md is in the public vault but contains only project
   architecture — no secrets. The .gitignore allows _template/ and llm-wiki/ by
   default; other project slugs must be opted in explicitly.
** memory/daily/*.md is gitignored for privacy (your raw work log). Back up
   separately if you want history preserved.
```

---

## 8. Disaster Recovery

**Scenario: disk dies, new machine.** Recovery in order:

1. Install prerequisites (Node, Python+uv, Git, age).
2. `git clone git@github.com:Ekgardt/dotfiles.git D:\dev\dotfiles`
3. `cd D:\dev\dotfiles && ./install.ps1`   ← restores ALL configs, plugins, profiles
4. `git clone git@github.com:Ekgardt/llm-wiki.git D:\LLM-wiki`
5. Set `LLM_WIKI_ROOT` and `LLM_WIKI_STATE_ROOT` env vars (the installer does this).
6. Decrypt secrets: `age -d -i ~/.config/age/keys.txt -o ~/.config/dotfiles-env/.env dotfiles/secrets/secrets.env.age` (requires the age key for THIS machine — if you lost the key, re-encrypt from a trusted backup or re-enter secrets).
7. Clone each project repo.
8. Verify: open opencode, run a search, confirm guard rails inject.

If step 8 fails, something was only in Zone 3 (runtime) and is now gone. Add it to Zone 2.

---

This document is part of the LLM-Wiki vault. Agents should read it when setting up a new project, adding a new skill, or deciding where to store a new artifact. Keep it updated as the system evolves.
