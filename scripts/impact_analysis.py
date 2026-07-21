"""Bounded Git-diff to active-Evidence-Graph impact analysis."""

from __future__ import annotations

import math
import os
import re
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bounded_io import read_stable_bytes  # noqa: E402
from corpus_snapshot import (  # noqa: E402
    CorpusChanged,
    _open_descriptor_chain,
    _read_bounded_descriptor,
    _seal_path,
    _verify_seal,
)
from memory_state import ROOT, STATE_ROOT  # noqa: E402
from repository_scope import sanitized_git_environment  # noqa: E402

KNOWLEDGE_DIR = ROOT / "knowledge" / "notes"
SKIP_NAMES = {"index.md", "log.md", "README.md", "state.md", "context.md"}
COMPARISONS = {"dirty", "worktree-index", "index-HEAD", "two-commits", "merge-base-branch"}
CODE_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb"}
TRAVERSED_EDGES = {
    "CALLS",
    "IMPORTS",
    "REFERENCES_SYMBOL",
    "DOCUMENTS",
    "DEFINES",
    "CHECKPOINT_CHANGED_FILE",
    "CHECKPOINT_RECORDED_DECISION",
}
CONFIRMED_CONFIDENCE = {"confirmed", "high"}
ZERO_OID = frozenset("0")
MAX_REVISION_LENGTH = 1024


class InvalidRevisionError(ValueError):
    """Raised when an impact endpoint is not a bounded verified commit."""


@dataclass(frozen=True)
class ImpactLimits:
    """Hard ceilings for one impact request."""

    max_files: int = 500
    max_blob_bytes: int = 4 * 1024 * 1024
    max_total_blob_bytes: int = 32 * 1024 * 1024
    max_graph_rows: int = 10_000
    max_symbols: int = 2_000
    max_depth: int = 8
    max_note_files: int = 2_000
    max_note_dirs: int = 256
    max_note_bytes: int = 2 * 1024 * 1024
    max_total_note_bytes: int = 32 * 1024 * 1024
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        values = (
            self.max_files,
            self.max_blob_bytes,
            self.max_total_blob_bytes,
            self.max_graph_rows,
            self.max_symbols,
            self.max_depth,
            self.max_note_files,
            self.max_note_dirs,
            self.max_note_bytes,
            self.max_total_note_bytes,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in values):
            raise ValueError("impact limits must be positive integers")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("impact timeout must be a positive finite number")


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("impact analysis deadline reached")
    return remaining


def _check_impact_stop(
    deadline: float | None, cancelled: Callable[[], bool] | None = None
) -> None:
    if cancelled is not None and cancelled():
        raise TimeoutError("impact analysis cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("impact analysis deadline reached")


def _git(root: Path, arguments: list[str], *, deadline: float, max_bytes: int) -> bytes:
    """Run Git without a shell and stop reading at the declared ceiling."""
    process = subprocess.Popen(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "diff.external=false",
            *arguments,
        ],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=sanitized_git_environment(),
        shell=False,
    )
    timed_out = threading.Event()

    def stop_at_deadline() -> None:
        timed_out.set()
        process.kill()

    timer = threading.Timer(_remaining(deadline), stop_at_deadline)
    timer.daemon = True
    timer.start()
    try:
        assert process.stdout is not None
        stdout = process.stdout.read(max_bytes + 1)
        if len(stdout) > max_bytes:
            process.kill()
        process.wait()
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()
    if timed_out.is_set():
        raise TimeoutError("Git impact command deadline reached")
    if len(stdout) > max_bytes:
        raise ValueError("Git impact output exceeds the read ceiling")
    if process.returncode != 0:
        detail = stdout[:1024].decode("utf-8", errors="replace").strip()
        raise ValueError(f"Git impact command failed: {detail or process.returncode}")
    return stdout


def _validate_revision(value: str | None, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_REVISION_LENGTH
        or value.startswith("-")
        or any(character in value for character in "\0\r\n")
    ):
        raise InvalidRevisionError(
            f"{label} revision is required and must be bounded and option-safe"
        )
    return value


