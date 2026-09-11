"""L0/L1/L2 tiered knowledge loading — progressive disclosure.

Generates multi-level summaries for each knowledge page so agents can
load the right amount of context without overpaying for tokens.

Levels (OpenViking model):
- L0: one-sentence summary (~100 tokens) — quick relevance check
- L1: structured overview (~500-1000 tokens) — planning decisions
- L2: full page content — deep reading (already exists in Markdown)

L0 already exists as the "One-sentence summary:" line in each page.
This module generates L1 overviews via LLM, cached in cache/tiers/.

The SessionStart advisory (build_advisory.py) can use L0 to decide
which pages to inject, then pull L1 for the top candidates, and only
read L2 (full page) when truly needed — cutting token usage 50-90%.

Usage:
    uv run python scripts/build_tiers.py              # generate L1 for all pages
    uv run python scripts/build_tiers.py --slug auth  # generate for one page
    uv run python scripts/build_tiers.py --status     # show cache stats
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT, STATE_ROOT, atomic_write  # noqa: E402

if TYPE_CHECKING:
    from corpus_snapshot import CapturedSource, CorpusSnapshot

KNOWLEDGE_DIR = ROOT / "knowledge" / "notes"
TIERS_DIR = STATE_ROOT / "cache" / "tiers"
SKIP_NAMES = {"index.md", "log.md", "README.md", "state.md", "context.md"}

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
SUMMARY_RE = re.compile(
    r"^One-sentence summary:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE
)
STATUS_RE = re.compile(r"^status:\s*(.+?)\s*$", re.MULTILINE)
TIER_EXTRACTOR_VERSION = "tier-extractor/v1"
TIER_ARTIFACT_SCHEMA_VERSION = "tier-artifact/v1"
MAX_TIER_SOURCES = 10_000
MAX_TIER_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_MODEL_DESCRIPTOR_BYTES = 16 * 1024


def _validate_source(source: CapturedSource) -> CapturedSource:
    from corpus_snapshot import CapturedSource

    if not isinstance(source, CapturedSource):
        raise TypeError("source must be a CapturedSource")
    digest = hashlib.sha256(source.content).hexdigest()
    if source.record.size != len(source.content) or source.record.sha256 != digest:
        raise ValueError("captured source content does not match its record")
    return source


_LINE_BREAKING = frozenset("\x00\r\n")


def _bounded_text(value: object) -> bool:
    """A non-empty single-line string of at most 128 characters."""
    if not isinstance(value, str) or not value:
        return False
    return len(value) <= 128 and _LINE_BREAKING.isdisjoint(value)


def _extractor_version(value: str) -> str:
    if not _bounded_text(value):
        raise ValueError("extractor_version must be a bounded non-empty string")
    return value


def _bounded_model_value(value: object, label: str) -> str:
    if not _bounded_text(value):
        raise ValueError(f"{label} must be a bounded non-empty string")
    return value


def _model_provenance(
    use_llm: bool, model_descriptor: object | None, model_revision: str | None
) -> dict[str, object] | None:
    if not use_llm:
        _require_no_model(model_descriptor, model_revision)
        return None
    if model_descriptor is None or model_revision is None:
        raise ValueError("LLM generation requires a model descriptor and revision")
    return _bounded_provenance(model_descriptor, model_revision)


def _require_no_model(model_descriptor: object | None, model_revision: str | None) -> None:
    if model_descriptor is not None or model_revision is not None:
        raise ValueError("model descriptor and revision require LLM generation")


def _bounded_provenance(model_descriptor: object, model_revision: str) -> dict[str, object]:
    from llm_client import ProviderDescriptor

    if not isinstance(model_descriptor, ProviderDescriptor):
        raise TypeError("model_descriptor must be a ProviderDescriptor")
    _bounded_model_value(model_descriptor.provider, "model provider")
    _bounded_model_value(model_descriptor.model, "model name")
    revision = _bounded_model_value(model_revision, "model revision")
    provenance = {**model_descriptor.canonical(), "revision": revision}
    encoded = json.dumps(
        provenance, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_MODEL_DESCRIPTOR_BYTES:
        raise ValueError("model descriptor exceeds the supported bound")
    return provenance


def _captured_text(source: CapturedSource) -> str:
    return _validate_source(source).captured_bytes.decode("utf-8", errors="strict")


def _fsync_directory(path: Path) -> None:
    from reliable_memory import fsync_directory

    fsync_directory(path)


def get_l0_for_source(source: CapturedSource) -> str:
    """Get L0 from immutable captured source bytes without live filesystem I/O."""
    body = FRONTMATTER_RE.sub("", _captured_text(source), count=1)
    l0 = _body_l0(body)
    if l0 is not None:
        return l0
    return Path(source.record.relative_path).stem.replace("-", " ")


def _body_l0(body: str) -> str | None:
    """The page's one-sentence summary, else its first prose line after the H1."""
    match = SUMMARY_RE.search(body)
    if match:
        return match.group(1).strip()
    return _first_prose_line(body)


