"""Uniform, JSON-compatible response contract for local MCP operations."""
from __future__ import annotations

import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from llm_wiki.mcp_response_values import (
    COMPONENT_FRESHNESS as COMPONENT_FRESHNESS,
)
from llm_wiki.mcp_response_values import (
    SCHEMA_VERSION as SCHEMA_VERSION,
)
from llm_wiki.mcp_response_values import (
    _as_utc,
    _bounded,
    _components,
    _freshness,
    _json_safe,
)
from llm_wiki.mcp_response_values import (
    envelope_schema as envelope_schema,
)


def build_envelope(
    data: Any,
    *,
    root: Path | None = None,
    state_root: Path | None = None,
    now: datetime | None = None,
    coverage: float = 1.0,
    confidence: float = 1.0,
    fallback: bool = False,
    partial: bool = False,
    warnings: list[Any] | None = None,
    components: dict[str, dict[str, Any]] | None = None,
    index_timestamp: str | None = None,
    source_root: str | None = None,
) -> dict[str, Any]:
    """Build one conservative response envelope from local metadata.

    `index_timestamp` is when the index behind the answer was built, for the
    answers that read one; None otherwise. `source_root` is the checkout an
    answer about some other repository was read from: its HEAD, not the
    vault's, is the answer's `source_commit`, and a root that is not absolute
    names no commit (audit 2026-09-26 C-9).
    """
    coverage = _bounded("coverage", coverage)
    confidence = _bounded("confidence", confidence)
    generated_at = _as_utc(now or datetime.now(UTC))
    vault_root, runtime_root = _roots(root, state_root)
    response_warnings = [str(item) for item in warnings or []]

    component_details = _components(components)
    freshness = _freshness(component_details)
    source_commit = _answer_commit(source_root, vault_root)
    if source_commit is None:
        response_warnings.append("Source commit is unavailable.")

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "index_timestamp": index_timestamp,
        "source_commit": source_commit,
        "freshness": freshness,
        "coverage": coverage,
        "confidence": confidence,
        "fallback": bool(fallback),
        "partial": bool(partial),
        "warnings": response_warnings,
        "components": component_details,
        "data": _json_safe(data),
    }


def _roots(root: Path | None, state_root: Path | None) -> tuple[Path, Path]:
    if root is not None:
        vault_root = Path(root).resolve()
        return vault_root, _state_root_or(state_root, vault_root)
    return _default_roots(state_root)


def _state_root_or(state_root: Path | None, fallback: Path) -> Path:
    return Path(state_root or fallback).resolve()


def _default_roots(state_root: Path | None) -> tuple[Path, Path]:
    try:
        from memory_state import ROOT, STATE_ROOT

        return ROOT, _state_root_or(state_root, STATE_ROOT)
    except (ImportError, OSError):
        fallback_root = Path(__file__).resolve().parent.parent
        return fallback_root, _state_root_or(state_root, fallback_root)


# The checkout's commit, read again after this long: cached for the life of the
# server, it named the commit from before the nightly fast-forward (audit B-34).
SOURCE_COMMIT_TTL_SECONDS = 5.0
_SOURCE_COMMITS: dict[str, tuple[float, str | None]] = {}


def _answer_commit(source_root: str | None, vault_root: Path) -> str | None:
    if source_root is None:
        return _source_commit(str(vault_root))
    if not Path(source_root).is_absolute():
        return None
    return _source_commit(source_root)


def _source_commit(root: str) -> str | None:
    cached = _SOURCE_COMMITS.get(root)
    now = time.monotonic()
    if cached is not None and cached[0] > now:
        return cached[1]
    commit = _read_source_commit(root)
    _SOURCE_COMMITS[root] = (now + SOURCE_COMMIT_TTL_SECONDS, commit)
    return commit


def _read_source_commit(root: str) -> str | None:
    try:
        result = subprocess.run(
            # Any repository may be asked about, so its config names no command.
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    if result.returncode != 0 or not commit:
        return None
    return commit
