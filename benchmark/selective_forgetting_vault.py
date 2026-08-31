"""Measure selective forgetting on a throwaway copy of this vault (MEM-12).

Memory Agent Bench scores four competencies; this stand covers the fourth,
*selective forgetting*, as a property rather than a claim: given material that
should be forgotten and material that must not be, does a query still surface
the forgotten item, does the retained item survive, and does the reverse
transition return exactly what it took away.

Run as a subprocess, one process per phase, because the product resolves
`LLM_WIKI_ROOT` / `LLM_WIKI_STATE_ROOT` at import time and a corpus generation
is process-global state. Every phase seeds a throwaway vault with *copies* of
real pages; the live vault is opened read-only and never archived.

The pipeline is the product's own, not a mock:

* `installed_memory_repair.repair_installed_vault` adopts the trial vault onto
  the Reliability V3 pair, so every move below is a real transaction;
* `longmemeval_vault.build_generation` builds and activates one immutable
  corpus generation with the same helpers `doctor` uses;
* `archive_stale` and `archive_sessions` do the forgetting;
* `retrieval.retrieve_via_search_memory` answers the probes.

Probes are deterministic on purpose: `semantic=False`, `rerank=False`. A probe
is a verbatim phrase lifted out of the target page itself, so a page that is
retrievable at all is retrieved by its own words. Ranking subtleties are not
what this stand measures — presence and absence are.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

BENCHMARK = Path(__file__).resolve().parent
REPO = BENCHMARK.parent

BUILD_DEADLINE_SECONDS = 900.0
PROBE_DEADLINE_SECONDS = 120.0
PROBE_LIMIT = 10
PROBE_WORDS = 12
PROBE_MIN_CHARS = 40

# Editorial vault metadata, not knowledge; the archiver skips these names too.
SKIP_NAMES = frozenset({"index.md", "log.md", "readme.md"})

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
FIELD_RE = "^{name}:\\s*(.+?)\\s*$"
SUPERSEDED_BY_RE = re.compile(r"^superseded_by:\s*(.+?)\s*$", re.MULTILINE)
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)")

# Days added on top of a type's own window when a page is aged for the stand.
AGEING_MARGIN_DAYS = 30

# How many aged pages are read once before archiving, to test the reprieve.
REPRIEVE_COHORT = 8


def field(frontmatter: str, name: str) -> str:
    """One frontmatter field, or the empty string when it is not declared."""
    match = re.search(FIELD_RE.format(name=name), frontmatter, re.MULTILINE)
    return match.group(1).strip().strip("\"'") if match else ""


def frontmatter_of(text: str) -> str:
    match = FRONTMATTER_RE.match(text)
    return match.group(1) if match else ""


def _body_of(text: str) -> str:
    match = FRONTMATTER_RE.match(text)
    return text[match.end():] if match else text


def _probe_candidate(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) < PROBE_MIN_CHARS:
        return False
    if stripped[0] in "#->|`*_[" or stripped.startswith("---"):
        return False
    return "http" not in stripped


def probe_phrase(text: str) -> str:
    """A verbatim phrase from the page: its longest plain body line, clipped.

    Deterministic by construction — longest first, then lexicographic — so the
    same page always yields the same probe and two runs are comparable.
    """
    lines = [line.strip() for line in _body_of(text).splitlines()]
    usable = sorted((line for line in lines if _probe_candidate(line)),
                    key=lambda line: (-len(line), line))
    if not usable:
        return ""
    return " ".join(usable[0].split()[:PROBE_WORDS])


def successor_slugs(text: str) -> list[str]:
    """The slugs a superseded page hands its readers, from `superseded_by`."""
    declared = SUPERSEDED_BY_RE.search(frontmatter_of(text))
    if declared is None:
        return []
    found = WIKILINK_RE.findall(declared.group(1))
    raw = found or [declared.group(1)]
    return [Path(item.strip()).name.removesuffix(".md") for item in raw if item.strip()]


def source_pages(source: Path) -> list[Path]:
    """Every real note worth copying, editorial metadata excluded."""
    return sorted(
        page for page in source.glob("*.md") if page.name.lower() not in SKIP_NAMES
    )


def prepare_environment(work: Path) -> tuple[Path, Path]:
    """Point the product at a throwaway vault before any scripts import."""
    root = work / "vault"
    state = work / "state"
    os.environ["LLM_WIKI_ROOT"] = str(root)
    os.environ["LLM_WIKI_STATE_ROOT"] = str(state)
    os.environ["MEMORY_LLM_PROVIDER"] = "fake"
    scripts = str(REPO / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts/integration_adapter.py").write_bytes(
        (REPO / "scripts/integration_adapter.py").read_bytes()
    )
    (root / "knowledge/notes").mkdir(parents=True, exist_ok=True)
    (root / "knowledge/daily").mkdir(parents=True, exist_ok=True)
    os.chdir(root)
    return root, state


def seed(root: Path, source: Path) -> list[Path]:
    """Copy the real pages into the trial vault; return the copies."""
    destination = root / "knowledge/notes"
    copies = []
    for page in source_pages(source):
        target = destination / page.name
        shutil.copyfile(page, target)
        copies.append(target)
    return copies


def adopt(root: Path, state: Path) -> None:
    from installed_memory_repair import repair_installed_vault

    report = repair_installed_vault(
        root=root,
        state_root=state,
        adopt_ownership_v3=True,
        confirm_all_agents_stopped=True,
    )
    if report["overall_status"] != "ok":
        raise RuntimeError(f"vault adoption failed: {report['overall_status']}")


def _active_id(catalog: object) -> str | None:
    active = catalog.get_active() or {}
    identifier = active.get("generation_id")
    return str(identifier) if identifier else None


def build_generation(root: Path, state: Path) -> dict:
    """One immutable corpus generation over the pages the vault holds now.

    `longmemeval_vault.build_generation` activates against `expected_active=None`
    — right for a vault that has never published one, wrong here: this stand
    builds a second generation over the same catalog after archiving, and the
    compare-and-swap correctly refuses to advance a pointer it was told to find
    empty (measured: `generation build finished but did not activate`). The
    build configuration is that stand's, imported rather than restated, so both
    stands index the same way.
    """
    from corpus_snapshot import collect_corpus
    from doctor import (
        _corpus_policy,
        _fresh_generation_id,
        _generation_source_bytes,
        _generation_source_extractor,
        _generation_source_rows,
    )
    from evidence_graph_builder import build_incremental_generation
    from generation_catalog import GenerationCatalog
    from longmemeval_vault import _reuse_config
    from repository_scope import resolve_repository_scope

    deadline = time.monotonic() + BUILD_DEADLINE_SECONDS
    snapshot = collect_corpus(root, code_roots=(), daily_paths=[], deadline=deadline)
    scope = resolve_repository_scope(root)
    catalog = GenerationCatalog(state)
    built = build_incremental_generation(
        catalog,
        sources=_generation_source_rows(snapshot),
        source_bytes=_generation_source_bytes(snapshot),
        extractor=_generation_source_extractor(snapshot, scope.repository_id),
        reuse_config=_reuse_config(snapshot),
        generation_id=_fresh_generation_id(catalog),
        parent_generation_id=_active_id(catalog),
        policy=_corpus_policy(snapshot),
        expected_active=_active_id(catalog),
        deadline=deadline,
        cancelled=None,
        repository_scope=scope,
        snapshot=snapshot,
        publication_root=root,
        coordinator=None,
    )
    if not built.activated:
        raise RuntimeError("generation build finished but did not activate")
    return _generation_info(built, catalog, snapshot)


def _generation_info(built: object, catalog: object, snapshot: object) -> dict:
    active = catalog.get_active() or {}
    return {
        "generation_id": built.generation_id,
        "vector_state": active.get("vector_state"),
        "sources": len(snapshot.sources),
        "chunks": len(snapshot.chunks),
    }


def corpus_paths(root: Path) -> set[str]:
    """Every source the collector admits — the authoritative membership answer."""
    from corpus_snapshot import collect_corpus

    snapshot = collect_corpus(
        root, code_roots=(), daily_paths=[], deadline=time.monotonic() + 300.0
    )
    return {source.record.relative_path for source in snapshot.sources}


def probe(query: str) -> list[str]:
    """The paths one deterministic lexical query returns, best first."""
    from retrieval import retrieve_via_search_memory

    rows = retrieve_via_search_memory(
        query,
        limit=PROBE_LIMIT,
        semantic=False,
        rerank=False,
        graph=False,
        deadline_monotonic=time.monotonic() + PROBE_DEADLINE_SECONDS,
    )
    return [str(row.get("path") or "") for row in rows]


def _rank_of(slug: str, paths: list[str]) -> int | None:
    for index, path in enumerate(paths):
        if Path(path).name == f"{slug}.md":
            return index + 1
    return None


def probe_result(slug: str, query: str) -> dict:
    """One probe: the phrase asked, and where its own page landed."""
    if not query:
        return {"slug": slug, "query": "", "rank": None, "surfaced": False,
                "probe_usable": False}
    paths = probe(query)
    rank = _rank_of(slug, paths)
    return {
        "slug": slug,
        "query": query,
        "rank": rank,
        "surfaced": rank is not None,
        "probe_usable": True,
        "top": paths[:3],
    }


# --------------------------------------------------------------------------
# phase: supersession (live pages, real `status: superseded`)
# --------------------------------------------------------------------------


def _force_active(page: Path) -> None:
    text = page.read_text(encoding="utf-8")
    page.write_text(
        re.sub(r"^status:\s*superseded\s*$", "status: accepted", text,
               count=1, flags=re.MULTILINE),
        encoding="utf-8",
    )


def _superseded_pages(pages: list[Path]) -> list[Path]:
    return [
        page for page in pages
        if field(frontmatter_of(page.read_text(encoding="utf-8")), "status") == "superseded"
    ]


def _successor_targets(pages: list[Path]) -> list[str]:
    slugs: list[str] = []
    for page in pages:
        slugs.extend(successor_slugs(page.read_text(encoding="utf-8")))
    return sorted(set(slugs))


def _probe_of(root: Path, slug: str) -> dict:
    page = root / "knowledge/notes" / f"{slug}.md"
    if not page.is_file():
        return {"slug": slug, "query": "", "rank": None, "surfaced": False,
                "probe_usable": False, "missing_page": True}
    return probe_result(slug, probe_phrase(page.read_text(encoding="utf-8")))


def phase_supersession(root: Path, state: Path, source: Path, keep_active: bool) -> dict:
    """Are superseded pages forgotten, and do their successors survive?"""
    pages = seed(root, source)
    retired = _superseded_pages(pages)
    targets = [page.stem for page in retired]
    successors = _successor_targets(retired)
    arm = "control_forced_active" if keep_active else "treatment_as_written"
    _apply_arm(retired, keep_active)
    adopt(root, state)
    generation = build_generation(root, state)
    collected = corpus_paths(root)
    return {
        "phase": "supersession",
        "arm": arm,
        "seeded_pages": len(pages),
        "generation": generation,
        "corpus_sources": len(collected),
        "forget_targets": [_membership(root, slug, collected) for slug in targets],
        "retain_targets": [_membership(root, slug, collected) for slug in successors],
    }


def _apply_arm(retired: list[Path], keep_active: bool) -> None:
    if not keep_active:
        return
    for page in retired:
        _force_active(page)


def _membership(root: Path, slug: str, collected: set[str]) -> dict:
    relative = f"knowledge/notes/{slug}.md"
    result = _probe_of(root, slug)
    result["in_corpus"] = relative in collected
    result["on_disk"] = (root / relative).is_file()
    return result


# --------------------------------------------------------------------------
# phase: ageing (live pages, synthetic mtimes)
# --------------------------------------------------------------------------


def _window_days(page_type: str) -> int:
    from okf_types import DEFAULT_AGE_DAYS, TYPE_AGE_DAYS

    return TYPE_AGE_DAYS.get(page_type, DEFAULT_AGE_DAYS)


def _archivable(page: Path) -> bool:
    from okf_types import NEVER_ARCHIVE_TYPES
    from page_status import is_retired

    frontmatter = frontmatter_of(page.read_text(encoding="utf-8"))
    if is_retired(field(frontmatter, "status")):
        return False
    return field(frontmatter, "type") not in NEVER_ARCHIVE_TYPES


def age_past_window(page: Path) -> int:
    """Push one page's mtime past its own type window; return the age in days."""
    page_type = field(frontmatter_of(page.read_text(encoding="utf-8")), "type")
    days = _window_days(page_type) + AGEING_MARGIN_DAYS
    stamp = time.time() - days * 86400
    os.utime(page, (stamp, stamp))
    return days


