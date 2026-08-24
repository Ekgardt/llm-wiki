"""Structural + semantic lint across the vault.

Covers the `knowledge/notes/` tree (filename kept for backward compat with
hooks and docs that still reference `lint_memory`).

Fourteen checks (Phase 2 expanded the original seven + Phase 6 temporal + invalid-type-value):
 1. broken_wikilinks — wikilinks whose target does not resolve to a file.
 2. orphan_pages — knowledge/wiki pages not referenced by the relevant index.md.
 3. orphan_daily_logs — daily logs with no compile recorded in state.json.
 4. stale_compiled — daily log hash changed after last compile.
 5. missing_backlinks — page A links to page B, but B does not link back.
 6. sparse_pages — pages under a word-count floor (default 200 words).
 7. contradictions — LLM-judged conflicts between pages (opt-in, --contradictions).
 8. missing_frontmatter — page has no YAML `---` block (OKF violation).
 9. missing_required_type — frontmatter exists but `type:` is absent/empty.
10. invalid_type_value — `type:` value is not in the canonical OKF type set.
11. missing_sources_section — claim-bearing page lacks `## Source` / `sources:`.
12. invalid_supersede_chain — `superseded_by:` points to a non-existent page.
13. orphan_gaps — page in `knowledge/notes/` has no inbound link from outside gaps/.
14. temporal_validity — `valid_to:` is in the past but `status:` is still active.

Usage:
    uv run python scripts/lint_memory.py                  # all scopes, structural only
    uv run python scripts/lint_memory.py --scope memory   # knowledge/notes/ only (legacy label)
    uv run python scripts/lint_memory.py --scope wiki     # knowledge/notes/ only
    uv run python scripts/lint_memory.py --contradictions # also run the LLM check
    uv run python scripts/lint_memory.py --sparse-words 300

Writes a report to `$LLM_WIKI_STATE_ROOT/logs/lint-YYYY-MM-DD.md`
(default: ``$LLM_WIKI_ROOT/logs/`` — inside the vault, gitignored) and prints a summary.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bounded_io import read_stable_bytes  # noqa: E402
from claims import (  # noqa: E402
    CANDIDATE_SCHEMA,
    MAX_CLAIM_PAGE_BYTES,
    parse_claim_ledger,
    validate_claim_record,
)
from evidence_resolver import (  # noqa: E402
    EvidenceResolutionError,
    EvidenceResolver,
    extract_evidence_references,
)
from memory_state import REPORTS_DIR, ROOT, STATE_ROOT, file_hash, load_state  # noqa: E402
from okf_types import CANONICAL_TYPES as VALID_TYPES  # noqa: E402
from okf_types import (
    INBOX_TYPES,  # noqa: E402
    TYPE_ALIASES,  # noqa: E402
)
from page_status import is_retired  # noqa: E402
from reliable_memory import canonical_json_bytes, validate_schema  # noqa: E402
from vault_editorial import (  # noqa: E402
    BACKLINK_EXEMPT_NAMES,
    BROKEN_LINK_SKIP_NAMES,
    EDITORIAL_NAMES,
)

# Three-zone layout: one knowledge tree, notes under it, one vault index.
VAULT = ROOT / "knowledge"
NOTES = VAULT / "notes"
DAILY_DIR = VAULT / "daily"
VAULT_INDEX = VAULT / "index.md"

REPORTS = REPORTS_DIR
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]")
WORD_RE = re.compile(r"\b\w+\b")

DEFAULT_SPARSE_WORDS = 200
MAX_LINT_PAGE_BYTES = 4 * 1024 * 1024

# Editorial page sets (EDITORIAL_NAMES, BACKLINK_EXEMPT_NAMES,
# BROKEN_LINK_SKIP_NAMES) come from `vault_editorial` — shared with
# `lookup_mode.py` so the two stay in sync.


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--scope", choices=["memory", "wiki", "all"], default="all")
    p.add_argument("--contradictions", action="store_true",
                   help="Run the LLM-based contradiction check (7th check; costs API calls).")
    p.add_argument("--sparse-words", type=int, default=DEFAULT_SPARSE_WORDS,
                   help=f"Minimum word count per page (default {DEFAULT_SPARSE_WORDS}).")
    p.add_argument("--structural-only", action="store_true",
                   help="Alias: disables --contradictions.")
    p.add_argument(
        "--fail-on-findings",
        action="store_true",
        help=(
            "Exit non-zero (1) when any structural finding is detected, "
            "instead of the default always-zero exit. Intended for CI: "
            "new broken wikilinks / orphan pages / missing backlinks / "
            "sparse pages / contradictions fail the build. "
            "`orphan_daily_logs` is exempt (self-resolves on next compile)."
        ),
    )
    p.add_argument(
        "--allowed-categories",
        nargs="*",
        default=["orphan_daily_logs"],
        help=(
            "Finding categories that do NOT trigger --fail-on-findings. "
            "Default: orphan_daily_logs (transient; next compile pass clears them)."
        ),
    )
    return p.parse_args()


# ---------- tree helpers ----------

def _iter_tree_md(tree: Path) -> list[Path]:
    if not tree.exists():
        return []
    return sorted(p for p in tree.rglob("*.md") if p.is_file())


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def _is_body_line(line: str) -> bool:
    """Content lines only: no blanks, no frontmatter fence, no headers."""
    stripped = line.strip()
    if not stripped or stripped.startswith("---"):
        return False
    return not line.lstrip().startswith("#")


def _word_count(md: Path) -> int:
    text = md.read_text(encoding="utf-8", errors="ignore")
    body = "\n".join(line for line in text.splitlines() if _is_body_line(line))
    return len(WORD_RE.findall(body))


def _extract_links(md: Path) -> list[str]:
    text = md.read_text(encoding="utf-8", errors="ignore")
    return [m.group(1) for m in WIKILINK_RE.finditer(text)]


def _resolve_path_style_link(target: str) -> Path | None:
    """A target with a slash is anchored at the vault root, with or without .md."""
    for candidate in ((ROOT / (target + ".md")).resolve(), (ROOT / target).resolve()):
        if candidate.is_file():
            return candidate
    return None


def _resolve_bare_link(target: str, search_roots: list[Path]) -> Path | None:
    for root in search_roots:
        for page in root.rglob(f"{target}.md"):
            return page
    return None


def _resolve_link(target: str, search_roots: list[Path]) -> Path | None:
    stripped = target.strip()
    if not stripped:
        return None
    if "/" in stripped:
        return _resolve_path_style_link(stripped)
    return _resolve_bare_link(stripped, search_roots)


def check_evidence_references(pages: list[Path]) -> list[str]:
    """Resolve every canonical logical evidence reference and fail closed."""
    resolver = EvidenceResolver(ROOT, state_root=STATE_ROOT)
    findings: list[str] = []
    for page in pages:
        try:
            text = read_stable_bytes(
                page, MAX_LINT_PAGE_BYTES, label="lint evidence page"
            ).decode("utf-8", errors="strict")
            references = extract_evidence_references(
                CLAIMS_SECTION_RE.sub("", text)
            )
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            findings.append(f"{_rel(page)}: evidence scan failed: {exc}")
            continue
        for reference in references:
            try:
                resolver.resolve(reference)
            except (EvidenceResolutionError, OSError, ValueError) as exc:
                findings.append(f"{_rel(page)}: {reference}: {exc}")
    return findings


# ---------- individual checks ----------

def _git_tracked_paths() -> set[str] | None:
    """Return repo-relative posix paths of git-tracked files, or None if unavailable.

    Used so CI (clean checkout) and local worktrees agree: a wikilink that only
    resolves to a gitignored personal file must count as broken.
    """
    try:
        out = subprocess.check_output(
            ["git", "ls-files", "-z"],
            cwd=str(ROOT),
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    if not out:
        return set()
    paths: set[str] = set()
    for raw in out.split(b"\0"):
        if not raw:
            continue
        paths.add(Path(raw.decode("utf-8", errors="replace")).as_posix())
    return paths


def _vault_relative(path: Path) -> str | None:
    """Path inside the vault, or None when it points outside."""
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return None


def _scannable_page(md: Path, tracked: set[str] | None) -> bool:
    """Daily logs quote wikilinks as prose, and untracked pages are personal.

    Both are skipped as link sources; links *to* an untracked page still fail
    below, because a clean checkout would not have that page.
    """
    if DAILY_DIR in md.parents or md.name in BROKEN_LINK_SKIP_NAMES:
        return False
    if tracked is None:
        return True
    relative = _vault_relative(md)
    return relative is not None and relative in tracked


def _is_placeholder_target(target: str) -> bool:
    """Templates and prose examples are not links to resolve."""
    stripped = target.strip()
    if stripped in ("...", "wikilinks"):
        return True
    return "<" in stripped or ">" in stripped


def _tracked_target_finding(
    md: Path, target: str, resolved: Path, tracked: set[str]
) -> str | None:
    """A link a clean checkout could not follow is broken, even if it resolves here."""
    relative = _vault_relative(resolved)
    if relative is None:
        return f"{_rel(md)} -> [[{target}]] (outside vault)"
    if relative not in tracked:
        return f"{_rel(md)} -> [[{target}]] (untracked/gitignored target)"
    return None


def _link_finding(
    md: Path, target: str, search_roots: list[Path], tracked: set[str] | None
) -> str | None:
    if _is_placeholder_target(target):
        return None
    resolved = _resolve_link(target, search_roots)
    if resolved is None:
        return f"{_rel(md)} -> [[{target}]]"
    if tracked is None:
        return None
    return _tracked_target_finding(md, target, resolved, tracked)


def _page_link_findings(
    md: Path, search_roots: list[Path], tracked: set[str] | None
) -> list[str]:
    out: list[str] = []
    for target in _extract_links(md):
        finding = _link_finding(md, target, search_roots, tracked)
        if finding is not None:
            out.append(finding)
    return out


def check_broken_links(pages: list[Path], search_roots: list[Path]) -> list[str]:
    tracked = _git_tracked_paths()
    out: list[str] = []
    for md in pages:
        if _scannable_page(md, tracked):
            out.extend(_page_link_findings(md, search_roots, tracked))
    return out


def _indexed(md: Path, index_text: str) -> bool:
    """The index may cite a page by stem or by full relative path."""
    relative = md.relative_to(ROOT).with_suffix("").as_posix()
    return md.stem in index_text or relative in index_text


STATUS_FIELD_RE = re.compile(r"^status:\s*(.+?)\s*$", re.MULTILINE)


def _is_retired(md: Path) -> bool:
    """Retired pages stay in git and out of the navigation map, by design.

    `rebuild_memory_index` removes superseded and archived pages from
    `index.md`, so requiring them to appear there would make the two rules
    unsatisfiable at once.
    """
    frontmatter = _frontmatter_of(md)
    if frontmatter is None:
        return False
    match = STATUS_FIELD_RE.search(frontmatter)
    if match is None:
        return False
    return is_retired(match.group(1))


def _is_published(md: Path) -> bool:
    """Whether this repository publishes the page, by the same rule the index uses."""
    from rebuild_memory_index import published_paths

    named, _hidden = published_paths(ROOT, [_rel(md)])
    return bool(named)


def _expects_an_index_entry(md: Path) -> bool:
    if md.name in EDITORIAL_NAMES:
        return False
    if not _is_published(md):
        # `knowledge/index.md` is tracked and names only published pages, so
        # demanding a private page's presence there demands a leak. The first
        # successful compile of this vault raised exactly that on the page it
        # had just written.
        return False
    return not _is_retired(md)


def check_orphans_against_index(pages: list[Path], index: Path) -> list[str]:
    if not index.exists():
        return []
    index_text = index.read_text(encoding="utf-8", errors="ignore")
    return [
        _rel(md)
        for md in pages
        if _expects_an_index_entry(md) and not _indexed(md, index_text)
    ]


DAILY_LOG_NAME_RE = re.compile(r"\d{4}-\d{2}-\d{2}\.md")


def _daily_logs() -> list[Path]:
    """Only `YYYY-MM-DD.md` is a daily log; `README.md` next to them is not."""
    if not DAILY_DIR.exists():
        return []
    return sorted(
        path
        for path in DAILY_DIR.glob("*.md")
        if DAILY_LOG_NAME_RE.fullmatch(path.name) is not None
    )


def check_orphan_daily_logs(state: dict) -> list[str]:
    compiled = state.get("compiled_daily_hashes", {})
    return [_rel(path) for path in _daily_logs() if path.name not in compiled]


def check_stale_compiled(state: dict) -> list[str]:
    compiled = state.get("compiled_daily_hashes", {})
    out: list[str] = []
    for path in _daily_logs():
        recorded = compiled.get(path.name)
        if recorded and recorded != file_hash(path):
            out.append(_rel(path))
    return out


def _is_backlink_exempt(md: Path) -> bool:
    return md.name in EDITORIAL_NAMES or md.name in BACKLINK_EXEMPT_NAMES


def _resolved_page_links(
    md: Path, page_set: set[Path], search_roots: list[Path]
) -> list[Path]:
    """Links from one page that land on another page in the same set."""
    resolved: list[Path] = []
    for target in _extract_links(md):
        landed = _resolve_link(target, search_roots)
        if landed is not None and landed in page_set:
            resolved.append(landed)
    return resolved


def _owed_pairs_from(source: Path, targets: list[Path]) -> list[tuple[Path, Path]]:
    return [(source, target) for target in targets if _pair_owes_backlink(source, target)]


def _backlink_pairs(link_map: dict[Path, list[Path]]) -> list[tuple[Path, Path]]:
    """Every ordered pair that owes a backlink, each pair once."""
    owed: list[tuple[Path, Path]] = []
    for source, targets in link_map.items():
        owed.extend(_owed_pairs_from(source, targets))
    return list(dict.fromkeys(owed))


def _pair_owes_backlink(source: Path, target: Path) -> bool:
    if source == target or _is_backlink_exempt(source):
        return False
    return not _is_backlink_exempt(target)


def missing_backlink_pairs(
    pages: list[Path], search_roots: list[Path]
) -> list[tuple[Path, Path]]:
    """Every (source, target) where the target still owes a link back."""
    page_set = set(pages)
    link_map = {md: _resolved_page_links(md, page_set, search_roots) for md in pages}
    return [
        (source, target)
        for source, target in _backlink_pairs(link_map)
        if source not in link_map.get(target, [])
        and _backlink_is_publishable(source, target)
    ]


def check_missing_backlinks(pages: list[Path], search_roots: list[Path]) -> list[str]:
    """Within a set of pages, A->B must be matched by B->A."""
    return [
        f"{_rel(source)} -> {_rel(target)} (no backlink)"
        for source, target in missing_backlink_pairs(pages, search_roots)
    ]


def _backlink_is_publishable(source: Path, target: Path) -> bool:
    """The obligation sits on the target, so the target must be able to carry it.

    A published page naming a private one would put a private slug into a
    tracked file, which is the leak the whole publication boundary exists to
    prevent. The reverse direction is already reported as a broken link.
    """
    if _is_published(source):
        return True
    return not _is_published(target)


def check_sparse_pages(pages: list[Path], min_words: int) -> list[str]:
    out: list[str] = []
    for md in pages:
        if md.name in EDITORIAL_NAMES:
            continue
        wc = _word_count(md)
        if wc < min_words:
            out.append(f"{_rel(md)} ({wc} words < {min_words})")
    return out


# ---------- OKF conformance checks (Phase 2) ----------
#
# Six new structural checks added when the vault migrated to OKF
# (Open Knowledge Format v0.1). These catch:
#   8. missing_frontmatter       — page has no `---` YAML block at all
#   9. missing_required_type     — frontmatter exists but `type:` is absent/empty
#  10. invalid_type_value        — `type:` value is not in the canonical OKF type set
#  11. missing_sources_section   — claim-bearing page lacks `## Source` or `sources:` frontmatter
#  12. invalid_supersede_chain   — `superseded_by:` points to a non-existent target
#  13. orphan_gaps               — page in knowledge/notes/ has no inbound link from a concept


FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
TYPE_FIELD_RE = re.compile(r"^type:\s*(.+?)\s*$", re.MULTILINE)
SUPERSEDED_BY_RE = re.compile(r"^superseded_by:\s*\[?\[?([^\]\n]+?)\]?\]?\s*$", re.MULTILINE)
SOURCES_FIELD_RE = re.compile(r"^sources:", re.MULTILINE)
SOURCE_SECTION_RE = re.compile(r"^##\s*(?:Source|Evidence|Provenance)", re.MULTILINE)
CANDIDATE_JSON_RE = re.compile(r"(?ms)```json[ \t]*\r?\n([^\r\n]+)\r?\n```")
CLAIMS_SECTION_RE = re.compile(r"(?ms)^## Claims[ \t]*\r?\n.*?(?=^## |\Z)")


# Page types where claims need provenance. Skill / rule / project-state
# pages are excluded — they are operational artifacts, not knowledge
# claims that cite external sources.
CLAIM_BEARING_TYPES = frozenset(
    {
        "concept",
        "decision",
        "pattern",
        "debugging",
        "qa",
        "gap",
    }
)


def _page_type(md: Path) -> str | None:
    """Extract OKF `type:` value from a page's frontmatter."""
    try:
        content = md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    fm = FRONTMATTER_RE.match(content)
    if not fm:
        return None
    m = TYPE_FIELD_RE.search(fm.group(1))
    return m.group(1).strip() if m else None