def _resolve_revision(
    root: Path, value: str | None, label: str, *, deadline: float
) -> str:
    revision = _validate_revision(value, label)
    try:
        resolved = _git(
            root,
            ["rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"],
            deadline=deadline,
            max_bytes=128,
        ).decode("ascii", errors="strict").strip()
    except (UnicodeError, ValueError) as exc:
        raise InvalidRevisionError(f"{label} revision is not a valid commit") from exc
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", resolved) is None:
        raise InvalidRevisionError(
            f"{label} revision did not resolve to a full commit object ID"
        )
    return resolved


def _diff_arguments(
    comparison: str,
    *,
    base: str | None,
    target: str | None,
    branch: str | None,
    root: Path,
    deadline: float,
) -> list[tuple[str, list[str], bool]]:
    common = [
        "diff",
        "--raw",
        "--no-abbrev",
        "-z",
        "-M",
        "--no-ext-diff",
        "--no-textconv",
    ]
    supplied = {
        name
        for name, value in (("base", base), ("target", target), ("branch", branch))
        if value is not None
    }
    if comparison == "dirty":
        if supplied:
            raise ValueError("dirty comparison does not accept base, target, or branch")
        head = _resolve_revision(root, "HEAD", "HEAD", deadline=deadline)
        return [
            ("index-HEAD", [*common, "--cached", head, "--"], False),
            ("worktree-index", [*common, "--"], True),
        ]
    if comparison == "worktree-index":
        if supplied:
            raise ValueError("worktree-index comparison does not accept base, target, or branch")
        return [(comparison, [*common, "--"], True)]
    if comparison == "index-HEAD":
        if supplied:
            raise ValueError("index-HEAD comparison does not accept base, target, or branch")
        head = _resolve_revision(root, "HEAD", "HEAD", deadline=deadline)
        return [(comparison, [*common, "--cached", head, "--"], False)]
    if comparison == "two-commits":
        if branch is not None:
            raise ValueError("two-commits comparison does not accept branch")
        return [
            (
                comparison,
                [
                    *common,
                    _resolve_revision(root, base, "base", deadline=deadline),
                    _resolve_revision(root, target, "target", deadline=deadline),
                    "--",
                ],
                False,
            )
        ]
    if comparison == "merge-base-branch":
        if target is not None:
            raise ValueError("merge-base-branch comparison does not accept target")
        base_value = _resolve_revision(root, base, "base", deadline=deadline)
        branch_value = _resolve_revision(root, branch, "branch", deadline=deadline)
        merge_base = _git(
            root,
            ["merge-base", "--", base_value, branch_value],
            deadline=deadline,
            max_bytes=4096,
        ).decode("ascii", errors="strict").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", merge_base):
            raise ValueError("Git merge-base did not return an object ID")
        return [(comparison, [*common, merge_base, branch_value, "--"], False)]
    raise ValueError(f"comparison must be one of: {', '.join(sorted(COMPARISONS))}")


def _decode_path(value: bytes) -> str:
    return os.fsdecode(value).replace("\\", "/")


def _parse_raw_records(raw: bytes, comparison: str) -> list[dict]:
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    records = []
    index = 0
    while index < len(fields):
        header = fields[index]
        index += 1
        if not header.startswith(b":"):
            raise ValueError("malformed zero-delimited Git diff record")
        parts = header[1:].split()
        if len(parts) != 5:
            raise ValueError("malformed Git raw diff metadata")
        status_text = parts[4].decode("ascii", errors="strict")
        status = status_text[:1]
        path_count = 2 if status in {"R", "C"} else 1
        if index + path_count > len(fields):
            raise ValueError("truncated zero-delimited Git pathname record")
        old_path = _decode_path(fields[index])
        new_path = _decode_path(fields[index + 1]) if path_count == 2 else old_path
        index += path_count
        records.append(
            {
                "comparison": comparison,
                "status": status,
                "similarity": int(status_text[1:]) if status_text[1:].isdigit() else None,
                "old_path": old_path,
                "new_path": new_path,
                "old_oid": parts[2].decode("ascii", errors="strict"),
                "new_oid": parts[3].decode("ascii", errors="strict"),
            }
        )
    return records


def _object_blob(
    root: Path,
    oid: str,
    *,
    deadline: float,
    limit: int,
) -> bytes | None:
    if not oid or set(oid) <= ZERO_OID:
        return None
    return _git(root, ["cat-file", "blob", oid], deadline=deadline, max_bytes=limit)


