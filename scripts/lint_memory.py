"""Structural + semantic lint across the vault.

Covers the `knowledge/notes/` tree (filename kept for backward compat with
hooks and docs that still reference `lint_memory`).

Thirteen checks (Phase 2 expanded the original seven + Phase 6 temporal):
 1. broken_wikilinks — wikilinks whose target does not resolve to a file.
 2. orphan_pages — knowledge/wiki pages not referenced by the relevant index.md.
 3. orphan_daily_logs — daily logs with no compile recorded in state.json.
 4. stale_compiled — daily log hash changed after last compile.
 5. missing_backlinks — page A links to page B, but B does not link back.
 6. sparse_pages — pages under a word-count floor (default 200 words).
 7. contradictions — LLM-judged conflicts between pages (opt-in, --contradictions).
 8. missing_frontmatter — page has no YAML `---` block (OKF violation).
 9. missing_required_type — frontmatter exists but `type:` is absent/empty.
10. missing_sources_section — claim-bearing page lacks `## Source` / `sources:`.
11. invalid_supersede_chain — `superseded_by:` points to a non-existent page.
12. orphan_gaps — page in `knowledge/notes/` has no inbound link from outside gaps/.
13. temporal_validity — `valid_to:` is in the past but `status:` is still active.

Usage:
    uv run python scripts/lint_memory.py                  # all scopes, structural only
    uv run python scripts/lint_memory.py --scope memory   # memory/ only
    uv run python scripts/lint_memory.py --scope wiki     # knowledge/notes/ only
    uv run python scripts/lint_memory.py --contradictions # also run the LLM check
    uv run python scripts/lint_memory.py --sparse-words 300

Writes a report to `$LLM_WIKI_STATE_ROOT/logs/lint-YYYY-MM-DD.md`
(default: ``$LLM_WIKI_ROOT/logs/`` — inside the vault, gitignored) and prints a summary.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import REPORTS_DIR, ROOT, file_hash, load_state  # noqa: E402
from vault_editorial import (  # noqa: E402
    BACKLINK_EXEMPT_NAMES,
    BROKEN_LINK_SKIP_NAMES,
    EDITORIAL_NAMES,
)

MEMORY = ROOT / "knowledge"
KNOWLEDGE = MEMORY / "notes"
DAILY_DIR = MEMORY / "daily"
MEMORY_INDEX = MEMORY / "index.md"

# Post three-zone: notes live under knowledge/notes; vault index is knowledge/index.md.
WIKI = ROOT / "knowledge" / "notes"
WIKI_INDEX = MEMORY / "index.md"

REPORTS = REPORTS_DIR
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?\]\]")
WORD_RE = re.compile(r"\b\w+\b")

DEFAULT_SPARSE_WORDS = 200

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


def _word_count(md: Path) -> int:
    text = md.read_text(encoding="utf-8", errors="ignore")
    body_lines = [
        ln for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("---")
    ]
    # Drop H1/H2/H3 headers from body for a content-only word count.
    body = "\n".join(ln for ln in body_lines if not ln.lstrip().startswith("#"))
    return len(WORD_RE.findall(body))


def _extract_links(md: Path) -> list[str]:
    text = md.read_text(encoding="utf-8", errors="ignore")
    return [m.group(1) for m in WIKILINK_RE.finditer(text)]


def _resolve_link(target: str, search_roots: list[Path]) -> Path | None:
    t = target.strip()
    if not t:
        return None
    # Path-style targets (e.g. "knowledge/notes/concepts/foo") — anchor at ROOT.
    if "/" in t:
        cands = [(ROOT / (t + ".md")).resolve(), (ROOT / t).resolve()]
        for c in cands:
            if c.exists() and c.is_file():
                return c
        return None
    # Bare targets — search each tree for a page whose stem matches.
    for root in search_roots:
        for p in root.rglob(f"{t}.md"):
            return p
    return None


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


def check_broken_links(pages: list[Path], search_roots: list[Path]) -> list[str]:
    out: list[str] = []
    tracked = _git_tracked_paths()
    for md in pages:
        # Daily logs contain transcribed prose that often cites `[[wikilinks]]`
        # or `[[...]]` as literal examples. They are append-only raw capture,
        # not curated pages — skip broken-link scanning over them.
        if DAILY_DIR in md.parents:
            continue
        if md.name in BROKEN_LINK_SKIP_NAMES:
            continue
        # When git is available, only scan tracked pages (public vault surface).
        # Personal gitignored notes under knowledge/projects/* must not fail CI
        # simulation or local lint — but links *from* tracked pages *to* those
        # files still fail below (untracked target).
        if tracked is not None:
            try:
                src_rel = md.resolve().relative_to(ROOT.resolve()).as_posix()
            except ValueError:
                continue
            if src_rel not in tracked:
                continue
        for t in _extract_links(md):
            # Skip placeholder-looking targets: ellipses, generic "wikilinks",
            # or angle-bracket templates like <category>/<slug>.
            tt = t.strip()
            if tt in ("...", "wikilinks") or "<" in tt or ">" in tt:
                continue
            resolved = _resolve_link(t, search_roots)
            if resolved is None:
                out.append(f"{_rel(md)} -> [[{t}]]")
                continue
            # If git metadata is available, require the target to be tracked.
            # Prevents "works on my machine" where a personal gitignored page
            # satisfies the link but CI clean checkout fails.
            if tracked is not None:
                try:
                    rel = resolved.resolve().relative_to(ROOT.resolve()).as_posix()
                except ValueError:
                    out.append(f"{_rel(md)} -> [[{t}]] (outside vault)")
                    continue
                if rel not in tracked:
                    out.append(f"{_rel(md)} -> [[{t}]] (untracked/gitignored target)")
    return out


def check_orphans_against_index(pages: list[Path], index: Path) -> list[str]:
    if not index.exists():
        return []
    index_txt = index.read_text(encoding="utf-8", errors="ignore")
    out: list[str] = []
    for md in pages:
        if md.name in EDITORIAL_NAMES:
            continue
        # Accept either stem or full relative path as a reference.
        stem = md.stem
        rel = md.relative_to(ROOT).with_suffix("").as_posix()
        if stem not in index_txt and rel not in index_txt:
            out.append(_rel(md))
    return out


def check_orphan_daily_logs(state: dict) -> list[str]:
    compiled = state.get("compiled_daily_hashes", {})
    out: list[str] = []
    if not DAILY_DIR.exists():
        return out
    for d in sorted(DAILY_DIR.glob("*.md")):
        if d.name not in compiled:
            out.append(_rel(d))
    return out


def check_stale_compiled(state: dict) -> list[str]:
    compiled = state.get("compiled_daily_hashes", {})
    out: list[str] = []
    if not DAILY_DIR.exists():
        return out
    for d in sorted(DAILY_DIR.glob("*.md")):
        h = compiled.get(d.name)
        if h and h != file_hash(d):
            out.append(_rel(d))
    return out


def check_missing_backlinks(pages: list[Path], search_roots: list[Path]) -> list[str]:
    """Within a set of pages, A->B must be matched by B->A."""
    page_set = set(pages)
    link_map: dict[Path, list[Path]] = {}
    for md in pages:
        resolved: list[Path] = []
        for t in _extract_links(md):
            r = _resolve_link(t, search_roots)
            if r is not None and r in page_set:
                resolved.append(r)
        link_map[md] = resolved

    out: list[str] = []
    seen: set[tuple[Path, Path]] = set()
    for a, targets in link_map.items():
        if a.name in EDITORIAL_NAMES or a.name in BACKLINK_EXEMPT_NAMES:
            continue
        for b in targets:
            if b == a or b.name in EDITORIAL_NAMES or b.name in BACKLINK_EXEMPT_NAMES:
                continue
            pair = (a, b)
            if pair in seen:
                continue
            seen.add(pair)
            if a not in link_map.get(b, []):
                out.append(f"{_rel(a)} -> {_rel(b)} (no backlink)")
    return out


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
# Five new structural checks added when the vault migrated to OKF
# (Open Knowledge Format v0.1). These catch:
#   8. missing_frontmatter       — page has no `---` YAML block at all
#   9. missing_required_type     — frontmatter exists but `type:` is absent/empty
#  10. missing_sources_section   — claim-bearing page lacks `## Source` or `sources:` frontmatter
#  11. invalid_supersede_chain   — `superseded_by:` points to a non-existent target
#  12. orphan_gaps               — page in knowledge/notes/ has no inbound link from a concept


FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
TYPE_FIELD_RE = re.compile(r"^type:\s*(.+?)\s*$", re.MULTILINE)
SUPERSEDED_BY_RE = re.compile(r"^superseded_by:\s*\[?\[?([^\]\n]+?)\]?\]?\s*$", re.MULTILINE)
SOURCES_FIELD_RE = re.compile(r"^sources:", re.MULTILINE)
SOURCE_SECTION_RE = re.compile(r"^##\s*Source", re.MULTILINE)


# Page types where claims need provenance. Skill / rule / project-state
# pages are excluded — they are operational artifacts, not knowledge
# claims that cite external sources.
CLAIM_BEARING_TYPES = frozenset(
    {
        "concept",
        "decision",
        "synthesis",
        "comparison",
        "connection",
        "pattern",
        "debugging",
        "qa",
        "fact",
        "entity",
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


def check_missing_required_type(pages: list[Path]) -> list[str]:
    """Pages whose frontmatter lacks a non-empty `type:` field."""
    out: list[str] = []
    for md in pages:
        if md.name in EDITORIAL_NAMES:
            continue
        try:
            content = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        fm = FRONTMATTER_RE.match(content)
        if not fm:
            continue  # reported by check_missing_frontmatter; don't double-count
        m = TYPE_FIELD_RE.search(fm.group(1))
        if not m or not m.group(1).strip():
            out.append(_rel(md))
    return out


def check_missing_sources_section(pages: list[Path]) -> list[str]:
    """Claim-bearing pages should cite their source (frontmatter or section).

    Only fires for OKF types in CLAIM_BEARING_TYPES — skills / rules /
    project-state are operational and need no external citation.
    Skips pages under 50 words (too short to require provenance).
    """
    out: list[str] = []
    for md in pages:
        if md.name in EDITORIAL_NAMES:
            continue
        ptype = _page_type(md)
        if ptype not in CLAIM_BEARING_TYPES:
            continue
        # Skip short pages — likely stubs, not yet ready for citation.
        if _word_count(md) < 50:
            continue
        try:
            content = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        fm = FRONTMATTER_RE.match(content)
        has_sources_field = bool(fm and SOURCES_FIELD_RE.search(fm.group(1)))
        has_source_section = bool(SOURCE_SECTION_RE.search(content))
        if not has_sources_field and not has_source_section:
            out.append(_rel(md))
    return out


def check_invalid_supersede_chain(pages: list[Path]) -> list[str]:
    """`superseded_by:` references must resolve to an existing page.

    Returns findings of the form:
        <page> -> superseded_by <target> (target not found)
    Cycle detection is left to a future check — for now we just verify
    the immediate target exists.
    """
    out: list[str] = []
    for md in pages:
        try:
            content = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # Look for superseded_by: in frontmatter OR body (be lenient).
        m = SUPERSEDED_BY_RE.search(content)
        if not m:
            continue
        target = m.group(1).strip().strip("`\"'")
        # Strip wikilink brackets if present.
        target = target.replace("[[", "").replace("]]", "").strip()
        if not target:
            continue
        # Try to resolve as a wikilink target.
        resolved = _resolve_link(target, [MEMORY, WIKI])
        if resolved is None:
            out.append(f"{_rel(md)} -> superseded_by [[{target}]] (target not found)")
    return out


# Fields for temporal validity (Phase 6 — from Graphiti concept)
VALID_FROM_RE = re.compile(r"^valid_from:\s*(.+?)\s*$", re.MULTILINE)
VALID_TO_RE = re.compile(r"^valid_to:\s*(.+?)\s*$", re.MULTILINE)


def check_temporal_validity(pages: list[Path]) -> list[str]:
    """Flag pages where valid_to is in the past but status is still active.

    Inspired by Graphiti's bi-temporal model. A fact with valid_to in
    the past should be marked superseded, not left as 'active' —
    otherwise stale facts pollute search results.
    """
    out: list[str] = []
    today = datetime.now().strftime("%Y-%m-%d")
    for md in pages:
        if md.name in EDITORIAL_NAMES:
            continue
        try:
            content = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        fm = FRONTMATTER_RE.match(content)
        if not fm:
            continue
        fm_text = fm.group(1)
        # Check if page has valid_to at all
        valid_to_m = VALID_TO_RE.search(fm_text)
        if not valid_to_m:
            continue
        valid_to = valid_to_m.group(1).strip().strip('"\'')
        if valid_to.lower() in ("null", "none", "~", ""):
            continue  # explicitly open-ended
        # Check if valid_to is in the past
        try:
            # Try ISO date parsing (YYYY-MM-DD or full ISO)
            vt_date = valid_to[:10]  # take just the date part
            if vt_date < today:
                # Check status — if still 'active', flag it
                status_val = ""
                sm = re.search(r"^status:\s*(.+?)\s*$", fm_text, re.MULTILINE)
                if sm:
                    status_val = sm.group(1).strip()
                if status_val in ("", "active"):
                    out.append(
                        f"{_rel(md)} (valid_to={vt_date} < today={today}, "
                        f"but status={status_val or 'unset'})"
                    )
        except (ValueError, IndexError):
            pass
    return out


def check_orphan_gaps(pages: list[Path]) -> list[str]:
    """Pages in knowledge/notes/ should be linked from at least one concept.

    A gap page exists to mark a "mentioned but not-yet-written" concept.
    If no concept references it, the gap itself is orphaned signal.
    """
    gaps_dir = WIKI / "gaps"
    if not gaps_dir.exists():
        return []
    out: list[str] = []
    # Collect all wikilink targets across non-gap pages.
    referenced: set[str] = set()
    for md in pages:
        if gaps_dir in md.parents:
            continue
        try:
            content = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for link in WIKILINK_RE.findall(content):
            referenced.add(link.strip().split("|")[0].strip())
    for gap_md in sorted(gaps_dir.glob("*.md")):
        if gap_md.name in EDITORIAL_NAMES:
            continue
        stem = gap_md.stem
        # Stem must appear as a wikilink target somewhere outside gaps/.
        if stem not in referenced and _rel(gap_md) not in {
            r for r in referenced
        }:
            out.append(_rel(gap_md))
    return out


# ---------- contradictions (LLM, opt-in) ----------

def check_contradictions(pages: list[Path]) -> list[str]:
    """Ask the LLM to flag pairs of pages that appear to contradict each other.

    Structural checks are free; this one uses the unified llm_client
    (Codex CLI / OpenAI / Ollama) and costs API calls. Opt-in via
    --contradictions. Returns a list of short finding strings. Non-fatal
    on LLM absence.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from llm_client import call_llm
    except ImportError:
        return ["(llm_client not available — skipped)"]

    if not pages:
        return []

    # Feed page bodies in a single pass; cap total size to keep cost bounded.
    MAX_BYTES = 120_000
    blob_parts: list[str] = []
    total = 0
    for md in pages:
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        chunk = f"\n\n### FILE: {_rel(md)}\n{text}"
        if total + len(chunk) > MAX_BYTES:
            break
        blob_parts.append(chunk)
        total += len(chunk)
    blob = "".join(blob_parts)

    prompt = f"""You are auditing a markdown knowledge vault for logical contradictions.

Read the following pages. Flag ONLY concrete contradictions between them — not
stylistic drift, not differing levels of detail, not overlapping scope.
Examples of real contradictions: two pages giving different answers to the same
question; a rule stated one way here and the opposite way there; a dated
decision on page A that page B silently violates.

Output format: one finding per line, prefixed with "- ". Each line: "<page A> vs <page B>: <what contradicts>". If there are no contradictions, output the single token: NO_CONTRADICTIONS

--- PAGES ---
{blob}
"""
    text = call_llm(
        prompt,
        system_prompt="You are a careful auditor. Only flag real contradictions.",
        max_tokens=2000,
    )
    if not text or "NO_CONTRADICTIONS" in text.upper():
        return []
    if text.startswith("("):
        # llm_client returns parenthesized error strings on failure.
        return [text]
    return [ln[2:].strip() for ln in text.splitlines() if ln.startswith("- ")]


