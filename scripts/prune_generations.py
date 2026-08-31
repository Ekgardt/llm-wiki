"""Collect the evidence-graph generations nothing reads any more.

`CLAUDE.md` calls `cache/` disposable and regenerable, and every published
generation is immutable after activation. Neither sentence collected anything:
the only removal path the product had, `GenerationCatalog.discard_unactivated`,
refuses any generation that was ever activated, which on a vault that builds
nightly is all of them but one. Measured here on 2026-08-29: 35 registered
generations, 6.3 GB, the oldest from 2026-08-21, growing about 180 MB a night.

What is kept is reachability from the active pointer, not an age window. The
active generation is the one thing anything reads — retrieval resolves the
pointer, and the incremental rebuild names the active generation as its reuse
parent — and one ancestor is kept behind it, because that ancestor is the first
alternative `_fallback_order` offers when the active tree stops validating. The
depth is `generation_catalog.RETAINED_ANCESTOR_GENERATIONS`, which carries the
reason. Everything else is dropped: registration, activation history and the
directory together, inside the catalog's own write transaction.

Two things this pass refuses to decide. A registration whose tree is missing, or
a tree with no registration, is the residue of an interrupted operation — that
is evidence, not garbage, and it is reported, never removed. A registration that
has never been activated is either an abandoned publication or one in flight
right now; `register` returns before `activate` is called, so the two look
identical from here. Those are reported as pending and left to
`discard_unactivated`, which runs under the publication fence.

Research: `docs/research/2026-08-29-how-many-superseded-generations-to-keep.md`.

Usage:
    uv run python scripts/prune_generations.py            # dry run (plan only)
    uv run python scripts/prune_generations.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generation_catalog import (  # noqa: E402
    RETAINED_ANCESTOR_GENERATIONS,
    GenerationCatalog,
)
from memory_state import STATE_ROOT  # noqa: E402

# One prune reads the catalog, walks two directories and unlinks whole trees on
# a local disk. Twenty minutes is the same budget the generation refresh gets
# and is far above the 6.3 GB case this was written for; it exists so a stuck
# filesystem ends the weekly step instead of the weekly pass.
PRUNE_BUDGET_SECONDS = 20 * 60.0

# A generation is a handful of large files. The ceiling refuses a directory that
# is no longer one rather than walking an unbounded tree to size it.
MAX_GENERATION_ENTRIES = 4096


class PrunePlan:
    """What retention keeps, what it drops, and what it refuses to judge."""

    def __init__(
        self,
        retained: tuple[str, ...],
        prunable: tuple[str, ...],
        unpaired: tuple[str, ...],
        pending: tuple[str, ...],
    ) -> None:
        self.retained = retained
        self.prunable = prunable
        self.unpaired = unpaired
        self.pending = pending


def _generation_directories(generations_path: Path) -> set[str]:
    if not generations_path.is_dir():
        return set()
    return {entry.name for entry in generations_path.iterdir() if entry.is_dir()}


def _directory_bytes(path: Path) -> int:
    """Bytes this generation occupies, refusing a tree that is no longer one."""
    total = 0
    seen = 0
    for current, _directories, files in os.walk(path):
        seen += len(files)
        _require_bounded_entries(seen)
        total += sum(_entry_bytes(Path(current) / name) for name in files)
    return total


def _require_bounded_entries(seen: int) -> None:
    if seen > MAX_GENERATION_ENTRIES:
        raise ValueError("generation entry ceiling exceeded")


def _entry_bytes(path: Path) -> int:
    try:
        return path.lstat().st_size
    except OSError:
        return 0


def _prune_candidates(
    retained: tuple[str, ...], registered: set[str], on_disk: set[str]
) -> set[str]:
    """Without an active pointer there is no root, so nothing is provably
    unreachable and nothing is a candidate."""
    if not retained:
        return set()
    return (registered & on_disk) - set(retained)


def plan_prune(
    catalog: GenerationCatalog, *, retained_ancestors: int = RETAINED_ANCESTOR_GENERATIONS
) -> PrunePlan:
    """Decide, without removing anything, which generations retention drops."""
    retained = catalog.retained_generations(retained_ancestors=retained_ancestors)
    registered = set(catalog.registered_generation_ids())
    activated = catalog.activated_generation_ids()
    on_disk = _generation_directories(catalog.generations_path)
    unpaired = registered.symmetric_difference(on_disk)
    candidates = _prune_candidates(retained, registered, on_disk)
    return PrunePlan(
        retained,
        tuple(sorted(candidates & activated)),
        tuple(sorted(unpaired)),
        tuple(sorted(candidates - activated)),
    )


def _discard_one(
    catalog: GenerationCatalog, identifier: str, retained_ancestors: int
) -> tuple[str, int]:
    """Size the tree before it goes, so the report can say what was reclaimed."""
    reclaimed = _directory_bytes(catalog.generations_path / identifier)
    catalog.discard_superseded(
        identifier,
        retained_ancestors=retained_ancestors,
        deadline=time.monotonic() + PRUNE_BUDGET_SECONDS,
    )
    return f"removed {identifier} ({reclaimed} bytes)", reclaimed


def _discard_reporting_failure(
    catalog: GenerationCatalog, identifier: str, retained_ancestors: int
) -> tuple[str, int]:
    try:
        return _discard_one(catalog, identifier, retained_ancestors)
    except (OSError, ValueError, TimeoutError, RuntimeError) as error:
        return f"ERROR: {identifier}: {error}", 0


def _planned(plan: PrunePlan) -> list[str]:
    return [f"would remove {identifier}" for identifier in plan.prunable]


def _applied(
    catalog: GenerationCatalog, plan: PrunePlan, retained_ancestors: int
) -> list[str]:
    outcomes = []
    reclaimed = 0
    for identifier in plan.prunable:
        line, freed = _discard_reporting_failure(catalog, identifier, retained_ancestors)
        outcomes.append(line)
        reclaimed += freed
    outcomes.append(f"reclaimed {reclaimed} bytes")
    return outcomes


def _retention_lines(plan: PrunePlan) -> list[str]:
    """Kept first, then the kinds this pass refuses to decide."""
    kept = [f"keeping {identifier}" for identifier in plan.retained]
    rootless = _rootless_lines(plan)
    unpaired = [
        f"UNPAIRED: {identifier}: registration and tree disagree"
        for identifier in plan.unpaired
    ]
    pending = [
        f"PENDING: {identifier}: registered but never activated"
        for identifier in plan.pending
    ]
    return kept + rootless + unpaired + pending


def _rootless_lines(plan: PrunePlan) -> list[str]:
    if plan.retained:
        return []
    return ["ERROR: catalog names no active generation; nothing is collectable"]


def prune_generations(
    *,
    state_root: Path | None = None,
    retained_ancestors: int = RETAINED_ANCESTOR_GENERATIONS,
    apply: bool = False,
) -> list[str]:
    """Report the retention decision; only `apply` removes anything."""
    catalog = GenerationCatalog(state_root or STATE_ROOT)
    plan = plan_prune(catalog, retained_ancestors=retained_ancestors)
    if not apply:
        return _retention_lines(plan) + _planned(plan)
    return _retention_lines(plan) + _applied(catalog, plan, retained_ancestors)


def _count_prefixed(outcomes: list[str], prefix: str) -> int:
    return len([line for line in outcomes if line.startswith(prefix)])


def _print_lines(outcomes: list[str]) -> None:
    for line in outcomes:
        print(f"  {line}")


def _report(outcomes: list[str]) -> int:
    """A pending publication is normal and does not fail the pass; a
    registration and a tree that disagree is a half-finished operation."""
    _print_lines(outcomes)
    failures = _count_prefixed(outcomes, "ERROR:")
    unpaired = _count_prefixed(outcomes, "UNPAIRED:")
    pending = _count_prefixed(outcomes, "PENDING:")
    print(
        f"prune_generations: {failures} failed, {unpaired} unpaired, "
        f"{pending} pending activation"
    )
    return min(1, failures + unpaired)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--retained-ancestors", type=int, default=RETAINED_ANCESTOR_GENERATIONS
    )
    parser.add_argument("--apply", action="store_true", help="remove the generations")
    arguments = parser.parse_args(argv)
    if arguments.retained_ancestors < 0:
        parser.error("--retained-ancestors cannot be negative")
    return _report(
        prune_generations(
            retained_ancestors=arguments.retained_ancestors, apply=arguments.apply
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
