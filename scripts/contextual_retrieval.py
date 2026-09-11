"""Contextual Retrieval — prepend a short context to pages before indexing.

Anthropic technique (provider-agnostic): for each page, derive a one-line
context that disambiguates it. This context is added to the FTS5 index,
making search more precise (-49% retrieval failures per Anthropic data).

Example:
  Page: "# Auth Decision"
  Context: "Project: llm-wiki. Type: decision. Topic: Auth Decision."

The context is deterministic — project, type, title and summary. An
LLM-written context is refused until a frozen ablation shows it helps
(`use_llm=True` raises); the cache identity for that mode already exists so
its artifacts can never collide with deterministic ones.

The context is stored in cache/contextual/ and merged into the search
index at build time. No changes to Markdown source files.

Usage:
    uv run python scripts/contextual_retrieval.py --all     # deterministic for all
    uv run python scripts/contextual_retrieval.py --status   # show stats
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
from collections.abc import Mapping
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from corpus_snapshot import CapturedSource, CorpusSnapshot  # noqa: E402
from memory_state import ROOT, STATE_ROOT, atomic_write  # noqa: E402

KNOWLEDGE_DIR = ROOT / "knowledge" / "notes"
CONTEXT_DIR = STATE_ROOT / "cache" / "contextual"
SKIP_NAMES = {"index.md", "log.md", "README.md", "state.md", "context.md"}
CONTEXT_EXTRACTOR_VERSION = "context-extractor/v1"
MAX_CONTEXT_SOURCES = 1024
MAX_CONTEXT_CHARS = 16_384
MAX_CONTEXT_ARTIFACT_BYTES = 64 * 1024
MAX_LLM_PROMPT_BYTES = 1024 * 1024
MAX_LLM_PROMPT_CHARS = 1024 * 1024
MAX_LEGACY_SLUG_CHARS = 128
_WINDOWS_RESERVED = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
SUMMARY_RE = re.compile(r"^One-sentence summary:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
PROJECT_RE = re.compile(r"^project:\s*[\"']?([^\"'\n]+)[\"']?\s*$", re.MULTILINE)
TYPE_RE = re.compile(r"^type:\s*(.+?)\s*$", re.MULTILINE)


# --- filesystem checks -------------------------------------------------------------


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _is_real_file(metadata: os.stat_result) -> bool:
    return not _is_link_or_reparse(metadata) and stat.S_ISREG(metadata.st_mode)


def _is_real_directory(metadata: os.stat_result) -> bool:
    return not _is_link_or_reparse(metadata) and stat.S_ISDIR(metadata.st_mode)


def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _validate_existing_ancestors(path: Path, label: str) -> None:
    absolute = Path(os.path.abspath(path))
    for candidate in reversed((absolute, *absolute.parents)):
        metadata = _lstat_or_none(candidate)
        if metadata is not None and _is_link_or_reparse(metadata):
            raise PermissionError(f"unsafe {label} ancestor: {candidate}")


def _real_directory(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    _validate_existing_ancestors(absolute, label)
    metadata = _lstat_or_none(absolute)
    if metadata is None:
        raise ValueError(f"{label} must be an existing real directory")
    if not _is_real_directory(metadata):
        raise PermissionError(f"unsafe {label} ancestor: {absolute}")
    return absolute


# --- names and identities ------------------------------------------------------------


def _slug_shape_unsafe(value: str) -> bool:
    normalized = unicodedata.normalize("NFKC", value)
    return (
        not value
        or len(value) > MAX_LEGACY_SLUG_CHARS
        or normalized != value
        or value in {".", ".."}
        or value[-1] in {".", " "}
    )


def _slug_characters_unsafe(value: str) -> bool:
    return any(character in value for character in "/\\:\x00\r\n") or any(
        ord(character) < 32 for character in value
    )


def _names_windows_device(value: str) -> bool:
    windows = PureWindowsPath(value)
    return bool(windows.drive) or bool(windows.root) or value.split(".", 1)[0].casefold() in _WINDOWS_RESERVED


def _legacy_slug(value: str) -> str:
    if _slug_shape_unsafe(value) or _slug_characters_unsafe(value) or _names_windows_device(value):
        raise ValueError("legacy slug must be a normalized safe component")
    return value


def _logical_path_denormalized(value: str, normalized: str, parts: tuple[str, ...]) -> bool:
    return not value or normalized != value or normalized != "/".join(parts)


def _absolute_anywhere(normalized: str) -> bool:
    posix = Path(normalized.replace("/", os.sep))
    windows = PureWindowsPath(normalized)
    return posix.is_absolute() or windows.is_absolute() or bool(windows.drive)


def _part_is_dot_or_padded(part: str) -> bool:
    return part in {".", ".."} or part.rstrip(". ") != part


def _part_names_device(part: str) -> bool:
    return part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED


def _part_has_separator(part: str) -> bool:
    return "\\" in part or ":" in part or "\x00" in part


def _part_unsafe(part: str) -> bool:
    return _part_is_dot_or_padded(part) or _part_names_device(part) or _part_has_separator(part)


def _logical_path_unsafe(value: str, normalized: str, parts: tuple[str, ...]) -> bool:
    return (
        _logical_path_denormalized(value, normalized, parts)
        or _absolute_anywhere(normalized)
        or any(_part_unsafe(part) for part in parts)
    )


def _safe_logical_path(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    parts = tuple(part for part in normalized.split("/") if part)
    if _logical_path_unsafe(value, normalized, parts):
        raise ValueError("logical_path must be a normalized safe relative path")
    return "/".join(parts)


def _bounded_version(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 128
        and not any(character in value for character in "\x00\r\n")
    )


def _extractor_version(value: str) -> str:
    if not _bounded_version(value):
        raise ValueError("extractor_version must be a bounded non-empty string")
    return value


def _validate_source(source: CapturedSource) -> CapturedSource:
    if not isinstance(source, CapturedSource):
        raise TypeError("source must be a CapturedSource")
    actual_hash = hashlib.sha256(source.content).hexdigest()
    if source.record.size != len(source.content) or source.record.sha256 != actual_hash:
        raise ValueError("captured source content does not match its record")
    return source


def _validate_snapshot_sources(snapshot: CorpusSnapshot) -> tuple[CapturedSource, ...]:
    sources: list[CapturedSource] = []
    identities: set[str] = set()
    for source in snapshot.sources:
        captured = _validate_source(source)
        identity = unicodedata.normalize("NFKC", captured.record.logical_id).casefold()
        if identity in identities:
            raise ValueError("snapshot contains a logical ID collision")
        identities.add(identity)
        sources.append(captured)
    return tuple(sources)


def _reject_contextual_llm(use_llm: bool) -> None:
    if use_llm:
        raise ValueError("LLM-generated contextual text is unavailable pending frozen ablation")


def _deterministic_identity(model_descriptor: object | None, model_revision: str | None) -> None:
    if model_descriptor is not None or model_revision is not None:
        raise ValueError("deterministic cache identity cannot include a model")


def _llm_identity(model_descriptor: object | None, model_revision: str | None) -> dict[str, object]:
    if model_descriptor is None or not model_revision:
        raise ValueError("LLM cache identity requires a model descriptor and revision")

    from llm_client import ProviderDescriptor

    if not isinstance(model_descriptor, ProviderDescriptor):
        raise TypeError("model_descriptor must be a ProviderDescriptor")
    return {**model_descriptor.canonical(), "revision": model_revision}


def _generation_model_identity(
    generation_mode: str,
    model_descriptor: object | None,
    model_revision: str | None,
) -> dict[str, object] | None:
    if generation_mode not in {"deterministic", "llm"}:
        raise ValueError("generation_mode must be 'deterministic' or 'llm'")
    if generation_mode == "deterministic":
        _deterministic_identity(model_descriptor, model_revision)
        return None
    return _llm_identity(model_descriptor, model_revision)


def _model_identity_or_none(model_descriptor: object | None, model_revision: str | None) -> dict | None:
    if model_descriptor is None:
        return None
    return {**model_descriptor.canonical(), "revision": model_revision}


def context_artifact_key(
    source: CapturedSource,
    *,
    extractor_version: str = CONTEXT_EXTRACTOR_VERSION,
    generation_mode: str = "deterministic",
    model_descriptor: object | None = None,
    model_revision: str | None = None,
) -> str:
    """Return the stable key for one exact captured source and extractor."""
    source = _validate_source(source)
    version = _extractor_version(extractor_version)
    model = _generation_model_identity(generation_mode, model_descriptor, model_revision)
    identity = json.dumps(
        [source.record.relative_path, source.record.sha256, version, generation_mode, model],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def legacy_context_cache_path(
    slug: str,
    *,
    source_sha256: str | None = None,
    logical_path: str | None = None,
    extractor_version: str = CONTEXT_EXTRACTOR_VERSION,
    generation_mode: str = "deterministic",
    model_descriptor: object | None = None,
    model_revision: str | None = None,
) -> Path:
    """Return the legacy mutable-cache path for a contextual entry.

    When ``source_sha256`` is provided the path is hash-suffixed so stale
    contextual entries invalidate on content changes (Task 15 contract).
    """
    safe_slug = _legacy_slug(slug)
    model = _generation_model_identity(generation_mode, model_descriptor, model_revision)
    if not source_sha256:
        if generation_mode == "llm":
            raise ValueError("LLM cache identity requires source_sha256")
        return CONTEXT_DIR / f"{safe_slug}.ctx"
    logical = _safe_logical_path(logical_path or f"{slug}.md")
    identity = json.dumps(
        [logical, source_sha256, extractor_version, generation_mode, model],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    suffix = hashlib.sha256(identity).hexdigest()[:20]
    return CONTEXT_DIR / f"{safe_slug}.{suffix}.ctx"


# --- the context itself -------------------------------------------------------------


def _first_group(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    if match is None:
        return ""
    return match.group(1).strip()


def _context_fields(content: str, fallback_title: str) -> tuple[str, str]:
    body = FRONTMATTER_RE.sub("", content, count=1)
    return _first_group(H1_RE, body) or fallback_title, _first_group(SUMMARY_RE, body)


def _labelled(label: str, value: str) -> str:
    if not value:
        return ""
    return f"{label}: {value}."


def _deterministic_context(title: str, summary: str, project: str, type_value: str) -> str:
    parts = (_labelled("Project", project), _labelled("Type", type_value), f"Topic: {title}.", summary)
    return " ".join(part for part in parts if part)


def _require_bounded_context(context: str) -> str:
    if len(context) > MAX_CONTEXT_CHARS:
        raise ValueError("generated context exceeds the supported bound")
    return context


def generate_context_for_source(
    source: CapturedSource,
    *,
    use_llm: bool = False,
    extractor_version: str = CONTEXT_EXTRACTOR_VERSION,
    max_prompt_bytes: int | None = None,
    max_prompt_chars: int | None = None,
    disclosure_policy: str | None = None,
    model_descriptor: object | None = None,
) -> str:
    """Generate context exclusively from an immutable captured source.

    Only the deterministic extractor exists. ``use_llm=True`` is refused until
    the frozen ablation permits it; the prompt bounds, disclosure policy and
    model are accepted for that path and unused today.
    """
    source = _validate_source(source)
    _extractor_version(extractor_version)
    _reject_contextual_llm(use_llm)
    del max_prompt_bytes, max_prompt_chars, disclosure_policy, model_descriptor
    content = source.content.decode("utf-8", errors="strict")
    title, summary = _context_fields(content, source.record.relative_path)
    context = _deterministic_context(title, summary, source.metadata.project or "", source.metadata.type)
    return _require_bounded_context(context)


# --- reading a context back ---------------------------------------------------------


def _legacy_source_slug(source: str | CapturedSource) -> str:
    if not isinstance(source, str):
        raise TypeError("legacy context lookup requires a string slug")
    return _legacy_slug(source)


def _legacy_context(source: str | CapturedSource, identity: Mapping[str, object]) -> str | None:
    slug = _legacy_source_slug(source)
    _validate_existing_ancestors(Path(os.path.abspath(CONTEXT_DIR)), "legacy context cache")
    ctx_file = legacy_context_cache_path(slug, **identity)
    metadata = _lstat_or_none(ctx_file)
    if metadata is None:
        return None
    if not _is_real_file(metadata):
        raise PermissionError("legacy context artifact must be a real file")
    return ctx_file.read_text(encoding="utf-8", errors="ignore").strip()


def _require_contained_directory(contextual: Path, generation: Path, metadata: os.stat_result) -> None:
    if not _is_real_directory(metadata):
        raise PermissionError("contextual directory must be a real contained directory")
    _validate_existing_ancestors(contextual, "contextual directory")
    if contextual.parent != generation:
        raise PermissionError("contextual directory is outside the generation directory")


def _contained_contextual_directory(generation: Path) -> Path | None:
    contextual = generation / "contextual"
    metadata = _lstat_or_none(contextual)
    if metadata is None:
        return None
    _require_contained_directory(contextual, generation, metadata)
    return contextual


def _require_bounded_artifact(metadata: os.stat_result) -> None:
    if not _is_real_file(metadata):
        raise ValueError("context artifact must be a regular file")
    if metadata.st_size > MAX_CONTEXT_ARTIFACT_BYTES:
        raise ValueError("context artifact exceeds the supported bound")


def _parsed_artifact(raw: bytes) -> tuple[Mapping, object, Mapping]:
    try:
        artifact = json.loads(raw)
        context = artifact["context"]
        artifact_source = artifact["source"]
        if not isinstance(artifact, Mapping) or not isinstance(artifact_source, Mapping):
            raise TypeError
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("context artifact is invalid") from exc
    return artifact, context, artifact_source


def _context_value_valid(context: object) -> bool:
    return isinstance(context, str) and len(context) <= MAX_CONTEXT_CHARS


def _artifact_fields_match(artifact: Mapping, expected: Mapping[str, object]) -> bool:
    return all(artifact.get(field) == value for field, value in expected.items())


def _artifact_source_matches(artifact_source: Mapping, captured: CapturedSource) -> bool:
    return (
        artifact_source.get("logical_id") == captured.record.logical_id
        and artifact_source.get("sha256") == captured.record.sha256
    )


class _ArtifactExpectation:
    """What a stored context artifact must say about itself to be used."""

    def __init__(self, captured: CapturedSource, key: str, version: str, generation: Mapping) -> None:
        self.captured = captured
        self.fields = {"key": key, "extractor_version": version, "generation": generation}

    def require(self, artifact: Mapping, context: object, artifact_source: Mapping) -> None:
        if not (
            _context_value_valid(context)
            and _artifact_fields_match(artifact, self.fields)
            and _artifact_source_matches(artifact_source, self.captured)
        ):
            raise ValueError("context artifact provenance does not match the source")


def _generation_context(source: object, generation_dir: Path, identity: Mapping[str, object]) -> str | None:
    captured = _validate_source(source)
    version = _extractor_version(identity["extractor_version"])
    key = context_artifact_key(
        captured,
        extractor_version=version,
        generation_mode=identity["generation_mode"],
        model_descriptor=identity["model_descriptor"],
        model_revision=identity["model_revision"],
    )
    contextual = _contained_contextual_directory(_real_directory(generation_dir, "generation directory"))
    if contextual is None:
        return None
    artifact_path = contextual / f"{key}.json"
    metadata = _lstat_or_none(artifact_path)
    if metadata is None:
        return None
    _require_bounded_artifact(metadata)
    artifact, context, artifact_source = _parsed_artifact(artifact_path.read_bytes())
    generation = {
        "mode": identity["generation_mode"],
        "model": _model_identity_or_none(identity["model_descriptor"], identity["model_revision"]),
    }
    _ArtifactExpectation(captured, key, version, generation).require(artifact, context, artifact_source)
    return context


def get_context(
    source: str | CapturedSource,
    *,
    generation_dir: Path | None = None,
    extractor_version: str = CONTEXT_EXTRACTOR_VERSION,
    source_sha256: str | None = None,
    logical_path: str | None = None,
    generation_mode: str = "deterministic",
    model_descriptor: object | None = None,
    model_revision: str | None = None,
) -> str | None:
    """Read by captured source in generation mode or string slug in legacy mode."""
    identity = {
        "extractor_version": extractor_version,
        "generation_mode": generation_mode,
        "model_descriptor": model_descriptor,
        "model_revision": model_revision,
    }
    if generation_dir is None:
        return _legacy_context(
            source, {**identity, "source_sha256": source_sha256, "logical_path": logical_path}
        )
    return _generation_context(source, generation_dir, identity)


# --- publishing a generation's contexts -------------------------------------------------


def _artifact_bytes(
    source: CapturedSource,
    context: str,
    key: str,
    extractor_version: str,
    generation: Mapping[str, object],
) -> bytes:
    metadata = source.metadata
    payload = {
        "context": context,
        "extractor_version": extractor_version,
        "key": key,
        "generation": generation,
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
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(raw) > MAX_CONTEXT_ARTIFACT_BYTES:
        raise ValueError("context artifact exceeds the supported bound")
    return raw


def _require_snapshot(snapshot: object) -> None:
    if not isinstance(snapshot, CorpusSnapshot):
        raise TypeError("snapshot must be a CorpusSnapshot")
    if len(snapshot.sources) > MAX_CONTEXT_SOURCES:
        raise ValueError("snapshot has too many sources for contextual artifacts")


def _require_no_output(output: Path) -> None:
    metadata = _lstat_or_none(output)
    if metadata is None:
        return
    if _is_link_or_reparse(metadata):
        raise PermissionError("contextual generation output must not be a link or reparse point")
    raise FileExistsError("contextual generation output already exists")


def _write_new_artifact(path: Path, raw: bytes) -> None:
    with path.open("xb") as artifact:
        artifact.write(raw)
        artifact.flush()
        os.fsync(artifact.fileno())


class _ContextStaging:
    """One snapshot's context artifacts, written create-only into a staging directory."""

    def __init__(self, staging: Path, version: str, generation: Mapping, model: tuple) -> None:
        self.staging = staging
        self.version = version
        self.generation = generation
        self.model_descriptor, self.model_revision = model

    def stage(self, source: CapturedSource) -> dict[str, object]:
        key = context_artifact_key(
            source,
            extractor_version=self.version,
            generation_mode=str(self.generation["mode"]),
            model_descriptor=self.model_descriptor,
            model_revision=self.model_revision,
        )
        context = generate_context_for_source(
            source, extractor_version=self.version, model_descriptor=self.model_descriptor
        )
        raw = _artifact_bytes(source, context, key, self.version, self.generation)
        name = f"{key}.json"
        _write_new_artifact(self.staging / name, raw)
        return {"path": f"contextual/{name}", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}

    def stage_all(self, sources: tuple[CapturedSource, ...]) -> list[dict[str, object]]:
        _real_directory(self.staging, "contextual staging directory")
        descriptors = [self.stage(source) for source in sources]
        descriptors.sort(key=lambda item: str(item["path"]))
        return descriptors


