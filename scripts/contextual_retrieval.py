"""Contextual Retrieval — prepend LLM-generated context to pages before indexing.

Anthropic technique (provider-agnostic): for each page, generate a one-line
context that disambiguates it. This context is added to the FTS5 index,
making search more precise (-49% retrieval failures per Anthropic data).

Example:
  Page: "# Auth Decision"
  Context: "This page is about choosing JWT over sessions for the
            llm-wiki project, decided March 2026."

The context is stored in cache/contextual/ and merged into the search
index at build time. No changes to Markdown source files.

Usage:
    uv run python scripts/contextual_retrieval.py --all     # generate for all
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


def get_context(
    source: str | CapturedSource,
    *,
    generation_dir: Path | None = None,
    extractor_version: str = CONTEXT_EXTRACTOR_VERSION,
) -> str | None:
    """Read by captured source in generation mode or string slug in legacy mode."""
    if generation_dir is None:
        if not isinstance(source, str):
            raise TypeError("legacy context lookup requires a string slug")
        slug = _legacy_slug(source)
        context_root = Path(os.path.abspath(CONTEXT_DIR))
        _validate_existing_ancestors(context_root, "legacy context cache")
        ctx_file = context_root / f"{slug}.ctx"
        try:
            artifact_metadata = ctx_file.lstat()
        except FileNotFoundError:
            return None
        if _is_link_or_reparse(artifact_metadata) or not stat.S_ISREG(
            artifact_metadata.st_mode
        ):
            raise PermissionError("legacy context artifact must be a real file")
        return ctx_file.read_text(encoding="utf-8", errors="ignore").strip()

    captured = _validate_source(source)
    version = _extractor_version(extractor_version)
    key = context_artifact_key(captured, extractor_version=version)
    generation = _real_directory(generation_dir, "generation directory")
    contextual = generation / "contextual"
    try:
        contextual_metadata = contextual.lstat()
    except FileNotFoundError:
        return None
    if _is_link_or_reparse(contextual_metadata) or not stat.S_ISDIR(
        contextual_metadata.st_mode
    ):
        raise PermissionError("contextual directory must be a real contained directory")
    _validate_existing_ancestors(contextual, "contextual directory")
    if contextual.parent != generation:
        raise PermissionError("contextual directory is outside the generation directory")
    artifact_path = contextual / f"{key}.json"
    try:
        artifact_metadata = artifact_path.lstat()
    except FileNotFoundError:
        return None
    if _is_link_or_reparse(artifact_metadata) or not stat.S_ISREG(
        artifact_metadata.st_mode
    ):
        raise ValueError("context artifact must be a regular file")
    if artifact_metadata.st_size > MAX_CONTEXT_ARTIFACT_BYTES:
        raise ValueError("context artifact exceeds the supported bound")
    try:
        artifact = json.loads(artifact_path.read_bytes())
        context = artifact["context"]
        artifact_source = artifact["source"]
        if not isinstance(artifact, Mapping) or not isinstance(artifact_source, Mapping):
            raise TypeError
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("context artifact is invalid") from exc
    if (
        not isinstance(context, str)
        or len(context) > MAX_CONTEXT_CHARS
        or artifact.get("key") != key
        or artifact.get("extractor_version") != version
        or artifact_source.get("logical_id") != captured.record.logical_id
        or artifact_source.get("sha256") != captured.record.sha256
    ):
        raise ValueError("context artifact provenance does not match the source")
    return context


def _legacy_slug(value: str) -> str:
    windows = PureWindowsPath(value)
    normalized = unicodedata.normalize("NFKC", value)
    if (
        not value
        or len(value) > MAX_LEGACY_SLUG_CHARS
        or normalized != value
        or value in {".", ".."}
        or value[-1] in {".", " "}
        or any(character in value for character in "/\\:\x00\r\n")
        or windows.drive
        or windows.root
        or value.split(".", 1)[0].casefold() in _WINDOWS_RESERVED
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("legacy slug must be a normalized safe component")
    return value


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _validate_existing_ancestors(path: Path, label: str) -> None:
    absolute = Path(os.path.abspath(path))
    for candidate in reversed((absolute, *absolute.parents)):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if _is_link_or_reparse(metadata):
            raise PermissionError(f"unsafe {label} ancestor: {candidate}")


def _real_directory(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    _validate_existing_ancestors(absolute, label)
    try:
        metadata = absolute.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"{label} must be an existing real directory") from exc
    if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise PermissionError(f"unsafe {label} ancestor: {absolute}")
    return absolute


def _context_fields(content: str, fallback_title: str) -> tuple[str, str]:
    body = FRONTMATTER_RE.sub("", content, count=1)
    title_match = H1_RE.search(body)
    title = title_match.group(1).strip() if title_match else fallback_title
    summary_match = SUMMARY_RE.search(body)
    summary = summary_match.group(1).strip() if summary_match else ""
    return title, summary


def _deterministic_context(title: str, summary: str, project: str, type_value: str) -> str:
    parts = []
    if project:
        parts.append(f"Project: {project}.")
    if type_value:
        parts.append(f"Type: {type_value}.")
    parts.append(f"Topic: {title}.")
    if summary:
        parts.append(summary)
    return " ".join(parts)


def _extractor_version(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(character in value for character in "\x00\r\n")
    ):
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


def _validate_llm_options(
    *,
    max_prompt_bytes: int | None,
    max_prompt_chars: int | None,
    disclosure_policy: str | None,
    model_descriptor: object | None,
) -> None:
    if (
        isinstance(max_prompt_bytes, bool)
        or not isinstance(max_prompt_bytes, int)
        or not 1 <= max_prompt_bytes <= MAX_LLM_PROMPT_BYTES
        or isinstance(max_prompt_chars, bool)
        or not isinstance(max_prompt_chars, int)
        or not 1 <= max_prompt_chars <= MAX_LLM_PROMPT_CHARS
    ):
        raise ValueError("max_prompt_bytes and max_prompt_chars must be explicit bounded integers")
    if disclosure_policy not in {None, "local", "remote"}:
        raise ValueError("disclosure_policy must be 'local' or 'remote'")
    if disclosure_policy is None and model_descriptor is None:
        raise ValueError("LLM use requires an explicit disclosure policy or model descriptor")
    if model_descriptor is not None:
        from llm_client import ProviderDescriptor

        if not isinstance(model_descriptor, ProviderDescriptor):
            raise TypeError("model_descriptor must be a ProviderDescriptor")


def context_artifact_key(
    source: CapturedSource, *, extractor_version: str = CONTEXT_EXTRACTOR_VERSION
) -> str:
    """Return the stable key for one exact captured source and extractor."""
    source = _validate_source(source)
    version = _extractor_version(extractor_version)
    identity = json.dumps(
        [source.record.logical_id, source.record.sha256, version],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


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
    """Generate context exclusively from an immutable captured source."""
    source = _validate_source(source)
    _extractor_version(extractor_version)
    content = source.content.decode("utf-8", errors="strict")
    title, summary = _context_fields(content, source.record.relative_path)
    project = source.metadata.project or ""
    type_value = source.metadata.type

    if not use_llm:
        result = _deterministic_context(title, summary, project, type_value)
    else:
        _validate_llm_options(
            max_prompt_bytes=max_prompt_bytes,
            max_prompt_chars=max_prompt_chars,
            disclosure_policy=disclosure_policy,
            model_descriptor=model_descriptor,
        )

        prompt = f"""Generate ONE sentence of context for this captured knowledge source.