# ---------- driver ----------

def run_checks(args: argparse.Namespace) -> dict[str, list[str]]:
    findings: dict[str, list[str]] = {k: [] for k in (
        "broken_wikilinks",
        "orphan_pages",
        "orphan_daily_logs",
        "stale_compiled",
        "missing_backlinks",
        "sparse_pages",
        # Phase 2 OKF conformance checks.
        "missing_frontmatter",
        "missing_required_type",
        "missing_sources_section",
        "invalid_supersede_chain",
        "orphan_gaps",
        # Phase 6 temporal validity.
        "temporal_validity",
        "contradictions",
    )}

    search_roots = [MEMORY, WIKI]

    # Three-zone: notes are a single tree. Keep legacy --scope labels as aliases
    # but never double-scan the same pages under "all".
    scopes: list[tuple[str, list[Path], Path]] = []
    notes_pages = [p for p in _iter_tree_md(KNOWLEDGE) if p.name not in EDITORIAL_NAMES]
    if args.scope in ("memory", "wiki", "all"):
        label = "notes" if args.scope == "all" else args.scope
        scopes.append((label, notes_pages, WIKI_INDEX if WIKI_INDEX.exists() else MEMORY_INDEX))

    # State-dependent checks (daily / compile hashes).
    if args.scope in ("memory", "all"):
        state = load_state()
        findings["orphan_daily_logs"] = check_orphan_daily_logs(state)
        findings["stale_compiled"] = check_stale_compiled(state)

    all_pages_for_contradictions: list[Path] = []

    for label, pages, index in scopes:
        # Single-tree scan (three-zone: notes is the only knowledge subtree).
        tree_all = list(_iter_tree_md(KNOWLEDGE))
        # Deduplicate by resolved path
        seen: set[Path] = set()
        unique_tree: list[Path] = []
        for p in tree_all:
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                unique_tree.append(p)
        findings["broken_wikilinks"] += [f"[{label}] {x}" for x in check_broken_links(unique_tree, search_roots)]
        findings["orphan_pages"] += [f"[{label}] {x}" for x in check_orphans_against_index(pages, index)]
        findings["missing_backlinks"] += [f"[{label}] {x}" for x in check_missing_backlinks(pages, search_roots)]
        findings["sparse_pages"] += [f"[{label}] {x}" for x in check_sparse_pages(pages, args.sparse_words)]
        findings["missing_frontmatter"] += [f"[{label}] {x}" for x in check_missing_frontmatter(pages)]
        findings["missing_required_type"] += [f"[{label}] {x}" for x in check_missing_required_type(pages)]
        findings["missing_sources_section"] += [f"[{label}] {x}" for x in check_missing_sources_section(pages)]
        findings["invalid_supersede_chain"] += [f"[{label}] {x}" for x in check_invalid_supersede_chain(pages)]
        findings["orphan_gaps"] += [f"[{label}] {x}" for x in check_orphan_gaps(pages)]
        findings["temporal_validity"] += [f"[{label}] {x}" for x in check_temporal_validity(pages)]
        all_pages_for_contradictions += pages

    if args.contradictions and not args.structural_only:
        findings["contradictions"] = check_contradictions(all_pages_for_contradictions)

    return findings