def check_missing_frontmatter(pages: list[Path]) -> list[str]:
    """Pages without any YAML frontmatter block (OKF violation)."""
    out: list[str] = []
    for md in pages:
        if md.name in EDITORIAL_NAMES:
            continue
        try:
            content = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not FRONTMATTER_RE.match(content):
            out.append(_rel(md))
    return out


def _frontmatter_of(md: Path) -> str | None:
    """The frontmatter block, or None when the page has none or cannot be read."""
    if md.name in EDITORIAL_NAMES:
        return None
    try:
        content = md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = FRONTMATTER_RE.match(content)
    if match is None:
        return None
    return match.group(1)


def _declared_type(frontmatter: str) -> str | None:
    match = TYPE_FIELD_RE.search(frontmatter)
    if match is None:
        return None
    return match.group(1).strip().strip("\"'")


def _missing_type(md: Path) -> bool:
    """A page with frontmatter but no usable `type:`.

    A page without frontmatter at all is reported by the frontmatter check;
    counting it twice would not tell the operator anything new.
    """
    frontmatter = _frontmatter_of(md)
    if frontmatter is None:
        return False
    return not _declared_type(frontmatter)


def check_missing_required_type(pages: list[Path]) -> list[str]:
    """Pages whose frontmatter lacks a non-empty `type:` field."""
    return [_rel(md) for md in pages if _missing_type(md)]