The context should disambiguate the source: project, topic, and decision when present.
Keep it under 100 characters. Use only the captured content and metadata below.

Logical ID: {source.record.logical_id}
Source SHA256: {source.record.sha256}
Project: {project}
Type: {type_value}
Captured content:
{content}

Return ONLY the context sentence. No preamble."""
        assert max_prompt_bytes is not None
        assert max_prompt_chars is not None
        if len(prompt) > max_prompt_chars or len(prompt.encode("utf-8")) > max_prompt_bytes:
            raise ValueError("complete context prompt exceeds the configured bound")
        if os.environ.get("MEMORY_LLM_PROVIDER", "").lower() == "fake":
            response = _deterministic_context(title, summary, project, type_value)
        elif model_descriptor is None:
            from llm_client import call_llm

            response = call_llm(prompt, "You are a context generator.", max_tokens=100)
        else:
            from llm_client import call_candidate

            llm_result = call_candidate(
                model_descriptor,
                prompt,
                "You are a context generator.",
                max_tokens=100,
            )
            response = llm_result.text
        if not response or not response.strip():
            raise RuntimeError("context LLM returned no content")
        result = response.strip()

    if len(result) > MAX_CONTEXT_CHARS:
        raise ValueError("generated context exceeds the supported bound")
    return result


def _artifact_bytes(
    source: CapturedSource, context: str, key: str, extractor_version: str
) -> bytes:
    metadata = source.metadata
    payload = {
        "context": context,
        "extractor_version": extractor_version,
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
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(raw) > MAX_CONTEXT_ARTIFACT_BYTES:
        raise ValueError("context artifact exceeds the supported bound")
    return raw


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
) -> list[dict[str, object]]:
    """Publish exact-snapshot context artifacts into an unpublished generation."""
    if not isinstance(snapshot, CorpusSnapshot):
        raise TypeError("snapshot must be a CorpusSnapshot")
    if len(snapshot.sources) > MAX_CONTEXT_SOURCES:
        raise ValueError("snapshot has too many sources for contextual artifacts")
    version = _extractor_version(extractor_version)
    sources = _validate_snapshot_sources(snapshot)
    if use_llm:
        _validate_llm_options(
            max_prompt_bytes=max_prompt_bytes,
            max_prompt_chars=max_prompt_chars,
            disclosure_policy=disclosure_policy,
            model_descriptor=model_descriptor,
        )
    generation = _real_directory(generation_dir, "generation directory")
    output = generation / "contextual"
    try:
        output_metadata = output.lstat()
    except FileNotFoundError:
        pass
    else:
        if _is_link_or_reparse(output_metadata):
            raise PermissionError("contextual generation output must not be a link or reparse point")
        raise FileExistsError("contextual generation output already exists")

    staging = Path(tempfile.mkdtemp(prefix=".contextual-", dir=generation))
    descriptors: list[dict[str, object]] = []
    try:
        _real_directory(staging, "contextual staging directory")
        for source in sources:
            key = context_artifact_key(source, extractor_version=version)
            context = generate_context_for_source(
                source,
                use_llm=use_llm,
                extractor_version=version,
                max_prompt_bytes=max_prompt_bytes,
                max_prompt_chars=max_prompt_chars,
                disclosure_policy=disclosure_policy,
                model_descriptor=model_descriptor,
            )
            raw = _artifact_bytes(source, context, key, version)
            name = f"{key}.json"
            with (staging / name).open("xb") as artifact:
                artifact.write(raw)
                artifact.flush()
                os.fsync(artifact.fileno())
            descriptors.append(
                {
                    "path": f"contextual/{name}",
                    "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        descriptors.sort(key=lambda item: str(item["path"]))
        _real_directory(generation, "generation directory")
        _real_directory(staging, "contextual staging directory")
        try:
            output.lstat()
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError("contextual generation output already exists")
        staging.replace(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return descriptors


def legacy_context_cache_path(slug: str, *, source_sha256: str | None = None) -> Path:
    """Return the legacy mutable-cache path for a contextual entry.

    When ``source_sha256`` is provided the path is hash-suffixed so stale
    contextual entries invalidate on content changes (Task 15 contract).
    """
    if not source_sha256:
        return CONTEXT_DIR / f"{slug}.ctx"
    digest_input = f"{source_sha256}:{CONTEXT_EXTRACTOR_VERSION}".encode()
    suffix = hashlib.sha256(digest_input).hexdigest()[:12]
    return CONTEXT_DIR / f"{slug}.{suffix}.ctx"


def generate_context(
    slug: str,
    use_llm: bool = True,
    source_sha256: str | None = None,
) -> str:
    """Legacy path-based context generation compatibility wrapper.

    If use_llm and LLM available: LLM generates context.
    Otherwise: deterministic extraction from title + summary + project.

    When ``source_sha256`` is provided, the cache file is hash-suffixed so
    stale entries cannot survive a content change (Task 15 contract).
    """
    slug = _legacy_slug(slug)
    knowledge_root = Path(os.path.abspath(KNOWLEDGE_DIR))
    _validate_existing_ancestors(knowledge_root, "legacy knowledge directory")
    page_path = knowledge_root / f"{slug}.md"
    try:
        page_metadata = page_path.lstat()
    except FileNotFoundError:
        return ""
    if _is_link_or_reparse(page_metadata) or not stat.S_ISREG(page_metadata.st_mode):
        raise PermissionError("legacy knowledge page must be a real file")

    content = page_path.read_text(encoding="utf-8", errors="ignore")
    title, summary = _context_fields(content, slug)

    project = ""
    type_val = ""
    fm = FRONTMATTER_RE.match(content)
    if fm:
        proj_m = PROJECT_RE.search(fm.group(1))
        project = proj_m.group(1).strip() if proj_m else ""
        type_m = TYPE_RE.search(fm.group(1))
        type_val = type_m.group(1).strip() if type_m else ""

    # Deterministic fallback (no LLM).
    if not use_llm or os.environ.get("MEMORY_LLM_PROVIDER", "").lower() == "fake":
        return _deterministic_context(title, summary, project, type_val)

    # LLM-based context generation.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from llm_client import call_llm
    except ImportError:
        return f"Topic: {title}. {summary}"

    prompt = f"""Generate ONE sentence of context for this knowledge page.