def _archive_stale_pages() -> list[str]:
    import archive_stale

    cutoff = time.time() - 180 * 86400
    stale = archive_stale._stale_pages(cutoff, 180)
    return [archive_stale._archive_page(page, True) for page in stale]


def _retained(page: Path) -> bool:
    """A page that must survive: an evergreen type still in force."""
    from page_status import is_retired

    frontmatter = frontmatter_of(page.read_text(encoding="utf-8"))
    return not _archivable(page) and not is_retired(field(frontmatter, "status"))


def _aged_and_kept(pages: list[Path]) -> tuple[list[Path], list[Path]]:
    aged = [page for page in pages if _archivable(page)]
    kept = [page for page in pages if _retained(page)]
    _age_all(aged)
    return aged, kept


def _age_all(pages: list[Path]) -> None:
    for page in pages:
        age_past_window(page)


def _queries(pages: list[Path], sample: int) -> dict[str, str]:
    """One probe per page, taken while the page is still where it lives.

    Taken once and reused after archiving on purpose: reading the probe out of
    the page again would ask nothing at all once the page has moved, and a
    probe that cannot be asked is not a measurement of forgetting. Measured on
    the first run of this phase — the after-round reported 0 of 37 probes
    usable, which is file absence, not retrieval.
    """
    return {page.stem: probe_phrase(page.read_text(encoding="utf-8")) for page in pages[:sample]}


