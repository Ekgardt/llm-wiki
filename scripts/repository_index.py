"""CODE-03: generations for repositories other than the vault.

Three verbs over the one existing catalog -- `index` a named repository, `list`
what is indexed, `detect` what changed since. Nothing here adds a graph, a
catalog, a runtime root or a daemon: a foreign repository's generation is an
ordinary immutable generation under `cache/evidence-graph/generations/`,
disposable derived cache like every other one.

Two rules run through the whole module.

**A foreign repository is read-only.** Nothing is written into it -- no config,
no marker, no lock, not even a temporary file. Every path this module writes is
under the vault's own state root.

**A foreign generation is registered, never activated.** The catalog has one
active pointer and it belongs to the vault. Activating a foreign generation
would make the vault's own scope unresolvable, and every knowledge query would
lose its generation and read Markdown directly (`no_active_generation`) --
NEW-65, recreated on purpose. Selection for a
foreign scope is `GenerationCatalog._scoped_generation`, which never moves the
pointer.

Design and sources: `docs/research/2026-08-28-generations-for-other-repositories.md`
and `docs/research/2026-08-29-which-directories-of-a-repository-hold-code.md`,
which answers what decides that a directory of a foreign repository holds code:
the set Git tracks, minus what the corpus walk prunes, overridable by `roots`.
"""
from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path

import repository_indexing
from llm_wiki import runtime_roots
from llm_wiki.step_budgets import REFRESH_ALL_BUDGET_SECONDS
from repository_refusal import SCHEMA_VERSION, RepositoryIndexRefused

_CLI_OK_STATUSES = frozenset(
    {
        "ok",
        "indexed",
        "fresh",
        "refreshed",
        "refresh_owned_elsewhere",
        "followed",
        "already_indexed",
        "planned",
    }
)


def refresh_row(row: Mapping, state_root: Path | None, deadline: float) -> dict:
    from ephemeral_paths import is_throwaway_checkout

    checkout = row.get("checkout_root")
    if not checkout or not Path(str(checkout)).is_dir():
        return {"checkout_root": checkout, "status": "checkout_missing"}
    if is_throwaway_checkout(Path(str(checkout))):
        return {"checkout_root": checkout, "status": "throwaway_not_refreshed"}
    return _refreshed_row(checkout, state_root, deadline)


def _refreshed_row(checkout: object, state_root: Path | None, deadline: float) -> dict:
    try:
        return repository_indexing.refresh_repository(checkout, state_root=state_root, deadline=deadline)
    except RepositoryIndexRefused as refusal:
        return {"checkout_root": checkout, **refusal.as_dict()}
    except TimeoutError as stopped:
        return {"checkout_root": checkout, **repository_indexing.deferred(stopped)}


def _vault_checkout(state_root: Path | None) -> Path:

    del state_root
    return Path(runtime_roots.vault_root()).resolve()


def indexed_already(directory: Path, state_root: Path | None, deadline: float) -> bool:
    """Whether this checkout already has a code generation registered.

    One catalog lookup. Asking `detect_repository_changes` captured the whole
    checkout a second time every night -- and a third when it was stale -- to
    learn something the catalog answers without reading a file. See
    `docs/research/2026-09-17-small-corrections-in-the-generations-area.md`.
    """
    admission = repository_indexing.admit_repository(directory, state_root=state_root, deadline=deadline)
    catalog = repository_indexing.open_catalog(repository_indexing.state_root_path(state_root), read_only=True)
    if catalog is None:
        return False
    generation_id, _manifest = repository_indexing.newest_generation_for(
        catalog, admission.scope, deadline
    )
    return generation_id is not None


def adopted_vault(state_root: Path | None, deadline: float) -> dict:
    """Index the vault's own checkout once, so the timer keeps it fresh after.

    The vault is a repository too (decision 2026-09-12), and the owner's standing
    requirement is that this needs no operator action. A checkout that already
    has a generation is left to the ordinary refresh below.
    """
    directory = _vault_checkout(state_root)
    try:
        if indexed_already(directory, state_root, deadline):
            return {"status": "current", "directory": str(directory)}
        return repository_indexing.index_repository(directory, state_root=state_root, deadline=deadline)
    except RepositoryIndexRefused as refusal:
        return refusal.as_dict()
    except TimeoutError as stopped:
        return {"directory": str(directory), **repository_indexing.deferred(stopped)}


def registered_rows(state_root: Path | None, deadline: float) -> list:
    """The registered checkouts, or none when the budget ran out before the list."""
    try:
        return repository_indexing.list_repositories(state_root=state_root, deadline=deadline)["repositories"]
    except TimeoutError:
        return []