def write_report(findings: dict[str, list[str]], args: argparse.Namespace) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    out = REPORTS / f"lint-{today}.md"
    lines = [
        f"# Vault lint — {today}",
        "",
        f"Scope: `{args.scope}`  |  Sparse floor: {args.sparse_words} words  |  "
        f"Contradictions: {'on' if (args.contradictions and not args.structural_only) else 'off'}",
        "",
    ]
    total = sum(len(v) for v in findings.values())
    lines.append(f"Total findings: **{total}**")
    lines.append("")
    for section, items in findings.items():
        lines.append(f"## {section.replace('_', ' ').title()} ({len(items)})")
        if items:
            for item in items:
                lines.append(f"- {item}")
        else:
            lines.append("- (none)")
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main() -> int:
    args = parse_args()
    findings = run_checks(args)
    report = write_report(findings, args)
    total = sum(len(v) for v in findings.values())
    # Report lives inside the vault (runtime logs/ is gitignored) — show the
    # path relative to ROOT for a compact display, absolute as fallback.
    try:
        display = report.relative_to(ROOT).as_posix()
    except ValueError:
        display = report.as_posix()
    print(f"lint_memory: {total} finding(s). Report: {display}")
    for section, items in findings.items():
        if items:
            print(f"  {section}: {len(items)}")

    # CI enforcement: exit non-zero if any finding falls OUTSIDE the
    # allowed-categories exemption list. Without --fail-on-findings,
    # behavior is unchanged (always exit 0). See parse_args() for
    # rationale; orphan_daily_logs is exempt by default because it
    # self-resolves on the next compile pass.
    if getattr(args, "fail_on_findings", False):
        allowed = set(args.allowed_categories or [])
        blocking = {
            section: items
            for section, items in findings.items()
            if items and section not in allowed
        }
        if blocking:
            print("")
            print(
                f"lint_memory: FAILING BUILD — "
                f"{sum(len(v) for v in blocking.values())} blocking finding(s) "
                f"in {len(blocking)} categor(ies): {', '.join(blocking)}."
            )
            print(f"(Allowed / non-blocking categories: {sorted(allowed) or '(none)'})")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
