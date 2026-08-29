"""Bounded daytime freshness for the evidence generation, without a daemon.

The nightly pass at 03:00 is the only thing that has ever cleared `stale`, so
between 03:00 and the next 03:00 the vault answers from an index that does not
contain what was written that day. Measured here on 2026-08-28 at 17:05: 84 of
the vault's 821 sources differed from the active generation — 33 changed, 51
added — every one of them written after that night's build.

This module is the observer that closes that window, and it is deliberately not
a process. It offers one bounded step, `refresh_if_quiet`, which answers
"nothing to do" for very little and only then spends anything, and one bounded
session, `run_watch_session`, which is that step on a timer with a hard wall
bound, a hard CPU bound, and a hard cap on how many refreshes it may start. It
holds no state on disk, starts no thread, and leaves nothing running: when the
process that called it exits, the watching is over.

Three tiers, cheapest first, each measured on this vault:

* **tier 0 — identity probe**, `corpus_snapshot.probe_corpus_identity`: 0.078 s
  of CPU for 831 files. Answers *is anything newer than the generation* and *has
  the vault been quiet long enough to be worth a rebuild*. Never hashes.
* **tier 1 — the authoritative snapshot**, `corpus_snapshot.collect_corpus`:
  1.21 s of CPU. Answers *is it actually stale*, by the same digests the builder
  and the doctor use. It exists because tier 0's timestamps have a known false
  positive — a file touched without a byte changing — and because this
  repository has been bitten before by two definitions of one thing.
* **tier 2 — the existing bounded fenced builder**,
  `doctor.run_generation_maintenance`. Nothing new: the same call the nightly
  pass makes, with a smaller budget.

What this module does not do, and the reason is measured rather than assumed:
it does not make a refresh cheap. See `REFRESH_BUDGET_SECONDS`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from corpus_snapshot import (  # noqa: E402
    APPROVED_CODE_ROOTS,
    CorpusChanged,
    collect_corpus,
    probe_corpus_identity,
)

NANOS = 1_000_000_000

# How long the corpus must have been unwritten before a refresh is worth
# starting. Measured on this vault over 20 minutes of a normal working
# afternoon on 2026-08-28: 117 probes, 25 distinct change events, gaps between
# consecutive writes of 10.2 s to 144.4 s with a median near 41 s, and only one
# gap over two minutes. A window much larger than this never opens on a busy
# machine; a window much smaller starts a build into a write. Sixty seconds is
# the largest value that still opened ten times in that twenty minutes.
QUIET_WINDOW_SECONDS = 60.0

# How often the bounded session takes tier 0. At 0.078 s of CPU per probe this
# is 0.26% of one core; over a sixteen-hour day it is 150 s of CPU.
PROBE_INTERVAL_SECONDS = 30.0

# The bound handed to the builder, and the number that decides whether daytime
# freshness is affordable at all. Measured on a copy of this vault's corpus
# (822 sources, 21 MB) on 2026-08-28, four cores under load, CPU seconds because
# they do not move with the load:
#
#   full build from nothing        806.6 s CPU  (935.7 s wall)
#   nothing changed at all         808.1 s CPU  (1152.1 s wall)  -> rebuilt
#   one note added                 757.9 s CPU  (659.8 s wall)
#   one note edited                730.0 s CPU  (520.3 s wall)
#
# A one-file change costs 90% of a build from nothing, and an unchanged vault
# rebuilds rather than reporting `current`. Instrumented, one such build spends
# 677.9 of its 721.2 CPU seconds inside `build_full_generation`, and 595.1 of
# those — 82.5% of the whole pass — re-embedding all 3,158 chunks.
#
# That was measured on 2026-08-28 and the cause it named has since been fixed:
# `_stored_incremental_manifest` used to drop the parent's reuse manifest above
# a 64 MiB constant while this corpus produced 158,075,010 bytes of it, so no
# published generation carried an `incremental-manifest.json` and nothing was
# ever reused. Since `283eb3a` the manifest is bounded by the size the sealed
# `manifest.json` declares, both live generations carry one, and reuse works —
# measured 2026-08-29, an idle pass answers `current` in 4.51 s and the build
# before it reused 487 of 910 sources. The paragraph above is kept as the
# reason this bound exists, not as a description of today.
#
# Twenty minutes is therefore not a comfortable bound. It is the smallest bound
# that a successful build has ever fitted inside here, and it is deliberately
# short enough that a refusal is reported instead of an hour being spent.
REFRESH_BUDGET_SECONDS = 20 * 60.0

# A file written while the previous build was running has a modification time
# older than the generation's manifest but is not in the generation. Tier 0
# therefore treats anything written within one build's duration of the
# generation's own timestamp as suspect and lets tier 1 decide. The cost of
# being wrong here is one extra tier-1 snapshot after each build; the cost of
# not doing it is a source that never gets indexed.
BUILD_SKEW_SECONDS = REFRESH_BUDGET_SECONDS

# Hard bounds on one session. The wall bound is what makes this not a daemon:
# the session cannot outlive it even if nothing ever goes quiet. The probe CPU
# bound covers the watching only — a refresh is accounted separately, because
# its cost is the builder's, not the watcher's. Two refreshes, not more: at
# 758 s of CPU each that is already 25 CPU-minutes, and a session that has lost
# two publication races is a session whose vault is too busy to index.
SESSION_WALL_BUDGET_SECONDS = 4 * 60 * 60.0
SESSION_PROBE_CPU_BUDGET_SECONDS = 120.0
MAX_REFRESHES_PER_SESSION = 2

# Bound on the tier-0 and tier-1 reads themselves, so a pathological vault
# cannot turn a probe into a hang.
PROBE_DEADLINE_SECONDS = 120.0

# The generation's own source manifest. 154 KB on this vault for 821 sources;
# eight megabytes is roughly fifty times that, and it is read through the
# bounded runtime reader rather than opened directly.
MAX_SOURCE_MANIFEST_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class FreshnessVerdict:
    """What one look at the vault concluded, and what it cost to conclude it."""

    status: str
    reason: str
    generation_id: str | None = None
    source_files: int = 0
    delta: int = 0
    quiet_seconds: float = 0.0
    tier: int = 0
    cost_seconds: float = 0.0

    @property
    def stale(self) -> bool:
        return self.status == "stale"


@dataclass
class SessionReport:
    """What one bounded session did, in the units the gate asks about."""

    verdicts: list[dict] = field(default_factory=list)
    refreshes: list[dict] = field(default_factory=list)
    probes: int = 0
    probe_cpu_seconds: float = 0.0
    refresh_cpu_seconds: float = 0.0
    wall_seconds: float = 0.0
    stopped_by: str = "wall_budget"


def approved_code_roots(root: Path) -> tuple[str, ...]:
    """Exactly the roots the builder indexes, so the probe cannot disagree.

    Read from `doctor` rather than re-derived: a second answer to "which roots
    are in the corpus" is how a watcher ends up chasing a delta the builder
    will never close.
    """
    from doctor import _approved_code_roots  # noqa: PLC2701 - one definition, not two

    return _approved_code_roots(root, set(APPROVED_CODE_ROOTS))


def _generation_directory(state_root: Path, generation_id: str) -> Path:
    return (
        Path(state_root) / "cache" / "evidence-graph" / "generations" / generation_id
    )


def _active_generation(state_root: Path, deadline: float) -> dict | None:
    from generation_catalog import GenerationCatalog

    try:
        return GenerationCatalog(Path(state_root)).get_active(deadline=deadline)
    except (OSError, ValueError, KeyError):
        return None


def _built_instant_ns(generation_path: Path) -> int | None:
    """When this generation was published, as the doctor's freshness check reads it."""
    try:
        return generation_path.joinpath("manifest.json").stat().st_mtime_ns
    except OSError:
        return None


