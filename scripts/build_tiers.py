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
import re
import sys
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT, STATE_ROOT, atomic_write  # noqa: E402

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
MAX_MODEL_DESCRIPTOR_BYTES = 16 * 1024
_LINE_BREAKING = frozenset("\x00\r\n")


def _bounded_text(value: object) -> bool:
    """A non-empty single-line string of at most 128 characters."""
    if not isinstance(value, str) or not value:
        return False
    return len(value) <= 128 and _LINE_BREAKING.isdisjoint(value)


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
    current: set[Path] = set()
    for md in sorted(KNOWLEDGE_DIR.rglob("*.md")):
        if md.name in SKIP_NAMES or "archive" in md.parts:
            continue
        stats[_build_page_tier(md, verbose, current)] += 1
    _prune_unreferenced_l1(current)
    if verbose:
        print(f"\nL1 tier generation: {stats['generated']} generated, "
              f"{stats['skipped']} skipped, {stats['errors']} errors.")
    return stats


def _prune_unreferenced_l1(current: set[Path]) -> None:
    """After a whole pass, the cache holds one file per live page and no more.

    The file name carries a hash of the page's bytes and the extractor version,
    so every edit left another file behind for ever and `--status` reported more
    L1 files than the vault has pages. Only a full pass knows the whole set: two
    pages can share a stem and differ only inside the hash. The cache is
    disposable and its only reader asks for the current hash, so what the pass
    did not name is residue.
    """
    for path in TIERS_DIR.glob("*.l1.md"):
        _remove_stale_l1(path, current)


def _remove_stale_l1(path: Path, current: set[Path]) -> None:
    if path in current:
        return
    try:
        path.unlink()
    except OSError:
        pass


def _build_page_tier(md: Path, verbose: bool, current: set[Path]) -> str:
    """The stats bucket of one page: generated, skipped or errors."""
    # One page that is not UTF-8, or whose name is unsafe (`aux.md`, `a:b.md`), is
    # that page's error, not the whole weekly run's. See
    # `docs/research/2026-09-14-the-rest-of-the-readers-before-the-writer.md`.
    try:
        captured = md.read_bytes()
        content = captured.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return "errors"
    if "status: superseded" in content or "status: archived" in content:
        return "skipped"
    return _refresh_page_tier(md, captured, content, verbose, current)


def _refresh_page_tier(
    md: Path, captured: bytes, content: str, verbose: bool, current: set[Path]
) -> str:
    slug = md.stem
    source_sha256 = hashlib.sha256(captured).hexdigest()
    logical_path = md.relative_to(KNOWLEDGE_DIR).as_posix()
    try:
        current.add(
            tier_legacy_cache_path(
                slug, source_sha256=source_sha256, logical_path=logical_path
            )
        )
        if not _needs_l1_regeneration(slug, md, source_sha256, logical_path=logical_path):
            return "skipped"
        _write_page_l1(slug, content, source_sha256, logical_path)
        _announce_generated(slug, verbose)
    except Exception:  # noqa: BLE001 - one page's failure is counted, the run goes on
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