def _record_reads(pages: list[Path]) -> int:
    """Consult each page once, which is what buys it a reprieve from its window."""
    from access_tracking import record_access

    for page in pages:
        record_access(page.stem, source="benchmark")
    return len(pages)


def _cohorts(aged: list[Path]) -> tuple[list[Path], list[Path]]:
    """Archivable pages split into the read ones and the untouched ones."""
    return aged[:REPRIEVE_COHORT], aged[REPRIEVE_COHORT:]


def phase_ageing(root: Path, state: Path, source: Path, sample: int) -> dict:
    """Age every archivable page past its window, archive, and re-ask.

    Three cohorts, because the policy has three answers: an aged page nobody
    read must go, an aged page that was read must stay (access reinforces), and
    an evergreen type must never be considered at all.
    """
    pages = seed(root, source)
    aged, kept = _aged_and_kept(pages)
    reprieved, forgettable = _cohorts(aged)
    reads = _record_reads(reprieved)
    queries = {
        "forget": _queries(forgettable, sample),
        "reprieve": _queries(reprieved, sample),
        "retain": _queries(kept, sample),
    }
    adopt(root, state)
    before_generation = build_generation(root, state)
    before = _probe_round(root, queries)
    outcomes = _archive_stale_pages()
    after_generation = build_generation(root, state)
    after = _probe_round(root, queries)
    return {
        "phase": "ageing",
        "seeded_pages": len(pages),
        "archivable_pages": len(aged),
        "never_archive_pages": len(kept),
        "reprieve_reads": reads,
        "ageing_margin_days": AGEING_MARGIN_DAYS,
        "archive_outcomes": _outcome_counts(outcomes),
        "generation_before": before_generation,
        "generation_after": after_generation,
        "before": before,
        "after": after,
        "still_on_disk": _archive_disk_state(root),
    }


