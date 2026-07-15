"""Regenerate `knowledge/index.md` from `knowledge/notes/**/*.md`.

Supports both flat notes (current public layout) and typed subdirs
(concepts/decisions/patterns/debugging/qa) when present.
"""
from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bounded_io import read_stable_bytes  # noqa: E402
from claim_tree_manifest import snapshot_claim_tree_with_content  # noqa: E402
from markdown_transaction import (  # noqa: E402
    TransactionFailure,
    mutate_knowledge,
    stable_operation_id,
)
from memory_state import ROOT  # noqa: E402

memory = ROOT / "knowledge"
knowledge = memory / "notes"
out = memory / "index.md"

TYPE_SECTIONS = {
    "concept": "Concepts",
    "decision": "Decisions",
    "pattern": "Patterns",
    "debugging": "Debugging",
    "qa": "Q&A",
    "entity": "Entities",
    "synthesis": "Syntheses",
    "comparison": "Comparisons",
    "connection": "Connections",
    "workflow": "Workflows",
    "raw-source": "Raw sources",
}

SUBDIR_SECTIONS = {
    "Concepts": knowledge / "concepts",
    "Decisions": knowledge / "decisions",
    "Patterns": knowledge / "patterns",
    "Debugging": knowledge / "debugging",
    "Q&A": knowledge / "qa",
}

SUMMARY_RE = re.compile(r"^One-sentence summary:\s*(.+?)\s*$", re.MULTILINE)
TYPE_RE = re.compile(r"^type:\s*(.+?)\s*$", re.MULTILINE)
STATUS_RE = re.compile(r"^status:\s*(.+?)\s*$", re.MULTILINE)
SKIP_NAMES = {"README.md", "index.md", "log.md"}
MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_PAGE_COUNT = 2_000
MAX_TOTAL_PAGE_BYTES = 32 * 1024 * 1024
MAX_REBUILD_ATTEMPTS = 4


def extract_hook(md_path: Path) -> str:
    text = md_path.read_text(encoding="utf-8", errors="ignore")
    m = SUMMARY_RE.search(text)
    if m:
        return m.group(1).strip()
    lines = [ln.strip() for ln in text.splitlines()]
    for i, ln in enumerate(lines):
        if ln.startswith("# "):
            for follow in lines[i + 1 :]:
                if follow and not follow.startswith("#") and not follow.startswith("---"):
                    return follow
            break
    return ""


def extract_type(md_path: Path) -> str:
    try:
        text = md_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    m = TYPE_RE.search(text)
    return m.group(1).strip().strip("\"'") if m else ""


def rel_link(md_path: Path) -> str:
    rel = md_path.relative_to(ROOT).with_suffix("")
    return rel.as_posix()