def _pass_status(rows: list, adopted: Mapping) -> str:
    """`deferred` when the budget ran out before anything registered could be read."""
    if not rows and adopted.get("status") == "deferred":
        return "deferred"
    return "ok"


def fair_refresh_deadline(deadline: float, pending: int) -> float:
    now = time.monotonic()
    return min(deadline, now + max(0.0, deadline - now) / pending)


def _refresh_rows_with_budget(rows, state_root, deadline):
    outcomes = []
    for position, row in enumerate(rows):
        bound = fair_refresh_deadline(deadline, len(rows) - position)
        outcomes.append(refresh_row(row, state_root, bound))
    return outcomes


def refresh_all_repositories(
    *,
    state_root: Path | None = None,
    budget_seconds: float = REFRESH_ALL_BUDGET_SECONDS,
) -> dict[str, object]:
    """The timer's pass: every registered checkout, then new worktrees.

    Every registered checkout that still exists is refreshed, then worktrees of
    a registered repository that have no generation yet are indexed
    (`repository_worktrees.follow_worktrees`, #24 D1) with the budget that is
    left: an incremental refresh is cheap and keeps answers current, a first
    build is the expensive part. A checkout that does not fit is deferred and
    named. A checkout that is gone is named here, not deleted: `retire` removes
    its generations. See `docs/research/2026-09-14-a-pass-that-keeps-its-budget.md`.
    """
    from repository_worktrees import follow_worktrees

    deadline = time.monotonic() + budget_seconds
    adopted = adopted_vault(state_root, deadline)
    rows = registered_rows(state_root, deadline)
    outcomes = _refresh_rows_with_budget(rows, state_root, deadline)
    followed = follow_worktrees(rows, state_root=state_root, deadline=deadline)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": _pass_status(rows, adopted),
        "adopted_vault": adopted,
        "followed": followed,
        "repositories": outcomes,
    }


def _cli_deadline(args) -> float:
    return time.monotonic() + float(args.budget_seconds)


def _cli_index(args) -> dict:
    return repository_indexing.index_repository(
        args.directory, roots=args.roots, state_root=args.state_root, deadline=_cli_deadline(args)
    )


def _cli_list(args) -> dict:
    return repository_indexing.list_repositories(state_root=args.state_root, deadline=_cli_deadline(args))


def _cli_detect(args) -> dict:
    return repository_indexing.detect_repository_changes(
        args.directory, state_root=args.state_root, deadline=_cli_deadline(args)
    )


def _cli_refresh(args) -> dict:
    return repository_indexing.refresh_repository(
        args.directory, state_root=args.state_root, deadline=_cli_deadline(args)
    )


def _cli_refresh_all(args) -> dict:
    return refresh_all_repositories(
        state_root=args.state_root, budget_seconds=float(args.budget_seconds)
    )


def _cli_follow(args) -> dict:
    from repository_worktrees import follow_worktree

    return follow_worktree(
        args.directory, roots=args.roots, state_root=args.state_root, deadline=_cli_deadline(args)
    )


def _cli_retire(args) -> dict:
    from repository_retention import failed_discards, retire_repositories

    answer = retire_repositories(state_root=args.state_root, apply=not args.dry_run)
    if failed_discards(answer):
        return {**answer, "status": "retire_failed"}
    return answer


_VERBS = {
    "index": _cli_index,
    "list": _cli_list,
    "detect": _cli_detect,
    "refresh": _cli_refresh,
    "refresh-all": _cli_refresh_all,
    "follow": _cli_follow,
    "retire": _cli_retire,
}
_DIRECTORY_VERBS = frozenset({"index", "detect", "refresh", "follow"})


def _parser():
    import argparse

    parser = argparse.ArgumentParser(
        description="Generations for repositories other than the vault (CODE-03, #24)."
    )
    parser.add_argument("verb", choices=sorted(_VERBS))
    parser.add_argument("directory", nargs="?", help="The repository checkout root.")
    parser.add_argument("--state-root", type=Path, default=None)
    parser.add_argument("--roots", nargs="*", default=None, help="Explicit code roots.")
    parser.add_argument(
        "--budget-seconds", type=float, default=REFRESH_ALL_BUDGET_SECONDS
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="retire: report the plan, remove nothing."
    )
    return parser


def _cli_answer(args) -> dict:
    try:
        return _VERBS[args.verb](args)
    except RepositoryIndexRefused as refusal:
        return refusal.as_dict()


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.verb in _DIRECTORY_VERBS and args.directory is None:
        parser.error(f"{args.verb} needs a repository directory")
    answer = _cli_answer(args)
    print(json.dumps(answer, indent=2, sort_keys=True, default=str))
    return 0 if answer.get("status") in _CLI_OK_STATUSES else 2


if __name__ == "__main__":
    raise SystemExit(main())
