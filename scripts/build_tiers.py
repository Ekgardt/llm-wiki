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
from pathlib import Path
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


def _extractor_version(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(character in value for character in "\x00\r\n")
    ):
        raise ValueError("extractor_version must be a bounded non-empty string")
    return value


def _bounded_model_value(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(character in value for character in "\x00\r\n")
    ):
        raise ValueError(f"{label} must be a bounded non-empty string")
    return value


def _model_provenance(
    use_llm: bool, model_descriptor: object | None, model_revision: str | None
) -> dict[str, object] | None:
    if not use_llm:
        if model_descriptor is not None or model_revision is not None:
            raise ValueError("model descriptor and revision require LLM generation")
        return None
    if model_descriptor is None or model_revision is None:
        raise ValueError("LLM generation requires a model descriptor and revision")

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
    content = _captured_text(source)
    body = FRONTMATTER_RE.sub("", content, count=1)
    match = SUMMARY_RE.search(body)
    if match:
        return match.group(1).strip()

    lines = body.splitlines()
    for line in lines[1:]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("---"):
            return stripped[:200]

    return Path(source.record.relative_path).stem.replace("-", " ")


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
{body[:3000]}

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
    use_llm = model_descriptor is not None or model_revision is not None or generated_l1 is not None
    model = _model_provenance(use_llm, model_descriptor, model_revision)
    if use_llm and not isinstance(generated_l1, str):
        raise ValueError("LLM artifact identity requires generated L1 bytes")
    generated_hash = (
        hashlib.sha256(generated_l1.encode("utf-8")).hexdigest() if use_llm else None
    )
    identity = json.dumps(
        [source.record.logical_id, source.record.sha256, version, model, generated_hash],
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
    from corpus_snapshot import CorpusSnapshot

    if not isinstance(snapshot, CorpusSnapshot):
        raise TypeError("snapshot must be a CorpusSnapshot")
    if len(snapshot.sources) > MAX_TIER_SOURCES:
        raise ValueError("snapshot has too many sources for tier artifacts")
    version = _extractor_version(extractor_version)
    model = _model_provenance(use_llm, model_descriptor, model_revision)
    generation = Path(generation_dir)
    metadata = generation.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or generation.is_symlink():
        raise ValueError("generation_dir must be a regular unpublished directory")
    output = generation / "tiers"
    if output.exists() or output.is_symlink():
        raise FileExistsError("tier output already exists")

    staging = Path(tempfile.mkdtemp(prefix=".tiers-", dir=generation))
    entries: list[dict[str, object]] = []
    seen_sources: set[str] = set()
    published = False
    try:
        for source in snapshot.sources:
            source = _validate_source(source)
            if source.record.logical_id in seen_sources:
                raise ValueError("snapshot contains duplicate logical source IDs")
            seen_sources.add(source.record.logical_id)
            l1 = generate_l1_for_source(
                source,
                use_llm=use_llm,
                model_descriptor=model_descriptor,
                model_revision=model_revision,
            )
            key = tier_artifact_key(
                source,
                extractor_version=version,
                model_descriptor=model_descriptor,
                model_revision=model_revision,
                generated_l1=l1 if use_llm else None,
            )
            tiers = {
                "l0": get_l0_for_source(source),
                "l1": l1,
                "l2": get_l2_for_source(source),
            }
            entries.append(_tier_entry(source, key, tiers))
        entries.sort(key=lambda item: str(item["source"]["logical_id"]))  # type: ignore[index]
        content = _tier_artifact_bytes(entries, version, model)
        name = "tiers.json"
        with (staging / name).open("xb") as artifact:
            artifact.write(content)
            artifact.flush()
            os.fsync(artifact.fileno())
        _fsync_directory(staging)
        descriptors = [
            {
                "path": f"tiers/{name}",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ]
        if output.exists() or output.is_symlink():
            raise FileExistsError("tier output already exists")
        staging.replace(output)
        published = True
        _fsync_directory(generation)
        return descriptors
    except BaseException:
        if published and output.exists() and not output.is_symlink():
            shutil.rmtree(output)
            try:
                _fsync_directory(generation)
            except OSError:
                pass
        raise
    finally:
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
    body = FRONTMATTER_RE.sub("", content, count=1)

    m = SUMMARY_RE.search(body)
    if m:
        return m.group(1).strip()

    # Fallback: first sentence after H1.
    lines = body.splitlines()
    for line in lines[1:]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("---"):
            return stripped[:200]

    return slug.replace("-", " ")


def get_l1(slug: str) -> str | None:
    """Get L1 (structured overview) for a page. ~500-1000 tokens.

    Reads from cache/tiers/<slug>.l1.md. Returns None if not generated.
    """
    l1_path = TIERS_DIR / f"{slug}.l1.md"
    if not l1_path.exists():
        return None
    return l1_path.read_text(encoding="utf-8", errors="ignore")


def get_l2(slug: str) -> str:
    """Get L2 (full page content). Just reads the markdown file."""
    page_path = KNOWLEDGE_DIR / f"{slug}.md"
    if not page_path.exists():
        return ""
    return page_path.read_text(encoding="utf-8", errors="ignore")


def _needs_l1_regeneration(slug: str, page_path: Path) -> bool:
    """Check if L1 needs to be (re)generated for this page."""
    l1_path = TIERS_DIR / f"{slug}.l1.md"
    if not l1_path.exists():
        return True
    # Check if page changed since L1 was generated.
    try:
        return page_path.stat().st_mtime > l1_path.stat().st_mtime
    except OSError:
        return True


def generate_l1(slug: str, use_llm: bool = True) -> str | None:
    """Generate L1 overview for a page.

    If use_llm=True and LLM available: LLM generates a structured overview.
    If use_llm=False or no LLM: deterministic extraction (first N paragraphs).
    """
    page_path = KNOWLEDGE_DIR / f"{slug}.md"
    if not page_path.exists():
        return None

    content = page_path.read_text(encoding="utf-8", errors="ignore")
    body = FRONTMATTER_RE.sub("", content, count=1)
    l0 = get_l0(slug)

    if not use_llm or os_env_fake():
        return _deterministic_l1(slug, body, l0)

    # LLM-based L1 generation.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from llm_client import call_llm
    except ImportError:
        return _deterministic_l1(slug, body, l0)

    # Skip for fake provider (tests).
    if os_env_fake():
        return _deterministic_l1(slug, body, l0)

    prompt = f"""Summarize this knowledge page into a structured overview.
Keep it under 500 words. Include:
- Key points (bulleted)
- Important decisions or constraints
- Links to related concepts

=== PAGE ===
{body[:3000]}

=== OUTPUT ===
Return ONLY the overview markdown (no title, no commentary).
"""

    result = call_llm(prompt, "You are a knowledge summarizer.", max_tokens=1000)
    if not result or not result.strip():
        return _deterministic_l1(slug, body, l0)

    return result.strip()


def _deterministic_l1(slug: str, body: str, l0: str) -> str:
    """Generate L1 without LLM — extract first sections."""
    lines = body.splitlines()
    overview_lines = [l0, ""]
    char_count = len(l0)

    for line in lines[1:]:  # Skip H1
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("## History"):
            break  # Stop at history section
        # Check limit BEFORE adding.
        if char_count + len(line) >= 2000:
            overview_lines.append("\n...(truncated, see full page for more)")
            break
        if stripped.startswith("## "):
            overview_lines.append("")
            overview_lines.append(stripped)
            char_count += len(stripped)
        else:
            overview_lines.append(line)
            char_count += len(line)

    return "\n".join(overview_lines)


def os_env_fake() -> bool:
    """Check if running with fake LLM provider (tests)."""
    import os
    return os.environ.get("MEMORY_LLM_PROVIDER", "").lower() == "fake"


def build_all_tiers(use_llm: bool = True, verbose: bool = True) -> dict:
    """Generate L1 overviews for all pages that need it.

    Returns stats: {generated, skipped, errors}
    """
    TIERS_DIR.mkdir(parents=True, exist_ok=True)

    stats = {"generated": 0, "skipped": 0, "errors": 0}

    if not KNOWLEDGE_DIR.exists():
        return stats

    for md in sorted(KNOWLEDGE_DIR.rglob("*.md")):
        if md.name in SKIP_NAMES or "archive" in md.parts:
            continue

        # Skip superseded pages.
        try:
            content = md.read_text(encoding="utf-8", errors="ignore")
            if "status: superseded" in content or "status: archived" in content:
                stats["skipped"] += 1
                continue
        except OSError:
            stats["errors"] += 1
            continue

        slug = md.stem

        if not _needs_l1_regeneration(slug, md):
            stats["skipped"] += 1
            continue

        try:
            l1 = generate_l1(slug, use_llm=use_llm)
            if l1:
                l1_path = TIERS_DIR / f"{slug}.l1.md"
                atomic_write(l1_path, l1)
                stats["generated"] += 1
                if verbose:
                    print(f"  Generated L1: {slug}")
            else:
                stats["errors"] += 1
        except Exception:
            stats["errors"] += 1

    if verbose:
        print(f"\nL1 tier generation: {stats['generated']} generated, "
              f"{stats['skipped']} skipped, {stats['errors']} errors.")

    return stats


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

    if level == "l0":
        return {"level": "l0", "content": l0, "available": ["l0"]}
    elif level == "l1":
        return {"level": "l1", "content": l1 or l0, "available": ["l0"] + (["l1"] if l1 else [])}
    elif level == "l2":
        return {"level": "l2", "content": l2, "available": ["l0"] + (["l1"] if l1 else []) + ["l2"]}
    else:  # auto
        content = l0
        if l1:
            content = l1
        return {
            "level": "l1" if l1 else "l0",
            "content": content,
            "l0": l0,
            "available": ["l0"] + (["l1"] if l1 else []) + (["l2"] if l2 else []),
        }


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="L0/L1/L2 tiered knowledge loading.")
    p.add_argument("--slug", type=str, default=None, help="Generate L1 for one page.")
    p.add_argument("--all", action="store_true", help="Generate L1 for all pages.")
    p.add_argument("--no-llm", action="store_true", help="Use deterministic extraction (no LLM).")
    p.add_argument("--status", action="store_true", help="Show cache statistics.")
    p.add_argument("--get", type=str, default=None, help="Get content at tier level.")
    args = p.parse_args()

    if args.status:
        if not TIERS_DIR.exists():
            print("No L1 cache. Run --all to generate.")
            return 0
        l1_files = list(TIERS_DIR.glob("*.l1.md"))
        pages = list(KNOWLEDGE_DIR.rglob("*.md")) if KNOWLEDGE_DIR.exists() else []
        page_count = sum(1 for p in pages if p.name not in SKIP_NAMES and "archive" not in p.parts)
        print(f"L1 cache: {len(l1_files)} / {page_count} pages")
        return 0

    if args.slug:
        l1 = generate_l1(args.slug, use_llm=not args.no_llm)
        if l1:
            l1_path = TIERS_DIR / f"{args.slug}.l1.md"
            TIERS_DIR.mkdir(parents=True, exist_ok=True)
            atomic_write(l1_path, l1)
            print(f"Generated L1 for {args.slug}: {len(l1)} chars.")
        else:
            print(f"Failed to generate L1 for {args.slug}.")
        return 0

    if args.get:
        result = get_tier(args.get)
        print(f"Level: {result['level']}")
        print(f"Available: {result['available']}")
        print(f"Content ({len(result['content'])} chars):")
        print(result['content'][:500])
        return 0

    if args.all or True:  # Default: build all
        build_all_tiers(use_llm=not args.no_llm)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