def _invalid_type_value(md: Path) -> str | None:
    frontmatter = _frontmatter_of(md)
    if frontmatter is None:
        return None
    declared = _declared_type(frontmatter)
    if not declared:
        return None
    canonical = TYPE_ALIASES.get(declared, declared)
    if canonical in VALID_TYPES:
        return None
    return canonical


def check_invalid_type_value(pages: list[Path]) -> list[str]:
    """Pages whose `type:` value is not in the canonical OKF type set."""
    out: list[str] = []
    for md in pages:
        invalid = _invalid_type_value(md)
        if invalid is not None:
            out.append(f"{_rel(md)} (type: {invalid!r} — not in canonical set)")
    return out


def _page_type_of(text: str) -> str | None:
    frontmatter = FRONTMATTER_RE.match(text)
    if frontmatter is None:
        return None
    return _declared_type(frontmatter.group(1))


def _candidate_record(page: Path, text: str) -> dict:
    """One inbox claim candidate: exactly one canonical JSON record, validated."""
    candidate_root = (ROOT / "knowledge" / "inbox" / "claims").resolve(strict=False)
    if candidate_root not in Path(page).resolve(strict=True).parents:
        raise ValueError("claim-candidate is allowed only under knowledge/inbox/claims")
    matches = CANDIDATE_JSON_RE.findall(text)
    if len(matches) != 1:
        raise ValueError("claim-candidate must embed exactly one JSON record")
    encoded = matches[0].encode("utf-8")
    candidate = json.loads(encoded)
    if canonical_json_bytes(candidate) != encoded:
        raise ValueError("claim-candidate record is not restricted canonical JSON")
    validate_schema(candidate, CANDIDATE_SCHEMA)
    validate_claim_record(candidate["claim"])
    return candidate["claim"]