def _asked(root: Path, queries: dict[str, str], collected: set[str]) -> list[dict]:
    return [_one_ask(root, slug, query, collected) for slug, query in sorted(queries.items())]


def _one_ask(root: Path, slug: str, query: str, collected: set[str]) -> dict:
    relative = f"knowledge/notes/{slug}.md"
    result = probe_result(slug, query)
    result["in_corpus"] = relative in collected
    result["in_active_tree"] = (root / relative).is_file()
    return result


def _probe_round(root: Path, queries: dict[str, dict[str, str]]) -> dict:
    collected = corpus_paths(root)
    round_result = {"corpus_sources": len(collected)}
    for cohort, asked in queries.items():
        round_result[f"{cohort}_targets"] = _asked(root, asked, collected)
    return round_result


def _outcome_counts(outcomes: list[str]) -> dict:
    counts: dict[str, int] = {}
    for line in outcomes:
        key = line.split(":", 1)[0]
        counts[key] = counts.get(key, 0) + 1
    return counts


def _archive_disk_state(root: Path) -> dict:
    archive = root / "knowledge/notes/archive"
    files = sorted(archive.rglob("*.md")) if archive.is_dir() else []
    return {
        "archived_files": len(files),
        "archive_root": "knowledge/notes/archive",
        "bytes_retained": sum(item.stat().st_size for item in files),
    }


# --------------------------------------------------------------------------
# phase: restore (byte fidelity of the reverse transition)
# --------------------------------------------------------------------------