def _publish_staging(generation: Path, staging: Path, output: Path) -> None:
    _real_directory(generation, "generation directory")
    _real_directory(staging, "contextual staging directory")
    if _lstat_or_none(output) is not None:
        raise FileExistsError("contextual generation output already exists")
    staging.replace(output)


def build_snapshot_contexts(
    snapshot: CorpusSnapshot,
    generation_dir: Path,
    *,
    use_llm: bool = False,
    extractor_version: str = CONTEXT_EXTRACTOR_VERSION,
    max_prompt_bytes: int | None = None,
    max_prompt_chars: int | None = None,
    disclosure_policy: str | None = None,
    model_descriptor: object | None = None,
    model_revision: str | None = None,
) -> list[dict[str, object]]:
    """Publish exact-snapshot context artifacts into an unpublished generation."""
    _require_snapshot(snapshot)
    _reject_contextual_llm(use_llm)
    del max_prompt_bytes, max_prompt_chars, disclosure_policy
    version = _extractor_version(extractor_version)
    sources = _validate_snapshot_sources(snapshot)
    generation_identity = {
        "mode": "deterministic",
        "model": _model_identity_or_none(model_descriptor, model_revision),
    }
    generation = _real_directory(generation_dir, "generation directory")
    output = generation / "contextual"
    _require_no_output(output)
    staging = Path(tempfile.mkdtemp(prefix=".contextual-", dir=generation))
    try:
        staged = _ContextStaging(staging, version, generation_identity, (model_descriptor, model_revision))
        descriptors = staged.stage_all(sources)
        _publish_staging(generation, staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return descriptors


# --- the legacy mutable cache -------------------------------------------------------


def _legacy_page_path(slug: str, logical_path: str | None) -> Path:
    knowledge_root = Path(os.path.abspath(KNOWLEDGE_DIR))
    _validate_existing_ancestors(knowledge_root, "legacy knowledge directory")
    logical = _safe_logical_path(logical_path or f"{slug}.md")
    page_path = knowledge_root.joinpath(*logical.split("/"))
    if not page_path.resolve().is_relative_to(knowledge_root.resolve()):
        raise PermissionError("legacy knowledge page is outside the knowledge root")
    return page_path


def _captured_page_bytes(page_path: Path, captured_bytes: bytes | None, source_sha256: str | None) -> bytes:
    raw = page_path.read_bytes() if captured_bytes is None else captured_bytes
    if source_sha256 is not None and hashlib.sha256(raw).hexdigest() != source_sha256:
        raise ValueError("captured source bytes do not match source_sha256")
    return raw


def _frontmatter_project_and_type(content: str) -> tuple[str, str]:
    frontmatter = FRONTMATTER_RE.match(content)
    if not frontmatter:
        return "", ""
    return _first_group(PROJECT_RE, frontmatter.group(1)), _first_group(TYPE_RE, frontmatter.group(1))


def generate_context(
    slug: str,
    use_llm: bool = False,
    source_sha256: str | None = None,
    logical_path: str | None = None,
    captured_bytes: bytes | None = None,
) -> str:
    """Legacy path-based context generation compatibility wrapper.

    Context is extracted deterministically from title, summary, and project.
    ``use_llm=True`` is rejected until the frozen ablation permits generation.

    When ``source_sha256`` is provided, the cache file is hash-suffixed so
    stale entries cannot survive a content change (Task 15 contract).
    """
    slug = _legacy_slug(slug)
    _reject_contextual_llm(use_llm)
    page_path = _legacy_page_path(slug, logical_path)
    metadata = _lstat_or_none(page_path)
    if metadata is None:
        return ""
    if not _is_real_file(metadata):
        raise PermissionError("legacy knowledge page must be a real file")
    content = _captured_page_bytes(page_path, captured_bytes, source_sha256).decode("utf-8", errors="strict")
    title, summary = _context_fields(content, slug)
    project, type_value = _frontmatter_project_and_type(content)
    return _deterministic_context(title, summary, project, type_value)


def _prepare_legacy_context_root(knowledge_root: Path) -> None:
    context_root = Path(os.path.abspath(CONTEXT_DIR))
    _validate_existing_ancestors(knowledge_root, "legacy knowledge directory")
    _validate_existing_ancestors(context_root, "legacy context cache")
    context_root.mkdir(parents=True, exist_ok=True)
    _real_directory(context_root, "legacy context cache")


def _skipped_page_name(md: Path) -> bool:
    return md.name in SKIP_NAMES or "archive" in md.parts


def _read_page(md: Path) -> tuple[bytes, str] | None:
    try:
        captured = md.read_bytes()
        return captured, captured.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None


def _retired_page(content: str) -> bool:
    return "status: superseded" in content or "status: archived" in content


def _cache_is_fresh(md: Path, ctx_file: Path) -> bool:
    """Hash-qualified reads and writes never consult stale unqualified entries."""
    if not ctx_file.exists():
        return False
    try:
        return md.stat().st_mtime <= ctx_file.stat().st_mtime
    except OSError:
        return False


class _LegacyPage:
    """One knowledge page and the hash-qualified cache entry it maps to."""

    def __init__(self, md: Path, knowledge_root: Path, captured: bytes) -> None:
        self.md = md
        self.captured = captured
        self.slug = md.stem
        self.source_sha256 = hashlib.sha256(captured).hexdigest()
        self.logical_path = md.relative_to(knowledge_root).as_posix()
        self.ctx_file = legacy_context_cache_path(
            self.slug, source_sha256=self.source_sha256, logical_path=self.logical_path
        )

    def write(self, verbose: bool) -> str:
        try:
            ctx = generate_context(
                self.slug,
                source_sha256=self.source_sha256,
                logical_path=self.logical_path,
                captured_bytes=self.captured,
            )
            if not ctx:
                return "errors"
            atomic_write(self.ctx_file, ctx)
            if verbose:
                print(f"  Generated context: {self.slug}")
            return "generated"
        except Exception:
            return "errors"


def _legacy_page_outcome(md: Path, knowledge_root: Path, verbose: bool) -> str:
    page_bytes = _read_page(md)
    if page_bytes is None:
        return "errors"
    return _readable_page_outcome(md, knowledge_root, page_bytes, verbose)


def _readable_page_outcome(md: Path, knowledge_root: Path, page_bytes: tuple[bytes, str], verbose: bool) -> str:
    captured, content = page_bytes
    if _retired_page(content):
        return "skipped"
    page = _LegacyPage(md, knowledge_root, captured)
    if _cache_is_fresh(md, page.ctx_file):
        return "skipped"
    return page.write(verbose)


def _report_context_stats(stats: Mapping[str, int], verbose: bool) -> None:
    if verbose:
        print(
            f"\nContext generation: {stats['generated']} generated, "
            f"{stats['skipped']} skipped, {stats['errors']} errors."
        )


def build_all_contexts(use_llm: bool = False, verbose: bool = True) -> dict:
    """Build the legacy mutable cache when no generation is supplied."""
    _reject_contextual_llm(use_llm)
    stats = {"generated": 0, "skipped": 0, "errors": 0}
    knowledge_root = Path(os.path.abspath(KNOWLEDGE_DIR))
    _prepare_legacy_context_root(knowledge_root)
    if not knowledge_root.exists():
        return stats
    for md in sorted(knowledge_root.rglob("*.md")):
        if _skipped_page_name(md):
            continue
        stats[_legacy_page_outcome(md, knowledge_root, verbose)] += 1
    _report_context_stats(stats, verbose)
    return stats


def _print_cache_status() -> int:
    if not CONTEXT_DIR.exists():
        print("No context cache. Run --all to generate.")
        return 0
    print(f"Context cache: {len(list(CONTEXT_DIR.glob('*.ctx')))} pages")
    return 0


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Contextual Retrieval — page context generation.")
    p.add_argument("--all", action="store_true", help="Generate for all pages.")
    p.add_argument("--slug", type=str, default=None, help="Generate for one page.")
    p.add_argument("--llm", action="store_true", help="Unavailable until contextual ablation passes.")
    p.add_argument("--status", action="store_true", help="Show cache stats.")
    args = p.parse_args()

    if args.llm:
        p.error("--llm is unavailable until contextual ablation passes")
    return _run_mode(args)


def _run_mode(args) -> int:
    if args.status:
        return _print_cache_status()
    if args.slug:
        print(generate_context(args.slug, use_llm=False))
        return 0
    build_all_contexts(use_llm=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