def _first_prose_line(body: str) -> str | None:
    for line in body.splitlines()[1:]:
        stripped = line.strip()
        if _is_prose(stripped):
            return stripped
    return None


def _is_prose(stripped: str) -> bool:
    return bool(stripped) and not stripped.startswith("#") and not stripped.startswith("---")


def generate_l1_for_source(
    source: CapturedSource,
    use_llm: bool = False,
    *,
    model_descriptor: object | None = None,
    model_revision: str | None = None,
) -> str:
    """Generate L1 using only immutable captured source bytes."""
    content = _captured_text(source)
    body = FRONTMATTER_RE.sub("", content, count=1)
    l0 = get_l0_for_source(source)
    slug = Path(source.record.relative_path).stem
    if not use_llm:
        _model_provenance(False, model_descriptor, model_revision)
        return _deterministic_l1(slug, body, l0)

    _model_provenance(True, model_descriptor, model_revision)
    from llm_client import call_candidate

    prompt = f"""Summarize this knowledge page into a structured overview.
Keep it under 500 words. Include:
- Key points (bulleted)
- Important decisions or constraints
- Links to related concepts

=== PAGE ===
{body}

=== OUTPUT ===
Return ONLY the overview markdown (no title, no commentary).
"""
    result = call_candidate(
        model_descriptor,
        prompt,
        "You are a knowledge summarizer.",
        max_tokens=1000,
    ).text
    if not result or not result.strip():
        raise RuntimeError("LLM returned no L1 overview")
    return result.strip()


def get_l2_for_source(source: CapturedSource) -> str:
    """Get L2 directly from immutable captured source bytes."""
    return _captured_text(source)