def _ledger_records(raw: bytes) -> list[dict]:
    ledger = parse_claim_ledger(raw)
    if ledger is None:
        if b"## Claims" in raw:
            raise ValueError("Claims heading is malformed")
        return []
    return ledger["claims"]


def _claim_records(page: Path, raw: bytes, text: str) -> list[dict]:
    if _page_type_of(text) in INBOX_TYPES:
        return [_candidate_record(page, text)]
    return _ledger_records(raw)


def _require_evidence_matches(resolver: EvidenceResolver, record: dict) -> None:
    evidence = record["evidence"]
    resolved = resolver.resolve(evidence["reference"])
    same_bytes = resolved.bytes.decode("utf-8", errors="strict") == evidence["text"]
    if resolved.sha256 != evidence["sha256"] or not same_bytes:
        raise ValueError("claim evidence does not match resolved bytes")


def _validate_one_claim_page(resolver: EvidenceResolver, page: Path) -> None:
    raw = read_stable_bytes(page, MAX_CLAIM_PAGE_BYTES, label="lint claim page")
    text = raw.decode("utf-8", errors="strict")
    for record in _claim_records(page, raw, text):
        _require_evidence_matches(resolver, record)


def _page_label(page: Path) -> str:
    try:
        return _rel(page)
    except ValueError:
        return Path(page).as_posix()