def _restore_one(root: Path, slug: str, original: bytes) -> dict:
    import archive_stale

    outcome = archive_stale.restore_page(slug, apply=True)
    page = root / "knowledge/notes" / f"{slug}.md"
    restored = page.read_bytes() if page.is_file() else b""
    return {
        "slug": slug,
        "outcome": outcome.split(":", 1)[0],
        "restored": page.is_file(),
        "identical": restored == original,
        "original_bytes": len(original),
        "restored_bytes": len(restored),
    }


def phase_restore(root: Path, state: Path, source: Path) -> dict:
    """Archive every archivable page, restore it, compare against the original."""
    pages = seed(root, source)
    aged, _ = _aged_and_kept(pages)
    originals = {page.stem: page.read_bytes() for page in aged}
    adopt(root, state)
    archived = _outcome_counts(_archive_stale_pages())
    results = [_restore_one(root, slug, body) for slug, body in sorted(originals.items())]
    return {
        "phase": "restore",
        "seeded_pages": len(pages),
        "archived": archived,
        "restored_pages": len(results),
        "byte_identical": sum(1 for item in results if item["identical"]),
        "results": results,
        "synthetic": _synthetic_restore_cases(root),
    }


_SYNTHETIC_CASES = (
    (
        "declared-status-page",
        "---\ntype: debugging\nstatus: preliminary\n---\n\nA page whose own "
        "status word is not the default one.\n",
    ),
    (
        "body-mentions-status-page",
        "A page with no frontmatter whose body writes status: somewhere.\n",
    ),
)


def _synthetic_case(root: Path, slug: str, body: str) -> dict:
    import archive_stale

    page = root / "knowledge/notes" / f"{slug}.md"
    page.write_text(body, encoding="utf-8")
    age_past_window(page)
    archive_outcome = archive_stale._archive_page(page, True)
    archived = _archived_copy(root, slug)
    result = _restore_one(root, slug, body.encode("utf-8"))
    result["archive_outcome"] = archive_outcome.split(":", 1)[0]
    result["archived_declares_retired"] = _declares_retired(archived)
    return result


def _archived_copy(root: Path, slug: str) -> bytes:
    archive = root / "knowledge/notes/archive"
    found = sorted(archive.rglob(f"{slug}.md")) if archive.is_dir() else []
    return found[0].read_bytes() if found else b""


def _declares_retired(content: bytes) -> bool:
    from page_status import is_retired

    text = content.decode("utf-8", errors="ignore")
    return is_retired(field(frontmatter_of(text), "status"))


def _synthetic_restore_cases(root: Path) -> list[dict]:
    """Two shapes the live vault does not currently hold; labelled synthetic."""
    return [_synthetic_case(root, slug, body) for slug, body in _SYNTHETIC_CASES]


# --------------------------------------------------------------------------
# phase: sessions (the second forgetting mechanism, on raw session records)
# --------------------------------------------------------------------------


SESSION_BODY = "# Session record\n\nA verbatim line the retention window will move one directory deeper.\n"


def _write_session(root: Path, day: str, name: str) -> Path:
    record = root / "knowledge/raw/sessions" / day / f"{name}.md"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(SESSION_BODY, encoding="utf-8")
    return record


def _session_days(root: Path, days: int) -> tuple[str, str]:
    from datetime import date, timedelta

    today = date.today()
    return (today - timedelta(days=days + 10)).isoformat(), today.isoformat()


def phase_sessions(root: Path, state: Path, source: Path, days: int) -> dict:
    """Session records age out of the hot tree — but were they ever retrievable?"""
    seed(root, source)
    old_day, new_day = _session_days(root, days)
    old = _write_session(root, old_day, "aged-out-session")
    fresh = _write_session(root, new_day, "recent-session")
    original = old.read_bytes()
    adopt(root, state)
    collected = corpus_paths(root)
    outcomes = _session_outcomes(days)
    return {
        "phase": "sessions",
        "retention_days": days,
        "records_written": 2,
        "records_in_corpus": len([
            item for item in (old, fresh)
            if item.relative_to(root).as_posix() in collected
        ]),
        "corpus_sources": len(collected),
        "outcomes": _outcome_counts(outcomes),
        "aged_record_moved": not old.is_file(),
        "recent_record_kept": fresh.is_file(),
        "archived_bytes_identical": _archived_session_bytes(root, old_day) == original,
        "restore_command_exists": _sessions_have_restore(),
    }


def _session_outcomes(days: int) -> list[str]:
    from datetime import date

    import archive_sessions

    return archive_sessions.archive_sessions(days=days, today=date.today(), apply=True)