def _manifest_digests(
    generation_path: Path, state_root: Path
) -> dict[str, str] | None:
    """The generation's own source digests, read through the bounded reader."""
    from reliable_memory import read_runtime_bytes

    try:
        raw = read_runtime_bytes(
            generation_path / "source-manifest.json",
            state_root,
            max_bytes=MAX_SOURCE_MANIFEST_BYTES,
        )
        stored = json.loads(raw)
        return {item["relative_path"]: item["sha256"] for item in stored["sources"]}
    except (OSError, PermissionError, ValueError, KeyError, TypeError):
        return None


def _source_delta(indexed: dict[str, str], snapshot) -> int:
    """How many sources the generation and the live vault disagree about."""
    current = {
        source.record.relative_path: source.record.sha256
        for source in snapshot.sources
    }
    return sum(
        indexed.get(path) != current.get(path)
        for path in indexed.keys() | current.keys()
    )


def _tier0_verdict(
    probe, built_ns: int, now_ns: int, window_seconds: float
) -> FreshnessVerdict | None:
    """Fresh, busy, or "ask tier 1" — decided from timestamps alone."""
    quiet_seconds = max(0.0, (now_ns - probe.newest_mtime_ns) / NANOS)
    if probe.newest_mtime_ns <= built_ns - int(BUILD_SKEW_SECONDS * NANOS):
        return FreshnessVerdict(
            status="fresh",
            reason="nothing_written_since_the_generation",
            source_files=probe.file_count,
            quiet_seconds=quiet_seconds,
            tier=0,
        )
    if quiet_seconds < window_seconds:
        return FreshnessVerdict(
            status="busy",
            reason="written_within_the_quiet_window",
            source_files=probe.file_count,
            quiet_seconds=quiet_seconds,
            tier=0,
        )
    return None