def tier_artifact_key(
    source: CapturedSource,
    *,
    extractor_version: str = TIER_EXTRACTOR_VERSION,
    model_descriptor: object | None = None,
    model_revision: str | None = None,
    generated_l1: str | None = None,
) -> str:
    """Return the content/version-bound identity for one source's tier data."""
    source = _validate_source(source)
    version = _extractor_version(extractor_version)
    use_llm = any(value is not None for value in (model_descriptor, model_revision, generated_l1))
    model = _model_provenance(use_llm, model_descriptor, model_revision)
    if use_llm and not isinstance(generated_l1, str):
        raise ValueError("LLM artifact identity requires generated L1 bytes")
    identity = json.dumps(
        [
            source.record.relative_path,
            source.record.sha256,
            version,
            "llm" if use_llm else "deterministic",
            model,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def _tier_entry(
    source: CapturedSource,
    key: str,
    tiers: dict[str, str],
) -> dict[str, object]:
    metadata = source.metadata
    return {
        "key": key,
        "source": {
            "authority": metadata.authority,
            "confidence": metadata.confidence,
            "git_oid": source.record.git_oid,
            "language": source.record.language,
            "logical_id": source.record.logical_id,
            "media_type": source.record.media_type,
            "project": metadata.project,
            "relative_path": source.record.relative_path,
            "sha256": source.record.sha256,
            "size": source.record.size,
            "status": metadata.status,
            "type": metadata.type,
            "valid_from": metadata.valid_from,
            "valid_to": metadata.valid_to,
        },
        "tiers": tiers,
    }


def _tier_artifact_bytes(
    entries: list[dict[str, object]],
    extractor_version: str,
    model: dict[str, object] | None,
) -> bytes:
    payload = {
        "entries": entries,
        "extractor_version": extractor_version,
        "generation": {"mode": "llm" if model is not None else "deterministic", "model": model},
        "schema_version": TIER_ARTIFACT_SCHEMA_VERSION,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_TIER_ARTIFACT_BYTES:
        raise ValueError("tier artifact exceeds the supported bound")
    return encoded


@dataclass(frozen=True)
class _TierRequest:
    """How every source of one snapshot gets its tiers."""

    use_llm: bool
    version: str
    model_descriptor: object | None
    model_revision: str | None


def build_snapshot_tiers(
    snapshot: CorpusSnapshot,
    generation_dir: Path,
    *,
    use_llm: bool = False,
    extractor_version: str = TIER_EXTRACTOR_VERSION,
    model_descriptor: object | None = None,
    model_revision: str | None = None,
) -> list[dict[str, object]]:
    """Build deterministic tier artifacts for exactly one immutable snapshot."""
    _require_bounded_snapshot(snapshot)
    version = _extractor_version(extractor_version)
    model = _model_provenance(use_llm, model_descriptor, model_revision)
    generation = Path(generation_dir)
    output = _unpublished_tier_output(generation)
    request = _TierRequest(use_llm, version, model_descriptor, model_revision)
    staging = Path(tempfile.mkdtemp(prefix=".tiers-", dir=generation))
    try:
        entries = _tier_entries(snapshot.sources, request)
        content = _tier_artifact_bytes(entries, version, model)
        descriptors = _staged_tier_artifact(staging, content)
        _publish_tiers(staging, output, generation)
        return descriptors
    finally:
        _remove_staging(staging)


def _require_bounded_snapshot(snapshot: object) -> None:
    from corpus_snapshot import CorpusSnapshot

    if not isinstance(snapshot, CorpusSnapshot):
        raise TypeError("snapshot must be a CorpusSnapshot")
    if len(snapshot.sources) > MAX_TIER_SOURCES:
        raise ValueError("snapshot has too many sources for tier artifacts")


def _unpublished_tier_output(generation: Path) -> Path:
    metadata = generation.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or generation.is_symlink():
        raise ValueError("generation_dir must be a regular unpublished directory")
    output = generation / "tiers"
    if output.exists() or output.is_symlink():
        raise FileExistsError("tier output already exists")
    return output


def _tier_entries(sources: object, request: _TierRequest) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    seen_sources: set[str] = set()
    for source in sources:
        source = _validate_source(source)
        _claim_logical_id(seen_sources, source.record.logical_id)
        entries.append(_source_tier_entry(source, request))
    entries.sort(key=lambda item: str(item["source"]["logical_id"]))  # type: ignore[index]
    return entries


def _claim_logical_id(seen_sources: set[str], logical_id: str) -> None:
    if logical_id in seen_sources:
        raise ValueError("snapshot contains duplicate logical source IDs")
    seen_sources.add(logical_id)


def _source_tier_entry(source: CapturedSource, request: _TierRequest) -> dict[str, object]:
    l1 = generate_l1_for_source(
        source,
        use_llm=request.use_llm,
        model_descriptor=request.model_descriptor,
        model_revision=request.model_revision,
    )
    key = tier_artifact_key(
        source,
        extractor_version=request.version,
        model_descriptor=request.model_descriptor,
        model_revision=request.model_revision,
        generated_l1=l1 if request.use_llm else None,
    )
    tiers = {
        "l0": get_l0_for_source(source),
        "l1": l1,
        "l2": get_l2_for_source(source),
    }
    return _tier_entry(source, key, tiers)


def _staged_tier_artifact(staging: Path, content: bytes) -> list[dict[str, object]]:
    name = "tiers.json"
    with (staging / name).open("xb") as artifact:
        artifact.write(content)
        artifact.flush()
        os.fsync(artifact.fileno())
    _fsync_directory(staging)
    return [
        {
            "path": f"tiers/{name}",
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    ]


def _publish_tiers(staging: Path, output: Path, generation: Path) -> None:
    """Rename the staged tiers into place; withdraw them if the rename cannot be made durable."""
    if output.exists() or output.is_symlink():
        raise FileExistsError("tier output already exists")
    staging.replace(output)
    try:
        _fsync_directory(generation)
    except BaseException:
        _withdraw_tiers(output, generation)
        raise


def _withdraw_tiers(output: Path, generation: Path) -> None:
    if not output.exists() or output.is_symlink():
        return
    shutil.rmtree(output)
    try:
        _fsync_directory(generation)
    except OSError:
        pass


def _remove_staging(staging: Path) -> None:
    if staging.exists():
        shutil.rmtree(staging)


def get_l0(slug: str) -> str:
    """Get L0 (one-sentence summary) for a page. ~100 tokens.

    Reads from the page's 'One-sentence summary:' line.
    Falls back to first sentence of body or slug.
    """
    page_path = KNOWLEDGE_DIR / f"{slug}.md"
    if not page_path.exists():
        return ""
    content = page_path.read_text(encoding="utf-8", errors="ignore")
    l0 = _body_l0(FRONTMATTER_RE.sub("", content, count=1))
    if l0 is not None:
        return l0
    return slug.replace("-", " ")


def get_l1(
    slug: str,
    source_sha256: str | None = None,
    *,
    extractor_version: str = TIER_EXTRACTOR_VERSION,
    logical_path: str | None = None,
) -> str | None:
    """Get L1 (structured overview) for a page. ~500-1000 tokens.

    Hash-qualified reads fail closed and never consult an unqualified legacy file.

    The hash suffix is derived from the source SHA-256 and extractor version
    so cache invalidation honors Task 15's logical-path-plus-source-hash
    contract.
    """
    if source_sha256:
        hashed_path = tier_legacy_cache_path(
            slug,
            source_sha256=source_sha256,
            extractor_version=extractor_version,
            logical_path=logical_path,
        )
        if hashed_path.exists():
            return hashed_path.read_text(encoding="utf-8", errors="ignore")
        return None
    legacy_path = tier_legacy_cache_path(slug)
    if not legacy_path.exists():
        return None
    return legacy_path.read_text(encoding="utf-8", errors="ignore")


def tier_legacy_cache_path(
    slug: str,
    *,
    source_sha256: str | None = None,
    extractor_version: str = TIER_EXTRACTOR_VERSION,
    logical_path: str | None = None,
    generation_mode: str = "deterministic",
    model_descriptor: object | None = None,
    model_revision: str | None = None,
) -> Path:
    """Return the legacy mutable-cache path for an L1 overview.

    When ``source_sha256`` is provided the path is suffixed with a short hash
    of (source SHA-256, extractor version) so stale caches invalidate on
    content or extractor changes.
    """
    safe_slug = _safe_cache_slug(slug)
    if generation_mode not in {"deterministic", "llm"}:
        raise ValueError("generation_mode must be 'deterministic' or 'llm'")
    model = _model_provenance(
        generation_mode == "llm", model_descriptor, model_revision
    )
    if not source_sha256:
        if generation_mode == "llm":
            raise ValueError("LLM cache identity requires source_sha256")
        return TIERS_DIR / f"{safe_slug}.l1.md"
    logical = _safe_logical_path(logical_path or f"{slug}.md")
    identity = json.dumps(
        [logical, source_sha256, extractor_version, generation_mode, model],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    suffix = hashlib.sha256(identity).hexdigest()[:20]
    return TIERS_DIR / f"{safe_slug}.{suffix}.l1.md"


def _safe_cache_slug(value: str) -> str:
    _safe_logical_path(f"{value}.md")
    return value


_RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)}
)
_FORBIDDEN_PART_CHARACTERS = frozenset("\\:\x00")


def _safe_logical_path(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    posix = PurePosixPath(normalized)
    if _unsafe_path_text(value, normalized, posix) or _unsafe_path_parts(posix.parts):
        raise ValueError("logical_path must be a normalized safe relative path")
    return posix.as_posix()


def _unsafe_path_text(value: str, normalized: str, posix: PurePosixPath) -> bool:
    if not value or normalized != value:
        return True
    return _absolute_path_text(normalized, posix) or posix.as_posix() != value


def _absolute_path_text(normalized: str, posix: PurePosixPath) -> bool:
    windows = PureWindowsPath(normalized)
    return posix.is_absolute() or windows.is_absolute() or bool(windows.drive)


def _unsafe_path_parts(parts: tuple[str, ...]) -> bool:
    return any(_unsafe_path_part(part) for part in parts)


def _unsafe_path_part(part: str) -> bool:
    if part in {"", ".", ".."} or part.rstrip(". ") != part:
        return True
    return _reserved_device_part(part) or not _FORBIDDEN_PART_CHARACTERS.isdisjoint(part)


def _reserved_device_part(part: str) -> bool:
    return PurePosixPath(part).stem.casefold() in _RESERVED_DEVICE_NAMES


def get_l2(slug: str) -> str:
    """Get L2 (full page content). Just reads the markdown file."""
    page_path = KNOWLEDGE_DIR / f"{slug}.md"
    if not page_path.exists():
        return ""
    return page_path.read_text(encoding="utf-8", errors="ignore")


def _needs_l1_regeneration(
    slug: str,
    page_path: Path,
    source_sha256: str | None = None,
    *,
    extractor_version: str = TIER_EXTRACTOR_VERSION,
    logical_path: str | None = None,
) -> bool:
    """Check if L1 needs to be (re)generated for this page."""
    l1_path = tier_legacy_cache_path(
        slug,
        source_sha256=source_sha256,
        extractor_version=extractor_version,
        logical_path=logical_path,
    )
    if not l1_path.exists():
        return True
    # Check if page changed since L1 was generated.
    try:
        return page_path.stat().st_mtime > l1_path.stat().st_mtime
    except OSError:
        return True


def generate_l1(
    slug: str,
    use_llm: bool = True,
    source_sha256: str | None = None,
    *,
    extractor_version: str = TIER_EXTRACTOR_VERSION,
) -> str | None:
    """Generate L1 overview for a page.

    If use_llm=True and LLM available: LLM generates a structured overview.
    If use_llm=False or no LLM: deterministic extraction (first N paragraphs).

    When ``source_sha256`` is supplied, the cache file is hash-suffixed so
    stale overviews cannot survive a content change (Task 15 contract).
    """
    page_path = KNOWLEDGE_DIR / f"{slug}.md"
    if not page_path.exists():
        return None
    content = page_path.read_text(encoding="utf-8", errors="ignore")
    body = FRONTMATTER_RE.sub("", content, count=1)
    l0 = get_l0(slug)
    if not use_llm or os_env_fake():
        return _deterministic_l1(slug, body, l0)
    return _llm_l1(slug, body, l0)


def _llm_l1(slug: str, body: str, l0: str) -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from llm_client import call_llm
    except ImportError:
        return _deterministic_l1(slug, body, l0)
    prompt = f"""Summarize this knowledge page into a structured overview.
Keep it under 500 words. Include:
- Key points (bulleted)
- Important decisions or constraints
- Links to related concepts

=== PAGE ===
{body}

=== OUTPUT ===
Return ONLY the overview markdown (no title, no commentary).
"""
    result = call_llm(prompt, "You are a knowledge summarizer.", max_tokens=1000)
    if not result or not result.strip():
        return _deterministic_l1(slug, body, l0)
    return result.strip()


def write_l1(
    slug: str,
    l1_text: str,
    *,
    source_sha256: str | None = None,
    extractor_version: str = TIER_EXTRACTOR_VERSION,
    logical_path: str | None = None,
) -> Path:
    """Persist an L1 overview to the (optionally hash-suffixed) legacy cache."""
    target = tier_legacy_cache_path(
        slug,
        source_sha256=source_sha256,
        extractor_version=extractor_version,
        logical_path=logical_path,
    )
    atomic_write(target, l1_text)
    return target


def _deterministic_l1(slug: str, body: str, l0: str) -> str:
    """Generate L1 without LLM — extract first sections."""
    overview_lines = [l0, ""]
    char_count = len(l0)
    for line in _overview_candidates(body):
        # Check limit BEFORE adding.
        if char_count + len(line) >= 2000:
            overview_lines.append("\n...(truncated, see full page for more)")
            break
        char_count += _append_overview_line(overview_lines, line)
    return "\n".join(overview_lines)


def _overview_candidates(body: str):
    """Non-blank body lines after the H1, stopping at the History section."""
    for line in body.splitlines()[1:]:
        stripped = line.strip()
        if stripped.startswith("## History"):
            return
        if stripped:
            yield line


def _append_overview_line(overview_lines: list[str], line: str) -> int:
    """Append one line (a section heading gets a blank line before it); its counted length."""
    stripped = line.strip()
    if stripped.startswith("## "):
        overview_lines.extend(("", stripped))
        return len(stripped)
    overview_lines.append(line)
    return len(line)


def os_env_fake() -> bool:
    """Check if running with fake LLM provider (tests)."""
    import os
    return os.environ.get("MEMORY_LLM_PROVIDER", "").lower() == "fake"


def build_all_tiers(use_llm: bool = False, verbose: bool = True) -> dict:
    """Generate deterministic L1 overviews for all pages that need it.

    Returns stats: {generated, skipped, errors}

    Each cache entry is keyed by source SHA-256 + extractor version. Legacy
    batch LLM generation fails closed because it cannot accept complete model
    provenance; snapshot generation remains the provenance-aware interface.
    """
    if use_llm:
        raise ValueError(
            "LLM generation requires an explicit model descriptor and revision; "
            "legacy tier batch generation is unavailable"
        )
    TIERS_DIR.mkdir(parents=True, exist_ok=True)
    if not KNOWLEDGE_DIR.exists():
        return {"generated": 0, "skipped": 0, "errors": 0}
    return _build_page_tiers(verbose)


def _build_page_tiers(verbose: bool) -> dict:
    stats = {"generated": 0, "skipped": 0, "errors": 0}
    for md in sorted(KNOWLEDGE_DIR.rglob("*.md")):
        if md.name in SKIP_NAMES or "archive" in md.parts:
            continue
        stats[_build_page_tier(md, verbose)] += 1
    if verbose:
        print(f"\nL1 tier generation: {stats['generated']} generated, "
              f"{stats['skipped']} skipped, {stats['errors']} errors.")
    return stats


def _build_page_tier(md: Path, verbose: bool) -> str:
    """The stats bucket of one page: generated, skipped or errors."""
    try:
        captured = md.read_bytes()
    except OSError:
        return "errors"
    content = captured.decode("utf-8", errors="strict")
    if "status: superseded" in content or "status: archived" in content:
        return "skipped"
    return _refresh_page_tier(md, captured, content, verbose)


def _refresh_page_tier(md: Path, captured: bytes, content: str, verbose: bool) -> str:
    slug = md.stem
    source_sha256 = hashlib.sha256(captured).hexdigest()
    logical_path = md.relative_to(KNOWLEDGE_DIR).as_posix()
    if not _needs_l1_regeneration(slug, md, source_sha256, logical_path=logical_path):
        return "skipped"
    try:
        _write_page_l1(slug, content, source_sha256, logical_path)
        _announce_generated(slug, verbose)
    except Exception:
        return "errors"
    return "generated"


def _write_page_l1(slug: str, content: str, source_sha256: str, logical_path: str) -> None:
    body = FRONTMATTER_RE.sub("", content, count=1)
    summary_match = SUMMARY_RE.search(body)
    l0 = summary_match.group(1).strip() if summary_match else slug.replace("-", " ")
    write_l1(
        slug,
        _deterministic_l1(slug, body, l0),
        source_sha256=source_sha256,
        logical_path=logical_path,
    )


def _announce_generated(slug: str, verbose: bool) -> None:
    if verbose:
        print(f"  Generated L1: {slug}")


def get_tier(slug: str, level: str = "auto") -> dict:
    """Get content at the specified tier level.

    Args:
        slug: Page slug.
        level: 'l0', 'l1', 'l2', or 'auto' (returns l0 + l1 if available).

    Returns:
        Dict with level, content, and available levels.
    """
    l0 = get_l0(slug)
    l1 = get_l1(slug)
    l2 = get_l2(slug)
    view = _TIER_VIEWS.get(level, _auto_tier)
    return view(l0, l1, l2)


def _levels_through_l1(l1: str | None) -> list[str]:
    return ["l0", "l1"] if l1 else ["l0"]


def _l0_tier(l0: str, l1: str | None, l2: str) -> dict:
    return {"level": "l0", "content": l0, "available": ["l0"]}


def _l1_tier(l0: str, l1: str | None, l2: str) -> dict:
    return {"level": "l1", "content": l1 or l0, "available": _levels_through_l1(l1)}


def _l2_tier(l0: str, l1: str | None, l2: str) -> dict:
    return {"level": "l2", "content": l2, "available": [*_levels_through_l1(l1), "l2"]}


def _auto_tier(l0: str, l1: str | None, l2: str) -> dict:
    return {
        "level": "l1" if l1 else "l0",
        "content": l1 or l0,
        "l0": l0,
        "available": _levels_through_l1(l1) + (["l2"] if l2 else []),
    }


_TIER_VIEWS = {"l0": _l0_tier, "l1": _l1_tier, "l2": _l2_tier}


def main() -> int:
    parser = _argument_parser()
    args = parser.parse_args()
    if args.llm:
        parser.error("--llm is unavailable without explicit model descriptor and revision")
    for flag, action in (("status", _print_status), ("slug", _generate_one), ("get", _print_tier)):
        if getattr(args, flag):
            return action(args)
    build_all_tiers(use_llm=False)
    return 0


def _argument_parser():
    import argparse

    p = argparse.ArgumentParser(description="L0/L1/L2 tiered knowledge loading.")
    p.add_argument("--slug", type=str, default=None, help="Generate L1 for one page.")
    p.add_argument("--all", action="store_true", help="Generate L1 for all pages.")
    p.add_argument("--llm", action="store_true", help="Unavailable without explicit model provenance.")
    p.add_argument("--status", action="store_true", help="Show cache statistics.")
    p.add_argument("--get", type=str, default=None, help="Get content at tier level.")
    return p


def _print_status(args) -> int:
    if not TIERS_DIR.exists():
        print("No L1 cache. Run --all to generate.")
        return 0
    l1_files = list(TIERS_DIR.glob("*.l1.md"))
    print(f"L1 cache: {len(l1_files)} / {_tier_page_count()} pages")
    return 0


def _tier_page_count() -> int:
    if not KNOWLEDGE_DIR.exists():
        return 0
    return sum(
        1
        for page in KNOWLEDGE_DIR.rglob("*.md")
        if page.name not in SKIP_NAMES and "archive" not in page.parts
    )


def _generate_one(args) -> int:
    l1 = generate_l1(args.slug, use_llm=False)
    if not l1:
        print(f"Failed to generate L1 for {args.slug}.")
        return 0
    write_l1(args.slug, l1)
    print(f"Generated L1 for {args.slug}: {len(l1)} chars.")
    return 0


def _print_tier(args) -> int:
    result = get_tier(args.get)
    print(f"Level: {result['level']}")
    print(f"Available: {result['available']}")
    print(f"Content ({len(result['content'])} chars):")
    print(result['content'][:500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
