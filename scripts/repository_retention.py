"""Retire the repository generations no checkout will read again (#24, D1).

A foreign repository's generation is registered and never activated, so the
vault's pruner (`prune_generations.py`) reports each one as pending and removes
none: every refresh added a generation and nothing took one away, and a
worktree removed by its agent left all of its generations behind. This pass
decides per checkout, by identity, never by age:

* the checkout root is gone - retire every generation of it;
* its branch or repository turned indexing off (`repository_worktrees`) -
  retire every generation of it;
* otherwise keep the newest generation and the one behind it (the depth and
  the reason are the vault's: `RETAINED_ANCESTOR_GENERATIONS`, a reader that
  resolved just before the last refresh) and retire the rest.

Every discard is `GenerationCatalog.discard_unactivated`, which refuses a
generation that is active or was ever activated, and runs under the same
per-repository lease a refresh takes, so it cannot race a refresh of that
repository. The vault's own generations are never considered. A hint table
whose checkout has no generation left is removed with it. Only `cache/` is
touched; `run/` is not. Research:
`docs/research/2026-09-11-the-graph-meets-the-agent-where-it-searches.md`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

import repository_index as index

SCHEMA_VERSION = "repository-retention/v1"
RETIRE_BUDGET_SECONDS = 5 * 60.0
DISCARD_ERRORS = (OSError, ValueError, TimeoutError, RuntimeError, PermissionError)


# --------------------------------------------------------------------------
# plan: which generations each foreign checkout keeps
# --------------------------------------------------------------------------


def _scope_of(manifest: Mapping) -> Mapping | None:
    scope = manifest.get("repository_scope")
    return scope if isinstance(scope, Mapping) else None


def _vault_root() -> Path:
    from memory_state import ROOT

    return Path(ROOT).resolve()


def _is_vault(scope: Mapping, vault: Path) -> bool:
    return Path(str(scope.get("checkout_root", ""))).resolve() == vault


def _foreign_groups(manifests, activated: frozenset[str]) -> dict[str, dict]:
    """checkout_id -> {scope, generations newest first}; vault and activated excluded."""
    vault = _vault_root()
    groups: dict[str, dict] = {}
    for identifier, _registered_at, manifest in manifests:
        scope = _scope_of(manifest)
        if scope is None or identifier in activated or _is_vault(scope, vault):
            continue
        group = groups.setdefault(str(scope.get("checkout_id")), {"scope": scope, "generations": []})
        group["generations"].append(identifier)
    return groups


def _verdict(scope: Mapping) -> str:
    from repository_worktrees import indexing_marked_off

    root = Path(str(scope.get("checkout_root", "")))
    if not root.is_dir():
        return "checkout_missing"
    if indexing_marked_off(root) is not None:
        return "marked_not_indexed"
    return "kept"


def _kept_count(verdict: str) -> int:
    from generation_catalog import RETAINED_ANCESTOR_GENERATIONS

    if verdict != "kept":
        return 0
    return 1 + RETAINED_ANCESTOR_GENERATIONS


def _planned(checkout_id: str, group: dict) -> dict:
    scope = group["scope"]
    verdict = _verdict(scope)
    keep = _kept_count(verdict)
    return {
        "checkout_id": checkout_id,
        "repository_id": scope.get("repository_id"),
        "checkout_root": scope.get("checkout_root"),
        "verdict": verdict,
        "kept": group["generations"][:keep],
        "retire": group["generations"][keep:],
    }


def plan_retention(catalog, *, deadline: float | None = None) -> list[dict]:
    """What each foreign checkout keeps and retires; nothing is removed here."""
    manifests = catalog.registered_manifests(deadline=deadline)
    groups = _foreign_groups(manifests, catalog.activated_generation_ids(deadline=deadline))
    return [_planned(checkout_id, group) for checkout_id, group in sorted(groups.items())]


# --------------------------------------------------------------------------
# apply: one repository at a time, under its lease
# --------------------------------------------------------------------------


def _discarded(catalog, identifier: str, deadline: float) -> dict:
    try:
        removed = catalog.discard_unactivated(identifier, deadline=deadline)
    except DISCARD_ERRORS as error:
        return {"generation_id": identifier, "status": "failed", "reason": type(error).__name__}
    return {"generation_id": identifier, "status": "retired" if removed else "kept_in_use"}


def _retired_checkout(catalog, entry: dict, state_root: Path, deadline: float) -> dict:
    from code_hints import remove_hints

    outcomes = [_discarded(catalog, identifier, deadline) for identifier in entry["retire"]]
    hints_removed = False
    if not entry["kept"]:
        hints_removed = remove_hints(state_root, str(entry["checkout_id"]))
    return {**entry, "retired": outcomes, "hints_removed": hints_removed}


def _by_repository(entries: Iterable[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        grouped.setdefault(str(entry["repository_id"]), []).append(entry)
    return grouped


def _retire_repository(catalog, repository_id: str, entries: list[dict], state_root: Path, deadline: float) -> list[dict]:
    def work(bound: float, _stop: Callable[[], bool]) -> dict:
        return {"entries": [_retired_checkout(catalog, entry, state_root, bound) for entry in entries]}

    outcome = index.run_fenced(repository_id, state_root, deadline, None, work)
    if outcome.get(index.FENCE_REFUSED):
        return [{**entry, "retired": [], "status": outcome["status"]} for entry in entries]
    return outcome["entries"]


def _has_work(entry: dict) -> bool:
    return bool(entry["retire"]) or not entry["kept"]


def _orphan_hints(state_root: Path, plan: list[dict]) -> list[str]:
    """Hint tables whose checkout holds no generation any more."""
    from code_hints import hinted_checkout_ids, remove_hints

    live = {entry["checkout_id"] for entry in plan if entry["kept"]}
    orphans = sorted(hinted_checkout_ids(state_root) - live)
    return [checkout_id for checkout_id in orphans if remove_hints(state_root, checkout_id)]


def _existing_catalog(root: Path):
    """A writable catalog, but only one that already exists: retention never creates one."""
    if index._open_catalog(root, read_only=True) is None:  # noqa: SLF001
        return None
    return index._open_catalog(root, read_only=False)  # noqa: SLF001


def retire_repositories(
    *,
    state_root: Path | None = None,
    apply: bool = True,
    budget_seconds: float = RETIRE_BUDGET_SECONDS,
) -> dict[str, object]:
    """The nightly retention pass; `apply=False` reports the plan only."""
    root = index.state_root_path(state_root)
    catalog = _existing_catalog(root)
    if catalog is None:
        return {"schema_version": SCHEMA_VERSION, "status": "ok", "checkouts": [], "orphan_hints_removed": []}
    deadline = time.monotonic() + budget_seconds
    plan = plan_retention(catalog, deadline=deadline)
    if not apply:
        return {"schema_version": SCHEMA_VERSION, "status": "planned", "checkouts": plan}
    return _applied(catalog, plan, root, deadline)


def _applied(catalog, plan: list[dict], root: Path, deadline: float) -> dict[str, object]:
    pending = _by_repository(entry for entry in plan if _has_work(entry))
    checkouts: list[dict] = []
    for repository_id, entries in sorted(pending.items()):
        checkouts.extend(_retire_repository(catalog, repository_id, entries, root, deadline))
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "checkouts": checkouts,
        "orphan_hints_removed": _orphan_hints(root, plan),
    }


def failed_discards(answer: Mapping) -> int:
    """How many discards failed; the command line exits non-zero on any."""
    return sum(
        1
        for checkout in answer.get("checkouts", [])
        for outcome in checkout.get("retired", [])
        if outcome.get("status") == "failed"
    )