def _tier1_verdict(
    root: Path,
    state_root: Path,
    generation_path: Path,
    quiet_seconds: float,
    deadline: float,
) -> FreshnessVerdict:
    """The authoritative answer: the builder's own digests against live bytes."""
    indexed = _manifest_digests(generation_path, state_root)
    if indexed is None:
        return FreshnessVerdict(
            status="stale", reason="source_manifest_unreadable", tier=1
        )
    snapshot = collect_corpus(
        root,
        code_roots=approved_code_roots(root),
        deadline=deadline,
    )
    delta = _source_delta(indexed, snapshot)
    return FreshnessVerdict(
        status="stale" if delta else "fresh",
        reason="source_delta" if delta else "no_source_delta",
        source_files=len(snapshot.sources),
        delta=delta,
        quiet_seconds=quiet_seconds,
        tier=1,
    )


def _active_generation_id(state_root: Path, deadline: float) -> str | None:
    active = _active_generation(state_root, deadline)
    if active is None:
        return None
    return str(active["generation_id"])


@dataclass(frozen=True)
class _Target:
    """The three paths one look needs: the vault, its runtime, its generation."""

    root: Path
    state_root: Path
    generation_path: Path


def _looked_at_generation(
    target: _Target,
    built_ns: int,
    now_ns: int,
    window_seconds: float,
    deadline: float,
) -> FreshnessVerdict:
    probe = probe_corpus_identity(
        target.root, code_roots=approved_code_roots(target.root), deadline=deadline
    )
    early = _tier0_verdict(probe, built_ns, now_ns, window_seconds)
    if early is not None:
        return early
    quiet_seconds = max(0.0, (now_ns - probe.newest_mtime_ns) / NANOS)
    return _tier1_verdict(
        target.root,
        target.state_root,
        target.generation_path,
        quiet_seconds,
        deadline,
    )


def _looked_at_vault(
    root: Path,
    state_root: Path,
    now_ns: int,
    window_seconds: float,
    deadline: float,
) -> FreshnessVerdict:
    generation_id = _active_generation_id(state_root, deadline)
    if generation_id is None:
        return FreshnessVerdict(status="no_generation", reason="no_active_generation")
    generation_path = _generation_directory(state_root, generation_id)
    built_ns = _built_instant_ns(generation_path)
    if built_ns is None:
        return FreshnessVerdict(
            status="no_generation",
            reason="active_generation_unreadable",
            generation_id=generation_id,
        )
    target = _Target(root, state_root, generation_path)
    return _named(
        _looked_at_generation(target, built_ns, now_ns, window_seconds, deadline),
        generation_id,
    )


