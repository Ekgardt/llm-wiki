# ⚠ THIS IS THE PUBLIC SOURCE REPO — NOT THE RUNNING MEMORY SYSTEM

**You are in `D:\projects\llm-wiki\` — the clean public source code of the
LLM-wiki memory system. This is where you DEVELOP the system. It is NOT
where the system runs.**

## The running instance is elsewhere

The INSTALLED, RUNNING memory system — with real user data — lives at:

    D:\tools-agent\llm-wiki\   (= $LLM_WIKI_ROOT)

**These two folders are completely separate entities. Never mix them.**

## What you may do here

- Edit source code (`scripts/`, `tests/`, `install.sh`, etc.)
- Edit public docs (`README.md`, `docs/`, `CONTRIBUTING.md`)
- Run tests: `uv run pytest -q`
- Commit and push to `Ekgardt/llm-wiki` (public repo)

## What you must NEVER do here

- **NEVER create personal knowledge pages** in `wiki/concepts/` or
  `memory/knowledge/` — those dirs hold PUBLIC EXAMPLES only (enforced by
  `.gitignore` allowlist). Personal knowledge goes in `$LLM_WIKI_ROOT`.
- **NEVER write daily logs, session state, or project state here.** The
  running system writes those to `$LLM_WIKI_ROOT` (the installed instance),
  not here.
- **NEVER run `compile_memory.py`, `flush_memory.py`, or any memory pipeline
  script against this folder.** These scripts operate on `$LLM_WIKI_ROOT`.
- **NEVER commit user data** (decisions about your projects, debugging notes
  from your work, personal design rules) to this repo. It is PUBLIC.

## How to tell which folder you're in

| Signal | `D:\projects\llm-wiki\` (HERE) | `D:\tools-agent\llm-wiki\` |
|---|---|---|
| Role | Public source (dev) | Installed tool (runtime) |
| Push | `git@github.com:Ekgardt/llm-wiki.git` | `no-push` (blocked) |
| User data | NONE (clean examples only) | YOUR memory, daily logs, state |
| This AGENTS.md | Says "PUBLIC SOURCE" | Does not exist (or says "INSTALLED") |
| `$LLM_WIKI_ROOT` | Does NOT point here | Points here |

If unsure: run `git remote get-url --push origin`. If it says `no-push`, you
are in the installed instance. If it says a real GitHub URL, you are here
(public source).

## When asked to "work on the memory system"

If the user asks to improve the memory system, you develop HERE (edit code,
run tests, commit, push). The installed instance picks up updates via
`D:\tools-agent\manage.ps1 update` (git pull).

If the user asks about their personal memory data, that's the INSTALLED
instance at `$LLM_WIKI_ROOT` — not here.
