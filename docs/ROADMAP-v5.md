# v5.0 Roadmap

> v4.0 is COMPLETE. All planned features shipped. This file tracks
> ideas for future versions, not incomplete work.

## Resolved (implemented in v4.0)

- ✅ Bi-temporal code edges — git commit tracking per symbol (`valid_from`).
- ✅ Label propagation community detection — pure Python, zero deps.
- ✅ Constrained decoding — `call_llm_json()` through existing providers.

## Removed from roadmap (not needed for personal knowledge vault)

- ~~Quality calibration~~ — existing lint checks (14) + access tracking
  + archive thresholds already provide quality signals. Logistic regression
  on top adds complexity without new information. This is an enterprise
  RAG feature (Cognee, Zep), not a personal vault feature.
- ~~Code health markers (25)~~ — existing tools (ruff, radon) cover
  complexity/linting. The "calibrated against defect corpus" part is
  an enterprise team-lead feature (repowise target audience), not a
  solo developer memory tool.

## Future ideas (v5.0+, not planned yet)

- Bi-temporal time-travel queries ("show auth as of March commit X")
- Cross-service API topology (HTTP call graph between repositories)
- Temporal scoring modes (impact, novelty, recency — Memtrace pattern)
- tree-sitter .scm queries for more languages (Go, Rust, C)