def check_claim_schemas(pages: list[Path]) -> list[str]:
    """Validate canonical claim ledgers and quarantined inbox candidates."""
    resolver = EvidenceResolver(ROOT, state_root=STATE_ROOT)
    findings: list[str] = []
    for page in pages:
        try:
            _validate_one_claim_page(resolver, page)
        except (OSError, UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
            findings.append(f"{_page_label(page)}: {exc}")
    return findings




def _project_claim_pages(projects_root: Path) -> list[Path]:
    """Return bounded-lint project documents that may carry claim ledgers."""
    if not projects_root.exists():
        return []
    names = {"context.md", "journal.md", "state.md"}
    return sorted(
        page
        for page in projects_root.rglob("*.md")
        if page.is_file() and page.name in names
    )


def _needs_source_citation(md: Path) -> bool:
    """Claim-bearing pages long enough to have said something.

    Skills, rules, and project state are operational and cite nothing; a page
    under fifty words is a stub, not yet a claim.
    """
    if md.name in EDITORIAL_NAMES:
        return False
    if _page_type(md) not in CLAIM_BEARING_TYPES:
        return False
    return _word_count(md) >= 50


def _cites_a_source(md: Path) -> bool:
    try:
        content = md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return True
    if SOURCE_SECTION_RE.search(content):
        return True
    frontmatter = FRONTMATTER_RE.match(content)
    return bool(frontmatter and SOURCES_FIELD_RE.search(frontmatter.group(1)))


def check_missing_sources_section(pages: list[Path]) -> list[str]:
    """Claim-bearing pages should cite their source (frontmatter or section)."""
    return [
        _rel(md)
        for md in pages
        if _needs_source_citation(md) and not _cites_a_source(md)
    ]


def _supersede_target(md: Path) -> str:
    """The page this one claims to be superseded by, or an empty string."""
    try:
        content = md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    match = SUPERSEDED_BY_RE.search(content)
    if match is None:
        return ""
    target = match.group(1).strip().strip("`\"'")
    return target.replace("[[", "").replace("]]", "").strip()


def check_invalid_supersede_chain(pages: list[Path]) -> list[str]:
    """`superseded_by:` references must resolve to an existing page.

    Cycle detection is left to a future check; this verifies the immediate
    target exists.
    """
    out: list[str] = []
    for md in pages:
        target = _supersede_target(md)
        if target and _resolve_link(target, [VAULT, NOTES]) is None:
            out.append(f"{_rel(md)} -> superseded_by [[{target}]] (target not found)")
    return out


# Fields for temporal validity (Phase 6 — from Graphiti concept)
VALID_FROM_RE = re.compile(r"^valid_from:\s*(.+?)\s*$", re.MULTILINE)
VALID_TO_RE = re.compile(r"^valid_to:\s*(.+?)\s*$", re.MULTILINE)


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STATUS_FIELD_RE = re.compile(r"^status:\s*(.+?)\s*$", re.MULTILINE)
_OPEN_ENDED_VALID_TO = ("null", "none", "~", "")


def _expiry_date(frontmatter: str) -> str | None:
    """The page's `valid_to` as an ISO date, or None when it does not expire.

    A non-date value such as "forever" is not a violation, and comparing it as
    a string would sort it before today and report one.
    """
    match = VALID_TO_RE.search(frontmatter)
    if match is None:
        return None
    value = match.group(1).strip().strip('"\'')
    if value.lower() in _OPEN_ENDED_VALID_TO:
        return None
    date = value[:10]
    if _ISO_DATE_RE.match(date) is None:
        return None
    return date


def _declared_status(frontmatter: str) -> str:
    match = _STATUS_FIELD_RE.search(frontmatter)
    if match is None:
        return ""
    return match.group(1).strip()


def _expired_active_finding(md: Path, today: str) -> str | None:
    frontmatter = _frontmatter_of(md)
    if frontmatter is None:
        return None
    expiry = _expiry_date(frontmatter)
    if expiry is None or expiry >= today:
        return None
    status = _declared_status(frontmatter)
    if status not in ("", "active"):
        return None
    return (
        f"{_rel(md)} (valid_to={expiry} < today={today}, "
        f"but status={status or 'unset'})"
    )


def check_temporal_validity(pages: list[Path]) -> list[str]:
    """Flag pages whose `valid_to` has passed while status is still active.

    A fact that stopped being true should be marked superseded; left active it
    keeps turning up in search results.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    out: list[str] = []
    for md in pages:
        finding = _expired_active_finding(md, today)
        if finding is not None:
            out.append(finding)
    return out


def _referenced_targets(pages: list[Path], skip: set[Path]) -> set[str]:
    """Every wikilink target named by a page outside `skip`, alias stripped."""
    referenced: set[str] = set()
    for md in pages:
        if md.resolve() in skip:
            continue
        referenced.update(_page_link_targets(md))
    return referenced


def _page_link_targets(md: Path) -> set[str]:
    try:
        content = md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return set()
    return {link.strip().split("|")[0].strip() for link in WIKILINK_RE.findall(content)}


def check_orphan_gaps(pages: list[Path]) -> list[str]:
    """Pages with type: gap should be linked from at least one non-gap page.

    A gap page marks a concept mentioned but not yet written. With no non-gap
    page pointing at it, the gap is itself an orphan and nobody will close it.
    """
    gap_pages = _gap_pages(pages)
    if not gap_pages:
        return []
    referenced = _referenced_targets(pages, {p.resolve() for p in gap_pages})
    return [_rel(gap) for gap in gap_pages if _is_orphan_gap(gap, referenced)]


def _gap_pages(pages: list[Path]) -> list[Path]:
    return sorted(page for page in pages if _page_type(page) == "gap")


def _is_orphan_gap(gap: Path, referenced: set[str]) -> bool:
    if gap.name in EDITORIAL_NAMES:
        return False
    return gap.stem not in referenced and _rel(gap) not in referenced


# ---------- contradictions (LLM, opt-in) ----------

MAX_CONTRADICTION_BYTES = 120_000
CONTRADICTION_SYSTEM_PROMPT = "You are a careful auditor. Only flag real contradictions."


def _bounded_page_blob(pages: list[Path], max_bytes: int) -> str:
    """Page bodies concatenated up to a byte cap, so one call stays affordable."""
    parts: list[str] = []
    total = 0
    for md in pages:
        chunk = _page_chunk(md)
        if chunk is None:
            continue
        if total + len(chunk) > max_bytes:
            break
        parts.append(chunk)
        total += len(chunk)
    return "".join(parts)


def _page_chunk(md: Path) -> str | None:
    try:
        text = md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    return f"\n\n### FILE: {_rel(md)}\n{text}"


def _contradiction_prompt(blob: str) -> str:
    return f"""You are auditing a markdown knowledge vault for logical contradictions.

Read the following pages. Flag ONLY concrete contradictions between them — not
stylistic drift, not differing levels of detail, not overlapping scope.
Examples of real contradictions: two pages giving different answers to the same
question; a rule stated one way here and the opposite way there; a dated
decision on page A that page B silently violates.

Output format: one finding per line, prefixed with "- ". Each line: "<page A> vs <page B>: <what contradicts>". If there are no contradictions, output the single token: NO_CONTRADICTIONS

--- PAGES ---
{blob}
"""


def _bulleted_findings(answer: str) -> list[str]:
    return [line[2:].strip() for line in answer.splitlines() if line.startswith("- ")]


def _contradiction_findings(answer: str | None) -> list[str]:
    if not answer:
        return []
    if "NO_CONTRADICTIONS" in answer.upper():
        return []
    if answer.startswith("("):
        # llm_client returns parenthesized error strings on failure.
        return [answer]
    return _bulleted_findings(answer)


def check_contradictions(pages: list[Path]) -> list[str]:
    """Ask the LLM to flag pairs of pages that appear to contradict each other.

    Structural checks are free; this one costs a model call and is opt-in via
    `--contradictions`. Absence of a provider is reported, not fatal.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from llm_client import call_llm
    except ImportError:
        return ["(llm_client not available — skipped)"]
    if not pages:
        return []
    answer = call_llm(
        _contradiction_prompt(_bounded_page_blob(pages, MAX_CONTRADICTION_BYTES)),
        system_prompt=CONTRADICTION_SYSTEM_PROMPT,
        max_tokens=2000,
    )
    return _contradiction_findings(answer)


# ---------- driver ----------

CHECK_NAMES = (
    "broken_wikilinks",
    "orphan_pages",
    "orphan_daily_logs",
    "stale_compiled",
    "missing_backlinks",
    "sparse_pages",
    # Phase 2 OKF conformance checks.
    "missing_frontmatter",
    "missing_required_type",
    "invalid_type_value",
    "missing_sources_section",
    "invalid_supersede_chain",
    "orphan_gaps",
    # Phase 6 temporal validity.
    "temporal_validity",
    "invalid_evidence",
    "invalid_claim_schema",
    "contradictions",
)


def _labelled(label: str, items: list[str]) -> list[str]:
    return [f"[{label}] {item}" for item in items]


def _unique_by_resolved_path(pages: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    unique: list[Path] = []
    for page in pages:
        resolved = page.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(page)
    return unique


def _notes_scope(scope: str) -> list[tuple[str, list[Path], Path]]:
    """Notes are one tree; the legacy scope labels stay as aliases."""
    if scope not in ("memory", "wiki", "all"):
        return []
    pages = [p for p in _iter_tree_md(NOTES) if p.name not in EDITORIAL_NAMES]
    label = "notes" if scope == "all" else scope
    return [(label, pages, VAULT_INDEX)]


def _state_findings(findings: dict[str, list[str]]) -> None:
    state = load_state()
    findings["orphan_daily_logs"] = check_orphan_daily_logs(state)
    findings["stale_compiled"] = check_stale_compiled(state)
    project_pages = _project_claim_pages(ROOT / "knowledge" / "projects")
    findings["invalid_claim_schema"] += _labelled(
        "projects", check_claim_schemas(project_pages)
    )


def _page_checks(
    label: str,
    pages: list[Path],
    index: Path,
    sparse_words: int,
) -> dict[str, list[str]]:
    """Every per-page check for one scope, already labelled."""
    tree = _unique_by_resolved_path(list(_iter_tree_md(NOTES)))
    search_roots = [VAULT, NOTES]
    results = {
        "broken_wikilinks": check_broken_links(tree, search_roots),
        "orphan_pages": check_orphans_against_index(pages, index),
        "missing_backlinks": check_missing_backlinks(pages, search_roots),
        "sparse_pages": check_sparse_pages(pages, sparse_words),
        "missing_frontmatter": check_missing_frontmatter(pages),
        "missing_required_type": check_missing_required_type(pages),
        "invalid_type_value": check_invalid_type_value(pages),
        "missing_sources_section": check_missing_sources_section(pages),
        "invalid_supersede_chain": check_invalid_supersede_chain(pages),
        "orphan_gaps": check_orphan_gaps(pages),
        "temporal_validity": check_temporal_validity(pages),
        "invalid_evidence": check_evidence_references(pages),
        "invalid_claim_schema": check_claim_schemas(pages),
    }
    return {name: _labelled(label, items) for name, items in results.items()}


def _frontmatter_only_findings(findings: dict[str, list[str]]) -> None:
    """Skills and rules carry OKF frontmatter but no wikilinks or backlinks."""
    for label, root in (("skills", ROOT / "skills"), ("rules", ROOT / "rules")):
        pages = _iter_tree_md(root)
        findings["missing_frontmatter"] += _labelled(
            label, check_missing_frontmatter(pages)
        )
        findings["missing_required_type"] += _labelled(
            label, check_missing_required_type(pages)
        )
    findings["invalid_claim_schema"] += _labelled(
        "inbox", check_claim_schemas(_iter_tree_md(ROOT / "knowledge" / "inbox"))
    )


def _merge(findings: dict[str, list[str]], produced: dict[str, list[str]]) -> None:
    for name, items in produced.items():
        findings[name] += items


def _scan_scopes(
    findings: dict[str, list[str]],
    scopes: list[tuple[str, list[Path], Path]],
    sparse_words: int,
) -> list[Path]:
    scanned: list[Path] = []
    for label, pages, index in scopes:
        _merge(findings, _page_checks(label, pages, index, sparse_words))
        scanned += pages
    return scanned


def _scope_findings(args: argparse.Namespace, findings: dict[str, list[str]]) -> list[Path]:
    """Run every scope-dependent check and return the pages that were scanned."""
    if args.scope in ("memory", "all"):
        _state_findings(findings)
    scanned = _scan_scopes(findings, _notes_scope(args.scope), args.sparse_words)
    if args.scope == "all":
        _frontmatter_only_findings(findings)
    return scanned


def run_checks(args: argparse.Namespace) -> dict[str, list[str]]:
    findings: dict[str, list[str]] = {name: [] for name in CHECK_NAMES}
    scanned = _scope_findings(args, findings)
    if args.contradictions and not args.structural_only:
        findings["contradictions"] = check_contradictions(scanned)
    return findings


def _contradiction_state(args: argparse.Namespace) -> str:
    if args.contradictions and not args.structural_only:
        return "on"
    return "off"


def _section_lines(section: str, items: list[str]) -> list[str]:
    lines = [f"## {section.replace('_', ' ').title()} ({len(items)})"]
    lines.extend(f"- {item}" for item in items or ["(none)"])
    lines.append("")
    return lines


def write_report(findings: dict[str, list[str]], args: argparse.Namespace) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"lint-{today}.md"
    total = sum(len(items) for items in findings.values())
    lines = [
        f"# Vault lint — {today}",
        "",
        f"Scope: `{args.scope}`  |  Sparse floor: {args.sparse_words} words  |  "
        f"Contradictions: {_contradiction_state(args)}",
        "",
        f"Total findings: **{total}**",
        "",
    ]
    for section, items in findings.items():
        lines.extend(_section_lines(section, items))
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def _report_display(report: Path) -> str:
    """Vault-relative when it lives inside the vault, absolute otherwise."""
    try:
        return report.relative_to(ROOT).as_posix()
    except ValueError:
        return report.as_posix()


def _print_summary(findings: dict[str, list[str]], report: Path) -> None:
    total = sum(len(items) for items in findings.values())
    print(f"lint_memory: {total} finding(s). Report: {_report_display(report)}")
    for section, items in findings.items():
        if items:
            print(f"  {section}: {len(items)}")


def _blocking_findings(
    findings: dict[str, list[str]], allowed: set[str]
) -> dict[str, list[str]]:
    return {
        section: items
        for section, items in findings.items()
        if items and section not in allowed
    }


def _report_blocking(blocking: dict[str, list[str]], allowed: set[str]) -> None:
    total = sum(len(items) for items in blocking.values())
    print("")
    print(
        f"lint_memory: FAILING BUILD — {total} blocking finding(s) "
        f"in {len(blocking)} categor(ies): {', '.join(blocking)}."
    )
    print(f"(Allowed / non-blocking categories: {sorted(allowed) or '(none)'})")


def main() -> int:
    args = parse_args()
    findings = run_checks(args)
    _print_summary(findings, write_report(findings, args))
    # Without --fail-on-findings the exit code stays 0. `orphan_daily_logs` is
    # exempt by default because the next compile pass clears it on its own.
    if not getattr(args, "fail_on_findings", False):
        return 0
    allowed = set(args.allowed_categories or [])
    blocking = _blocking_findings(findings, allowed)
    if not blocking:
        return 0
    _report_blocking(blocking, allowed)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
