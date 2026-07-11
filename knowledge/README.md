# knowledge/ — Knowledge zone

All durable and episodic content lives under this tree. Runtime state does **not**.

| Path | Role |
|------|------|
| `daily/` | Append-only session capture (`YYYY-MM-DD.md`) |
| `notes/` | Compiled durable pages (OKF frontmatter) |
| `projects/<slug>/` | Per-project `state.md`, context, blackboard |
| `raw/` | Immutable source material |
| `inbox/` | Unprocessed staging before compile |
| `feedback/` | Correction candidates (promote manually) |
| `index.md` / `log.md` | Catalog + chronological editorial log |

## Runtime (not here)

Indexes, PID locks, queues, lint reports:

```
$LLM_WIKI_STATE_ROOT/{run,logs,cache}
```

Default: the vault itself → `cache/` (incl. `cache/cognee/`), `logs/`, `run/`
at vault root, all gitignored. Override via `LLM_WIKI_STATE_ROOT` (tests use
a temp dir).