def build_index_bytes(
    root: Path = ROOT,
    pending: Mapping[str, bytes | None] | None = None,
    *,
    base: Mapping[str, bytes] | None = None,
) -> bytes:
    """Render the index from disk plus unpublished transaction after-images."""
    root = Path(root)
    notes = root / "knowledge" / "notes"
    virtual: dict[str, bytes] = dict(base or {})
    if base is None and notes.exists():
        for path in notes.rglob("*.md"):
            relative = path.relative_to(root).as_posix()
            virtual[relative] = read_stable_bytes(
                path, MAX_PAGE_BYTES, label="knowledge index source"
            )
    if len(virtual) > MAX_PAGE_COUNT:
        raise ValueError("knowledge index page count exceeds limit")
    if sum(len(content) for content in virtual.values()) > MAX_TOTAL_PAGE_BYTES:
        raise ValueError("knowledge index source bytes exceed limit")
    for relative, content in (pending or {}).items():
        if not relative.startswith("knowledge/notes/"):
            raise ValueError("pending index path is outside knowledge notes")
        if content is None:
            virtual.pop(relative, None)
        else:
            if len(content) > MAX_PAGE_BYTES:
                raise ValueError("pending index page exceeds limit")
            virtual[relative] = content
    if len(virtual) > MAX_PAGE_COUNT:
        raise ValueError("knowledge index page count exceeds limit")
    if sum(len(content) for content in virtual.values()) > MAX_TOTAL_PAGE_BYTES:
        raise ValueError("knowledge index source bytes exceed limit")

    buckets: dict[str, list[tuple[str, str]]] = {
        name: [] for name in TYPE_SECTIONS.values()
    }
    buckets["Other"] = []
    for relative, raw in sorted(virtual.items()):
        if Path(relative).name in SKIP_NAMES or "/archive/" in f"/{relative}/":
            continue
        text = raw.decode("utf-8", errors="strict")
        status = STATUS_RE.search(text)
        if status and status.group(1).strip() in {"superseded", "archived"}:
            continue
        page_type = TYPE_RE.search(text)
        section = TYPE_SECTIONS.get(
            page_type.group(1).strip().strip("\"'") if page_type else "",
            "Other",
        )
        summary = SUMMARY_RE.search(text)
        buckets[section].append(
            (str(Path(relative).with_suffix("")).replace("\\", "/"), summary.group(1).strip() if summary else "")
        )

    lines = [
        "# Session Memory Index",
        "",
        "This index catalogs durable memory distilled from AI agent sessions",
        "(OpenCode, Codex, Claude Code, Cursor, Antigravity).",
        "",
        "## Entry points",
        "- [[docs/operating-model]] — compile cadence, promotion rules, and the daily ↔ notes boundary.",
        "- Recent daily logs live under `knowledge/daily/` — raw, timestamped session captures awaiting compile.",
        "",
    ]
    for name in [*TYPE_SECTIONS.values(), "Other"]:
        pages = sorted(set(buckets.get(name, [])), key=lambda item: item[0].casefold())
        if not pages:
            continue
        lines.append(f"## {name}")
        for link, summary in pages:
            lines.append(f"- [[{link}]] — {summary}" if summary else f"- [[{link}]]")
        lines.append("")
    lines.extend(
        [
            "## Editorial note",
            "This index is vault metadata — a navigation map over `knowledge/notes/`, not a page derived from `raw/` or `inbox/`. It is regenerated by `scripts/rebuild_memory_index.py`; edits to page titles or one-sentence summaries will be picked up on the next rebuild.",
        ]
    )
    output = ("\n".join(lines).rstrip() + "\n").encode("utf-8")
    if len(output) > MAX_INDEX_BYTES:
        raise ValueError("knowledge index output exceeds limit")
    return output


def collect_pages() -> dict[str, list[Path]]:
    buckets: dict[str, list[Path]] = {name: [] for name in TYPE_SECTIONS.values()}
    buckets["Other"] = []

    # Prefer typed subdirs when they exist and have content.
    used_subdir = False
    for name, path in SUBDIR_SECTIONS.items():
        if path.exists():
            pages = sorted(p for p in path.glob("*.md") if p.name not in SKIP_NAMES)
            # Check status FIRST, before bucketing — superseded/archived
            # pages must not appear in the index regardless of location.
            filtered = []
            for p in pages:
                try:
                    content = p.read_text(encoding="utf-8", errors="ignore")
                    status_m = STATUS_RE.search(content)
                    if status_m and status_m.group(1).strip() in ("superseded", "archived"):
                        continue
                except OSError:
                    continue
                filtered.append(p)
            if filtered:
                buckets[name].extend(filtered)
                used_subdir = True

    # Flat notes + any remaining nested files.
    already_bucketed: set[Path] = {x for xs in buckets.values() for x in xs}
    if knowledge.exists():
        for p in sorted(knowledge.rglob("*.md")):
            if p.name in SKIP_NAMES:
                continue
            if "archive" in p.parts:
                continue
            # Skip superseded/archived pages from the index
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
                status_m = STATUS_RE.search(content)
                if status_m and status_m.group(1).strip() in ("superseded", "archived"):
                    continue
            except OSError:
                continue
            if used_subdir and p.parent != knowledge and p.parent.name in {
                "concepts", "decisions", "patterns", "debugging", "qa"
            }:
                continue  # already listed via subdir
            if p in already_bucketed:
                continue
            t = extract_type(p)
            section = TYPE_SECTIONS.get(t, "Other")
            buckets.setdefault(section, []).append(p)

    return buckets


def main() -> int:
    for _ in range(MAX_REBUILD_ATTEMPTS):
        manifest, tree = snapshot_claim_tree_with_content(ROOT)
        notes = {
            path: content
            for path, content in tree.items()
            if path.startswith("knowledge/notes/")
        }
        content = build_index_bytes(ROOT, base=notes)
        generation = str(manifest["absence_generation"])
        try:
            mutate_knowledge(
                stable_operation_id("rebuild-index", generation, content),
                {out: content},
                preconditions={"claim_tree_manifest": manifest},
            )
            return 0
        except TransactionFailure as exc:
            if exc.code != "precondition_failed":
                raise
    raise RuntimeError("knowledge index rebuild did not converge")


if __name__ == "__main__":
    raise SystemExit(main())