The context should disambiguate the page — what project, what topic,
what decision. Keep it under 100 characters.

Title: {title}
Summary: {summary}
Project: {project}
Type: {type_val}

Return ONLY the context sentence. No preamble."""

    result = call_llm(prompt, "You are a context generator.", max_tokens=100)
    if result and result.strip():
        return result.strip()

    return f"Topic: {title}. {summary}"


def build_all_contexts(use_llm: bool = True, verbose: bool = True) -> dict:
    """Build the legacy mutable cache when no generation is supplied."""
    stats = {"generated": 0, "skipped": 0, "errors": 0}
    knowledge_root = Path(os.path.abspath(KNOWLEDGE_DIR))
    context_root = Path(os.path.abspath(CONTEXT_DIR))
    _validate_existing_ancestors(knowledge_root, "legacy knowledge directory")
    _validate_existing_ancestors(context_root, "legacy context cache")
    context_root.mkdir(parents=True, exist_ok=True)
    _real_directory(context_root, "legacy context cache")

    if not knowledge_root.exists():
        return stats

    for md in sorted(knowledge_root.rglob("*.md")):
        if md.name in SKIP_NAMES or "archive" in md.parts:
            continue

        content = md.read_text(encoding="utf-8", errors="ignore")
        if "status: superseded" in content or "status: archived" in content:
            stats["skipped"] += 1
            continue

        slug = md.stem
        try:
            source_sha256 = hashlib.sha256(md.read_bytes()).hexdigest()
        except OSError:
            source_sha256 = None
        ctx_file = legacy_context_cache_path(slug, source_sha256=source_sha256)
        legacy_file = legacy_context_cache_path(slug)

        # Skip if hash-keyed context exists and page hasn't changed; fall
        # back to legacy un-suffixed cache for migration compatibility.
        if ctx_file.exists():
            try:
                if md.stat().st_mtime <= ctx_file.stat().st_mtime:
                    stats["skipped"] += 1
                    continue
            except OSError:
                pass
        elif legacy_file.exists():
            try:
                if md.stat().st_mtime <= legacy_file.stat().st_mtime:
                    stats["skipped"] += 1
                    continue
            except OSError:
                pass

        try:
            ctx = generate_context(slug, use_llm=use_llm, source_sha256=source_sha256)
            if ctx:
                atomic_write(ctx_file, ctx)
                stats["generated"] += 1
                if verbose:
                    print(f"  Generated context: {slug}")
            else:
                stats["errors"] += 1
        except Exception:
            stats["errors"] += 1

    if verbose:
        print(
            f"\nContext generation: {stats['generated']} generated, "
            f"{stats['skipped']} skipped, {stats['errors']} errors."
        )
    return stats


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Contextual Retrieval — page context generation.")
    p.add_argument("--all", action="store_true", help="Generate for all pages.")
    p.add_argument("--slug", type=str, default=None, help="Generate for one page.")
    p.add_argument("--no-llm", action="store_true", help="Use deterministic extraction.")
    p.add_argument("--status", action="store_true", help="Show cache stats.")
    args = p.parse_args()

    if args.status:
        if not CONTEXT_DIR.exists():
            print("No context cache. Run --all to generate.")
            return 0
        ctx_files = list(CONTEXT_DIR.glob("*.ctx"))
        print(f"Context cache: {len(ctx_files)} pages")
        return 0

    if args.slug:
        ctx = generate_context(args.slug, use_llm=not args.no_llm)
        print(ctx)
        return 0

    build_all_contexts(use_llm=not args.no_llm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