def _named(verdict: FreshnessVerdict, generation_id: str) -> FreshnessVerdict:
    from dataclasses import replace

    return replace(verdict, generation_id=generation_id)


def _priced(verdict: FreshnessVerdict, cpu_seconds: float) -> FreshnessVerdict:
    from dataclasses import replace

    return replace(verdict, cost_seconds=round(cpu_seconds, 4))


def check_freshness(
    root: Path,
    state_root: Path,
    *,
    quiet_window_seconds: float = QUIET_WINDOW_SECONDS,
    deadline_seconds: float = PROBE_DEADLINE_SECONDS,
    now_ns: int | None = None,
) -> FreshnessVerdict:
    """One bounded look at the vault. Never writes, never builds, never blocks."""
    started_cpu = time.process_time()
    deadline = time.monotonic() + float(deadline_seconds)
    instant = time.time_ns() if now_ns is None else int(now_ns)
    try:
        verdict = _looked_at_vault(
            Path(root), Path(state_root), instant, float(quiet_window_seconds), deadline
        )
    except CorpusChanged:
        # The vault was written to while it was being read. That is the ordinary
        # state of a live vault, and the honest answer is "ask again", not a
        # rebuild started on a corpus that is already moving.
        verdict = FreshnessVerdict(status="busy", reason="written_during_the_probe")
    except TimeoutError:
        # Its own bound, not the vault's fault, and a different sentence: the
        # observer ran out of time before it could say anything.
        verdict = FreshnessVerdict(status="busy", reason="probe_deadline_reached")
    return _priced(verdict, time.process_time() - started_cpu)


def _refresh(root: Path, state_root: Path, refresh_budget_seconds: float) -> dict:
    from doctor import DEFAULT_GENERATION_SOURCE_LIMIT, run_generation_maintenance

    return run_generation_maintenance(
        root=Path(root),
        state_root=Path(state_root),
        time_budget_seconds=float(refresh_budget_seconds),
        max_sources=DEFAULT_GENERATION_SOURCE_LIMIT,
    )


def refresh_if_quiet(
    root: Path,
    state_root: Path,
    *,
    quiet_window_seconds: float = QUIET_WINDOW_SECONDS,
    refresh_budget_seconds: float = REFRESH_BUDGET_SECONDS,
    now_ns: int | None = None,
) -> dict:
    """The bounded step: look, and rebuild only when looking says it is worth it.

    Safe to call from any process that already exists — a hook, an MCP server, a
    scheduled pass — because every path other than a genuine, settled staleness
    costs one tier-0 probe and returns.
    """
    verdict = check_freshness(
        root,
        state_root,
        quiet_window_seconds=quiet_window_seconds,
        now_ns=now_ns,
    )
    outcome = {"verdict": asdict(verdict), "refresh": None}
    if not verdict.stale:
        return outcome
    started = time.process_time()
    result = _refresh(root, state_root, refresh_budget_seconds)
    result["cpu_seconds"] = round(time.process_time() - started, 2)
    outcome["refresh"] = result
    return outcome


def _budget_stop(
    report: SessionReport, elapsed: float, wall_budget: float, interval: float
) -> str | None:
    """A bound the session would overshoot is not a bound, so the next probe counts."""
    if elapsed + interval >= wall_budget:
        return "wall_budget"
    if report.probe_cpu_seconds >= SESSION_PROBE_CPU_BUDGET_SECONDS:
        return "probe_cpu_budget"
    return None


def _record_refresh(report: SessionReport, outcome: dict) -> None:
    refresh = outcome["refresh"]
    if refresh is None:
        return
    report.refresh_cpu_seconds += float(refresh.get("cpu_seconds") or 0.0)
    report.refreshes.append(refresh)