def _worktree_blob(root: Path, relative: str, limit: int) -> bytes | None:
    components = relative.split("/")
    if not components or any(part in {"", ".", ".."} for part in components):
        raise PermissionError("changed worktree path is not repository-relative")
    path = root.joinpath(*components)
    try:
        return read_stable_bytes(path, limit, label=f"changed worktree file {relative}")
    except FileNotFoundError:
        return None


def _capture_note_file(root: Path, path: Path, limit: int) -> bytes:
    """Capture one note through a root-anchored stable descriptor chain."""
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise PermissionError("impact note path escapes the notes root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise PermissionError("impact note path is not root-relative")
    seal = _seal_path(
        root,
        path,
        target_directory=False,
        max_components=len(relative.parts),
    )
    descriptor = -1
    try:
        if os.name == "posix":
            descriptor = _open_descriptor_chain(seal, changed_error=PermissionError)
            content = _read_bounded_descriptor(descriptor, limit)
        else:
            content = read_stable_bytes(path, limit, label="impact note")
        _verify_seal(seal, changed_error=PermissionError)
        return content
    except CorpusChanged as exc:
        raise PermissionError("impact note changed during capture") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def collect_git_changes(
    root: Path = ROOT,
    *,
    comparison: str = "dirty",
    base: str | None = None,
    target: str | None = None,
    branch: str | None = None,
    limits: ImpactLimits | None = None,
    deadline: float | None = None,
) -> list[dict]:
    """Collect NUL-safe diff records and their bounded old/new blobs."""
    bounds = limits or ImpactLimits()
    root = Path(root).resolve(strict=True)
    if comparison not in COMPARISONS:
        raise ValueError(f"comparison must be one of: {', '.join(sorted(COMPARISONS))}")
    end = time.monotonic() + bounds.timeout_seconds if deadline is None else deadline
    records: list[dict] = []
    total_bytes = 0
    for phase, arguments, worktree_new in _diff_arguments(
        comparison, base=base, target=target, branch=branch, root=root, deadline=end
    ):
        raw = _git(
            root,
            arguments,
            deadline=end,
            max_bytes=max(64 * 1024, bounds.max_files * 16 * 1024),
        )
        parsed = _parse_raw_records(raw, phase)
        if len(records) + len(parsed) > bounds.max_files:
            raise ValueError("changed file ceiling exceeded")
        for record in parsed:
            old_blob = _object_blob(
                root, record["old_oid"], deadline=end, limit=bounds.max_blob_bytes
            )
            new_blob = (
                _worktree_blob(root, record["new_path"], bounds.max_blob_bytes)
                if worktree_new and record["status"] != "D"
                else _object_blob(
                    root, record["new_oid"], deadline=end, limit=bounds.max_blob_bytes
                )
            )
            total_bytes += len(old_blob or b"") + len(new_blob or b"")
            if total_bytes > bounds.max_total_blob_bytes:
                raise ValueError("changed blob total ceiling exceeded")
            record["old_blob"] = old_blob
            record["new_blob"] = new_blob
            records.append(record)
    return records


def get_changed_files(git_range: str | None = None) -> list[str]:
    """Compatibility wrapper; ranges are accepted only as explicit commit pairs."""
    try:
        if git_range is None:
            changes = collect_git_changes(ROOT)
        else:
            match = re.fullmatch(r"([^.]\S*)\.\.([^.]\S*)", git_range)
            if match is None:
                return []
            changes = collect_git_changes(
                ROOT, comparison="two-commits", base=match.group(1), target=match.group(2)
            )
        return sorted({str(item["new_path"] or item["old_path"]) for item in changes})
    except (OSError, TimeoutError, ValueError):
        return []


def extract_symbols_from_file(file_path: Path) -> list[str]:
    """Extract names for the explicitly low-confidence textual fallback."""
    if not file_path.exists():
        return []
    try:
        from code_graph import parse_file

        parsed = parse_file(file_path)
        return sorted(
            {item["name"] for key in ("functions", "classes") for item in parsed.get(key, [])}
        )
    except (ImportError, OSError, ValueError):
        try:
            content = file_path.read_bytes()
        except OSError:
            return []
        return _textual_symbols(content)


def _textual_symbols(content: bytes | None) -> list[str]:
    if not content:
        return []
    text = content.decode("utf-8", errors="ignore")
    patterns = (r"\bdef\s+(\w+)", r"\bclass\s+(\w+)", r"\bfunction\s+(\w+)")
    return sorted({match.group(1) for pattern in patterns for match in re.finditer(pattern, text)})


def find_stale_wiki_pages(
    changed_symbols: list[str],
    *,
    limits: ImpactLimits | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[dict]:
    """Legacy textual-name fallback; never represents graph evidence."""
    if not changed_symbols or not KNOWLEDGE_DIR.exists():
        return []
    bounds = limits or ImpactLimits()
    results = []
    markdown_files: list[tuple[Path, int]] = []
    pending = [KNOWLEDGE_DIR]
    file_count = 0
    directory_count = 0
    total_bytes = 0
    while pending:
        _check_impact_stop(deadline, cancelled)
        current = pending.pop()
        try:
            entries = os.scandir(current)
        except OSError:
            continue
        with entries:
            for entry in entries:
                _check_impact_stop(deadline, cancelled)
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    directory_count += 1
                    if directory_count > bounds.max_note_dirs:
                        raise ValueError("impact note directory ceiling exceeded")
                    pending.append(Path(entry.path))
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                file_count += 1
                if file_count > bounds.max_note_files:
                    raise ValueError("impact note file ceiling exceeded")
                if not entry.name.casefold().endswith(".md"):
                    continue
                if metadata.st_size > bounds.max_note_bytes:
                    raise ValueError("impact note file byte ceiling exceeded")
                total_bytes += metadata.st_size
                if total_bytes > bounds.max_total_note_bytes:
                    raise ValueError("impact note total byte ceiling exceeded")
                markdown_files.append((Path(entry.path), metadata.st_size))

    for markdown, expected_size in sorted(markdown_files):
        _check_impact_stop(deadline, cancelled)
        if markdown.name in SKIP_NAMES or "archive" in markdown.parts:
            continue
        try:
            raw = _capture_note_file(KNOWLEDGE_DIR, markdown, bounds.max_note_bytes)
        except FileNotFoundError:
            continue
        except PermissionError:
            raise
        except OSError:
            continue
        if len(raw) != expected_size:
            raise PermissionError("impact note changed after discovery")
        content = raw.decode("utf-8", errors="ignore")
        if "status: superseded" in content:
            continue
        matched = [
            symbol
            for symbol in changed_symbols
            if re.search(r"\b" + re.escape(symbol) + r"\b", content)
        ]
        if not matched:
            continue
        try:
            relative = markdown.relative_to(ROOT).as_posix()
        except ValueError:
            relative = str(markdown)
        results.append(
            {
                "slug": markdown.stem,
                "path": relative,
                "matched_symbols": matched,
                "confidence": "high" if len(matched) >= 3 else "medium",
                "reason": f"mentions {len(matched)} changed symbol(s): {', '.join(matched[:5])}",
            }
        )
    return sorted(
        results,
        key=lambda item: (item["confidence"] != "high", -len(item["matched_symbols"]), item["path"]),
    )


def apply_significance_budget(pages: list[dict], threshold: float = 0.8) -> list[dict]:
    """Retain the smallest prefix covering the requested textual-match weight."""
    if not pages or len(pages) <= 5:
        return pages
    total = sum(len(page.get("matched_symbols", [])) for page in pages)
    if total == 0:
        return pages
    selected = []
    cumulative = 0
    for page in sorted(pages, key=lambda item: len(item.get("matched_symbols", [])), reverse=True):
        selected.append(page)
        cumulative += len(page.get("matched_symbols", []))
        if cumulative / total >= threshold:
            break
    return selected


def _changed_ranges(
    old: bytes | None, new: bytes | None, *, deadline: float | None = None
) -> list[dict]:
    _check_impact_stop(deadline)
    old_lines = (old or b"").splitlines(keepends=True)
    new_lines = (new or b"").splitlines(keepends=True)
    old_offsets = [0]
    new_offsets = [0]
    for line in old_lines:
        _check_impact_stop(deadline)
        old_offsets.append(old_offsets[-1] + len(line))
    for line in new_lines:
        _check_impact_stop(deadline)
        new_offsets.append(new_offsets[-1] + len(line))
    prefix = 0
    shared = min(len(old_lines), len(new_lines))
    while prefix < shared and old_lines[prefix] == new_lines[prefix]:
        _check_impact_stop(deadline)
        prefix += 1
    if prefix == len(old_lines) == len(new_lines):
        return []
    suffix = 0
    while (
        suffix < len(old_lines) - prefix
        and suffix < len(new_lines) - prefix
        and old_lines[len(old_lines) - suffix - 1]
        == new_lines[len(new_lines) - suffix - 1]
    ):
        _check_impact_stop(deadline)
        suffix += 1
    old_end = len(old_lines) - suffix
    new_end = len(new_lines) - suffix
    return [
        {
            "old": {
                "line_start": prefix + 1,
                "line_end": max(prefix + 1, old_end),
                "byte_start": old_offsets[prefix],
                "byte_end": old_offsets[old_end],
            },
            "new": {
                "line_start": prefix + 1,
                "line_end": max(prefix + 1, new_end),
                "byte_start": new_offsets[prefix],
                "byte_end": new_offsets[new_end],
            },
        }
    ]


def _active_graph(root: Path, deadline: float):
    try:
        from evidence_graph import EvidenceGraph
        from generation_catalog import GenerationCatalog
        from repository_scope import resolve_repository_scope

        state_root = STATE_ROOT
        catalog_path = state_root / "cache" / "evidence-graph" / "catalog.sqlite3"
        if not catalog_path.is_file():
            return None
        scope = resolve_repository_scope(root, deadline=deadline)
        return EvidenceGraph.open_active_for_repository(
            GenerationCatalog(state_root, catalog_path=catalog_path),
            scope,
            deadline=deadline,
        )
    except TimeoutError:
        raise
    except (OSError, PermissionError, TypeError, ValueError):
        return None


def _overlaps(occurrence: dict, changed: dict) -> bool:
    byte_start = int(changed["byte_start"])
    byte_end = int(changed["byte_end"])
    if byte_end > byte_start:
        return int(occurrence["byte_start"]) < byte_end and int(occurrence["byte_end"]) > byte_start
    line = int(changed["line_start"])
    return int(occurrence["line_start"]) <= line <= int(occurrence["line_end"])


def _symbol_record(node: dict, occurrence: dict, side: str, changed: dict) -> dict:
    metadata = node.get("metadata", {})
    return {
        "node_id": node["node_id"],
        "name": metadata.get("name", node.get("identity_key", "")),
        "kind": node["kind"],
        "path": occurrence["relative_path"],
        "sides": [side],
        "classification": "exact",
        "evidence": {
            "path": occurrence["relative_path"],
            "line_start": occurrence["line_start"],
            "line_end": occurrence["line_end"],
            "changed_range": changed,
        },
    }


def _map_symbols(graph, changes: list[dict], bounds: ImpactLimits, deadline: float) -> list[dict]:
    symbols: dict[str, dict] = {}
    for change in changes:
        for changed_range in change["ranges"]:
            for side in ("old", "new"):
                if time.monotonic() >= deadline:
                    raise TimeoutError("impact analysis deadline reached")
                if change[f"{side}_blob"] is None:
                    continue
                path = change[f"{side}_path"]
                if not path or Path(path).suffix.lower() not in CODE_EXTENSIONS:
                    continue
                nodes = graph.find_nodes(path=path, max_rows=bounds.max_graph_rows, deadline=deadline)
                for node in nodes:
                    if node["kind"] not in {"symbol", "function", "method", "class"}:
                        continue
                    occurrences = graph.occurrences(
                        node["node_id"], max_rows=32, deadline=deadline
                    )
                    for occurrence in occurrences:
                        if occurrence["relative_path"] != path or not _overlaps(
                            occurrence, changed_range[side]
                        ):
                            continue
                        existing = symbols.get(node["node_id"])
                        if existing is None:
                            symbols[node["node_id"]] = _symbol_record(
                                node, occurrence, side, changed_range[side]
                            )
                        elif side not in existing["sides"]:
                            existing["sides"].append(side)
                        if len(symbols) > bounds.max_symbols:
                            raise ValueError("changed symbol ceiling exceeded")
                        break
    return sorted(symbols.values(), key=lambda item: (item["path"], item["name"], item["node_id"]))


def _project_file_ids(graph, changes: list[dict], bounds: ImpactLimits, deadline: float) -> set[str]:
    """Resolve project-journal file values before following checkpoint edges."""
    paths = {
        str(change[key])
        for change in changes
        for key in ("old_path", "new_path")
        if change.get(key)
    }
    if not paths:
        return set()
    nodes = graph.find_nodes(kinds=("file",), max_rows=bounds.max_graph_rows, deadline=deadline)
    return {
        str(node["node_id"])
        for node in nodes
        if str(node.get("metadata", {}).get("value", "")).replace("\\", "/") in paths
    }


def _edge_evidence(graph, assertion_id: str, bounds: ImpactLimits, deadline: float) -> list[dict]:
    try:
        rows = graph.evidence(assertion_id=assertion_id, max_rows=8, deadline=deadline)
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return []
    return [
        {
            "path": row["relative_path"],
            "line_start": row["line_start"],
            "line_end": row["line_end"],
            "assertion_id": assertion_id,
        }
        for row in rows
    ]


def _affected_nodes(graph, symbol_ids: set[str], bounds: ImpactLimits, deadline: float) -> dict:
    groups = {"decisions": [], "pages": [], "tests": [], "checkpoints": []}
    edges = graph.edges(
        edge_types=tuple(sorted(TRAVERSED_EDGES)),
        max_rows=bounds.max_graph_rows,
        deadline=deadline,
    )
    reached = set(symbol_ids)
    used: dict[str, dict] = {}
    for _depth in range(bounds.max_depth):
        added = False
        for edge in edges:
            if edge.get("confidence") not in CONFIRMED_CONFIDENCE:
                continue
            if edge["target_node_id"] not in reached or edge["source_node_id"] in reached:
                continue
            reached.add(edge["source_node_id"])
            used[edge["source_node_id"]] = edge
            added = True
        if not added:
            break
    seen = set()
    for node_id, edge in used.items():
        node = graph.node(node_id)
        if node is None:
            continue
        metadata = node.get("metadata", {})
        path = str(metadata.get("path", node.get("identity_key", "")))
        group = None
        if node["kind"] == "decision":
            group = "decisions"
        elif node["kind"] in {"knowledge-page", "debugging-note"}:
            group = "pages"
        elif node["kind"] == "checkpoint":
            group = "checkpoints"
        elif (
            Path(path).name.startswith("test_")
            or "tests" in Path(path).parts
            or str(metadata.get("name", "")).startswith("test_")
        ):
            group = "tests"
        if group is None or (group, node_id) in seen:
            continue
        seen.add((group, node_id))
        groups[group].append(
            {
                "node_id": node_id,
                "kind": node["kind"],
                "path": path,
                "name": metadata.get("name", node.get("identity_key", "")),
                "classification": "exact",
                "via": edge["edge_type"],
                "evidence": _edge_evidence(graph, edge["assertion_id"], bounds, deadline),
            }
        )
    for values in groups.values():
        values.sort(key=lambda item: (item["path"], item["name"], item["node_id"]))
    return groups


def analyze_impact(
    git_range: str | None = None,
    *,
    root: Path = ROOT,
    comparison: str = "dirty",
    base: str | None = None,
    target: str | None = None,
    branch: str | None = None,
    graph=None,
    limits: ImpactLimits | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    """Map explicit Git endpoints through canonical graph symbols and edges."""
    bounds = limits or ImpactLimits()
    if git_range is not None:
        match = re.fullmatch(r"([^.]\S*)\.\.([^.]\S*)", git_range)
        if match is None:
            raise ValueError("legacy git_range must contain exactly two commit endpoints")
        comparison, base, target = "two-commits", match.group(1), match.group(2)
    root = Path(root).resolve(strict=True)
    deadline = monotonic() + bounds.timeout_seconds if deadline is None else deadline
    _check_impact_stop(deadline, cancelled)
    warnings: list[str] = []
    partial = False
    try:
        changes = collect_git_changes(
            root,
            comparison=comparison,
            base=base,
            target=target,
            branch=branch,
            limits=bounds,
            deadline=deadline,
        )
    except (InvalidRevisionError, TimeoutError):
        raise
    except ValueError as exc:
        changes = []
        warnings.append(str(exc))
        partial = True
    public_changes = []
    textual_names = set()
    for change in changes:
        _check_impact_stop(deadline, cancelled)
        ranges = _changed_ranges(
            change["old_blob"], change["new_blob"], deadline=deadline
        )
        change["ranges"] = ranges
        textual_names.update(_textual_symbols(change["old_blob"]))
        textual_names.update(_textual_symbols(change["new_blob"]))
        public_changes.append(
            {
                key: change[key]
                for key in (
                    "comparison",
                    "status",
                    "similarity",
                    "old_path",
                    "new_path",
                    "old_oid",
                    "new_oid",
                    "ranges",
                )
            }
        )
    selected_graph = graph if graph is not None else _active_graph(root, deadline)
    owns_graph = graph is None and selected_graph is not None
    changed_symbols = []
    affected = {"decisions": [], "pages": [], "tests": [], "checkpoints": []}
    generation_id = getattr(selected_graph, "generation_id", None)
    if selected_graph is None:
        if changes:
            warnings.append("No valid active Evidence Graph generation is available.")
    else:
        try:
            changed_symbols = _map_symbols(selected_graph, changes, bounds, deadline)
            affected = _affected_nodes(
                selected_graph,
                {item["node_id"] for item in changed_symbols}
                | _project_file_ids(selected_graph, changes, bounds, deadline),
                bounds,
                deadline,
            )
            if any(not item["evidence"] for values in affected.values() for item in values):
                warnings.append("One or more resolved impact edges have no source evidence path.")
                partial = True
            textual_names.update(str(item["name"]) for item in changed_symbols)
            if changes and not changed_symbols:
                warnings.append("Changed ranges did not resolve to canonical symbols in the active graph.")
                partial = True
        except TimeoutError:
            raise
        except (OSError, ValueError) as exc:
            warnings.append(str(exc))
            partial = True
        finally:
            if owns_graph:
                selected_graph.close()
    fallback = find_stale_wiki_pages(
        sorted(textual_names),
        limits=bounds,
        deadline=deadline,
        cancelled=cancelled,
    )
    for item in fallback:
        item["confidence"] = "low"
        item["method"] = "textual-name-match"
        item["classification"] = "conservative"
    fallback = apply_significance_budget(fallback)
    if selected_graph is None and changes:
        classification = "unresolved"
    elif warnings or partial:
        classification = "conservative"
    elif changes and changed_symbols:
        classification = "exact"
    elif changes:
        classification = "unresolved"
    else:
        classification = "exact"
    summary = (
        f"{len(public_changes)} diff record(s), {len(changed_symbols)} canonical symbol(s), "
        f"{sum(len(values) for values in affected.values())} affected artifact(s)."
    )
    return {
        "comparison": comparison,
        "generation_id": generation_id,
        "classification": classification,
        "partial": partial,
        "warnings": warnings,
        "changes": public_changes,
        "changed_files": sorted(
            {str(item["new_path"] or item["old_path"]) for item in public_changes}
        ),
        "changed_symbols": changed_symbols,
        "affected": affected,
        "textual_fallback": fallback,
        "stale_pages": fallback,
        "summary": summary,
    }


def format_for_advisory(impact: dict, max_pages: int = 3) -> str:
    """Format the compatibility textual fallback for SessionStart."""
    stale = impact.get("stale_pages", [])
    if not stale:
        return ""
    lines = ["### Code-Knowledge Impact", impact["summary"], ""]
    for page in stale[:max_pages]:
        marker = "!!!" if page["confidence"] == "high" else "!"
        lines.append(f"{marker} **{page['slug']}** - {page['reason']}")
    if len(stale) > max_pages:
        lines.append(f"... and {len(stale) - max_pages} more.")
    return "\n".join(lines)


def main() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Diff-to-Evidence-Graph impact analysis")
    parser.add_argument("--comparison", choices=sorted(COMPARISONS), default="dirty")
    parser.add_argument("--base")
    parser.add_argument("--target")
    parser.add_argument("--branch")
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args()
    try:
        impact = analyze_impact(
            comparison=arguments.comparison,
            base=arguments.base,
            target=arguments.target,
            branch=arguments.branch,
        )
    except (OSError, TimeoutError, ValueError) as exc:
        parser.error(str(exc))
    if arguments.json:
        print(json.dumps(impact, indent=2, ensure_ascii=False, allow_nan=False))
    else:
        print(impact["summary"])
        print(f"Classification: {impact['classification']}")
        for group, values in impact["affected"].items():
            if values:
                print(f"{group}: {', '.join(str(item['name']) for item in values)}")
        if impact["warnings"]:
            print("Warnings: " + "; ".join(impact["warnings"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