def _archived_session_bytes(root: Path, day: str) -> bytes:
    archived = root / "knowledge/raw/sessions/archive" / day[:7] / day / "aged-out-session.md"
    return archived.read_bytes() if archived.is_file() else b""


def _sessions_have_restore() -> bool:
    """Whether the session archiver offers the reverse transition pages have."""
    import archive_sessions

    return hasattr(archive_sessions, "restore_session")


# --------------------------------------------------------------------------
# phase: legacy (the index used when no generation is usable)
# --------------------------------------------------------------------------


def _legacy_pages() -> set[str]:
    """Every page the legacy lexical walker admits, vault-relative."""
    import search_memory
    from memory_state import ROOT as VAULT

    return {
        page.resolve().relative_to(VAULT.resolve()).as_posix()
        for page in search_memory._collect_pages("all")
    }


def _archived_relatives(root: Path) -> list[str]:
    archive = root / "knowledge/notes/archive"
    files = sorted(archive.rglob("*.md")) if archive.is_dir() else []
    return [item.relative_to(root).as_posix() for item in files]


def _legacy_probe(root: Path, relative: str) -> dict:
    page = root / relative
    result = probe_result(Path(relative).stem, probe_phrase(page.read_text(encoding="utf-8")))
    result["path"] = relative
    return result


def phase_legacy(root: Path, state: Path, source: Path, sample: int) -> dict:
    """With no corpus generation, does the archive still answer a query?"""
    pages = seed(root, source)
    aged, kept = _aged_and_kept(pages)
    adopt(root, state)
    archived = _outcome_counts(_archive_stale_pages())
    synthetic = [_archive_synthetic(root, slug, body) for slug, body in _SYNTHETIC_CASES]
    relatives = _archived_relatives(root)
    collected = _legacy_pages()
    leaked = [item for item in relatives if item in collected]
    return {
        "phase": "legacy",
        "seeded_pages": len(pages),
        "archivable_pages": len(aged),
        "never_archive_pages": len(kept),
        "archived": archived,
        "synthetic": synthetic,
        "archived_files": len(relatives),
        "legacy_collected_pages": len(collected),
        "archived_still_collected": leaked,
        "leaked_probes": [_legacy_probe(root, item) for item in leaked[:sample]],
    }


def _archive_synthetic(root: Path, slug: str, body: str) -> dict:
    import archive_stale

    page = root / "knowledge/notes" / f"{slug}.md"
    page.write_text(body, encoding="utf-8")
    age_past_window(page)
    outcome = archive_stale._archive_page(page, True)
    return {
        "slug": slug,
        "archive_outcome": outcome.split(":", 1)[0],
        "archived_declares_retired": _declares_retired(_archived_copy(root, slug)),
    }


def _run_legacy(root: Path, state: Path, args: argparse.Namespace) -> dict:
    return phase_legacy(root, state, args.pages, args.sample)


def _run_sessions(root: Path, state: Path, args: argparse.Namespace) -> dict:
    return phase_sessions(root, state, args.pages, args.retention_days)


def _run_supersession(root: Path, state: Path, args: argparse.Namespace) -> dict:
    return phase_supersession(root, state, args.pages, args.keep_active)


def _run_ageing(root: Path, state: Path, args: argparse.Namespace) -> dict:
    return phase_ageing(root, state, args.pages, args.sample)


def _run_restore(root: Path, state: Path, args: argparse.Namespace) -> dict:
    return phase_restore(root, state, args.pages)


PHASES = {
    "supersession": _run_supersession,
    "ageing": _run_ageing,
    "restore": _run_restore,
    "legacy": _run_legacy,
    "sessions": _run_sessions,
}


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--phase", choices=sorted(PHASES), required=True)
    parser.add_argument("--work", type=Path, required=True, help="throwaway directory")
    parser.add_argument("--pages", type=Path, required=True, help="notes to copy in")
    parser.add_argument("--sample", type=int, default=200, help="probes per cohort")
    parser.add_argument("--retention-days", type=int, default=90,
                        help="session retention window used by the sessions phase")
    parser.add_argument("--keep-active", action="store_true",
                        help="supersession control arm: force the retired pages active")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pages = args.pages.resolve()
    root, state = prepare_environment(args.work.resolve())
    started = time.monotonic()
    report = PHASES[args.phase](root, state, argparse.Namespace(**{**vars(args), "pages": pages}))
    report["seconds"] = round(time.monotonic() - started, 2)
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