def _session_step(
    report: SessionReport, root: Path, state_root: Path, options: dict
) -> None:
    outcome = refresh_if_quiet(
        root,
        state_root,
        quiet_window_seconds=options["quiet_window_seconds"],
        refresh_budget_seconds=options["refresh_budget_seconds"],
    )
    report.probes += 1
    report.probe_cpu_seconds += float(outcome["verdict"]["cost_seconds"])
    report.verdicts.append(outcome["verdict"])
    _record_refresh(report, outcome)


def _refresh_cap_reached(report: SessionReport, max_refreshes: int) -> bool:
    """Every started refresh counts, including a deferred one.

    A refusal is cheap, but the two that are not — `corpus_changed` and
    `time_limit` — are refusals that arrive after the builder has already spent
    most of its budget. Counting only successes would let a session that cannot
    win spend the whole afternoon losing.
    """
    return len(report.refreshes) >= max_refreshes


def run_watch_session(
    root: Path,
    state_root: Path,
    *,
    wall_budget_seconds: float = SESSION_WALL_BUDGET_SECONDS,
    probe_interval_seconds: float = PROBE_INTERVAL_SECONDS,
    quiet_window_seconds: float = QUIET_WINDOW_SECONDS,
    refresh_budget_seconds: float = REFRESH_BUDGET_SECONDS,
    max_refreshes: int = MAX_REFRESHES_PER_SESSION,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> SessionReport:
    """Take the bounded step on a timer until a bound is reached, then stop.

    There is no daemon here and there cannot be one: the loop is bounded by
    wall time, by the CPU its own probing has spent, and by how many refreshes
    it is allowed to start. It runs on the caller's thread and returns.
    """
    report = SessionReport()
    options = {
        "quiet_window_seconds": quiet_window_seconds,
        "refresh_budget_seconds": refresh_budget_seconds,
    }
    started = monotonic()
    while True:
        _session_step(report, root, state_root, options)
        if _refresh_cap_reached(report, max_refreshes):
            report.stopped_by = "refresh_cap"
            break
        stop = _budget_stop(
            report, monotonic() - started, wall_budget_seconds, probe_interval_seconds
        )
        if stop is not None:
            report.stopped_by = stop
            break
        sleep(probe_interval_seconds)
    report.wall_seconds = round(monotonic() - started, 2)
    return report


def _default_roots() -> tuple[Path, Path]:
    from memory_state import ROOT, STATE_ROOT

    return Path(ROOT), Path(STATE_ROOT)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--state-root", type=Path, default=None)
    parser.add_argument("--check", action="store_true", help="look only, never build")
    parser.add_argument("--session", action="store_true", help="bounded watch session")
    parser.add_argument("--quiet-window", type=float, default=QUIET_WINDOW_SECONDS)
    parser.add_argument("--refresh-budget", type=float, default=REFRESH_BUDGET_SECONDS)
    parser.add_argument(
        "--wall-budget", type=float, default=SESSION_WALL_BUDGET_SECONDS
    )
    parser.add_argument("--interval", type=float, default=PROBE_INTERVAL_SECONDS)
    parser.add_argument(
        "--max-refreshes", type=int, default=MAX_REFRESHES_PER_SESSION
    )
    return parser


def _resolved_roots(arguments) -> tuple[Path, Path]:
    default_root, default_state = _default_roots()
    root = arguments.root or default_root
    return Path(root), Path(arguments.state_root or default_state)


def _run_command(arguments) -> dict:
    root, state_root = _resolved_roots(arguments)
    if arguments.check:
        return asdict(
            check_freshness(
                root, state_root, quiet_window_seconds=arguments.quiet_window
            )
        )
    if arguments.session:
        return asdict(
            run_watch_session(
                root,
                state_root,
                wall_budget_seconds=arguments.wall_budget,
                probe_interval_seconds=arguments.interval,
                quiet_window_seconds=arguments.quiet_window,
                refresh_budget_seconds=arguments.refresh_budget,
                max_refreshes=arguments.max_refreshes,
            )
        )
    return refresh_if_quiet(
        root,
        state_root,
        quiet_window_seconds=arguments.quiet_window,
        refresh_budget_seconds=arguments.refresh_budget,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    print(json.dumps(_run_command(arguments), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
