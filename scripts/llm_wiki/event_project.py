"""The project an event's worktree belongs to, named by its knowledge slug."""
from __future__ import annotations

from pathlib import Path

from event_envelope import EventEnvelope
from llm_wiki import runtime_roots
from session_start_project_state import compute_slug


def project_context(envelope: EventEnvelope) -> tuple[str | None, Path | None]:
    if not envelope.worktree:
        return None, None
    try:
        project_dir = Path(envelope.worktree).resolve()
        slug = compute_slug(project_dir, runtime_roots.vault_root() / "knowledge" / "projects")
    except (OSError, ValueError):
        return None, None
    return slug, project_dir
