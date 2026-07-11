"""Echo knowledge/notes/ knowledge pages into a Cognee graph for semantic search.

Phase 4 — OPTIONAL semantic layer. This script is the bridge between
the markdown vault and a Cognee graph database. It runs AFTER a
successful compile_memory pass and feeds the touched pages into Cognee
for entity extraction + relationship graph construction.

Design:
- Graceful degradation: if `cognee` is not installed (ImportError) or
  if Ollama is not running, the script exits 0 with a one-line notice.
  Never fails the parent compile pipeline.
- Idempotent: re-running on the same pages is safe (Cognee deduplicates
  by content hash internally).
- Local-only: configured to use Ollama embeddings + LLM by default, to
  preserve the LLM-agnostic axiom. Override via env vars for cloud.
- Storage: $LLM_WIKI_STATE_ROOT/cache/cognee/ (under the search-index cache tree)

Usage:
    uv run python scripts/cognee_sync.py                    # sync all knowledge/notes/
    uv run python scripts/cognee_sync.py --file PATH        # sync one file
    uv run python scripts/cognee_sync.py --dry-run          # plan only
    uv run python scripts/cognee_sync.py --status           # check setup health

Prerequisites (see docs/setup-cognee.md):
    1. pip install cognee
    2. Ollama installed + OLLAMA_MODELS env var pointing to <your-models-path>
    3. ollama pull mxbai-embed-large
    4. ollama pull qwen3:0.6b  (or llama3.2:1b for slightly better extraction)
    5. ollama serve running on localhost:11434

NOTE [UNVERIFIED]: The Cognee integration contract (add, cognify, search
APIs and their kwargs) is version-dependent. Verify against the installed
`cognee` package version before relying on this script in production.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT, STATE_ROOT  # noqa: E402

COGNEE_DATA_DIR = STATE_ROOT / "cache" / "cognee"

KNOWLEDGE_DIR = ROOT / "knowledge" / "notes"

# Skip these subtrees — they are operational / editorial, not knowledge.
SKIP_SUBTREES = (
    ROOT / "knowledge" / "projects",  # state.md is operational, not semantic knowledge
    KNOWLEDGE_DIR / "gaps",  # gaps are placeholders, not real content yet
)


def _have_cognee() -> bool:
    """True iff the `cognee` package is importable. Used for graceful skip.

    Uses ``importlib.util.find_spec`` instead of a bare ``import cognee``
    to avoid executing the module body — Cognee reads env vars at import
    time and caches them. A bare import here would poison the module
    cache with default values before our ``os.environ.setdefault`` block
    runs in ``main()``.
    """
    return importlib.util.find_spec("cognee") is not None


def _ollama_running() -> bool:
    """Best-effort check that Ollama is reachable on the default port."""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=1.0):
            return True
    except (OSError, ConnectionError):
        return False


def _collect_pages() -> list[Path]:
    """All .md pages under knowledge/notes/ (except projects/gaps subtrees)."""
    out: list[Path] = []
    seen: set[Path] = set()

    def _add_tree(tree: Path) -> None:
        if not tree.exists():
            return
        for p in sorted(tree.rglob("*.md")):
            if not p.is_file() or p in seen:
                continue
            # Skip excluded subtrees.
            if any(skip in p.parents for skip in SKIP_SUBTREES):
                continue
            # Skip editorial reserved filenames.
            if p.name in {"index.md", "log.md"}:
                continue
            seen.add(p)
            out.append(p)

    _add_tree(KNOWLEDGE_DIR)
    return out


def _status() -> int:
    """Print setup health and exit. Always returns 0."""
    print("=== Cognee setup status ===")
    print(f"  cognee installed:     {'YES' if _have_cognee() else 'NO — pip install cognee'}")
    print(f"  cognee data dir:      {COGNEE_DATA_DIR}")
    print(f"  COGNEE_DATA_DIR env:  {os.environ.get('COGNEE_DATA_DIR', '(not set)')}")
    print(f"  OLLAMA_MODELS env:    {os.environ.get('OLLAMA_MODELS', '(not set)')}")
    print(f"  Ollama reachable:     {'YES' if _ollama_running() else 'NO — start `ollama serve`'}")
    print(f"  LLM_API_KEY env:      {'(set)' if os.environ.get('LLM_API_KEY') else '(not set — required for cloud fallback)'}")
    pages = _collect_pages()
    print(f"  Pages eligible:       {len(pages)}")
    if not _have_cognee():
        print("\nNext steps:")
        print("  1. pip install cognee")
        print("  2. setx OLLAMA_MODELS \"<your-models-path>\"")
        print("  3. Download + run Ollama, then `ollama pull mxbai-embed-large`")
        print("  4. Re-run this script")
    elif not _ollama_running():
        print("\nNext steps:")
        print("  1. Start Ollama: `ollama serve`")
        print("  2. Verify models: `ollama list` should include mxbai-embed-large")
    return 0


def _sync_one(cognee_module, page: Path) -> str:
    """Add one page's content to Cognee. Returns status string."""
    try:
        content = page.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        return f"read_error: {type(e).__name__}"

    # Cognee's add() takes text or a file path. We hand it the text
    # directly so we can strip frontmatter noise if needed.
    try:
        # Use the relative path as the dataset identifier so re-runs
        # replace rather than duplicate.
        rel = page.relative_to(ROOT).as_posix()
        asyncio_run = __import__("asyncio").run
        asyncio_run(cognee_module.add(content, dataset_name=rel))
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"cognee_add_failed: {type(e).__name__}: {e}"


def _cognify(cognee_module) -> str:
    """Run Cognee's cognify() to build the graph. Returns status."""
    try:
        asyncio_run = __import__("asyncio").run
        asyncio_run(cognee_module.cognify())
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"cognify_failed: {type(e).__name__}: {e}"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--file", type=str, default=None, help="Sync one file path.")
    p.add_argument("--dry-run", action="store_true", help="Plan only, no changes.")
    p.add_argument("--status", action="store_true", help="Print setup health and exit.")
    p.add_argument(
        "--skip-cognify",
        action="store_true",
        help="Add pages to Cognee but skip the graph build (faster; cognify later).",
    )
    args = p.parse_args()

    if args.status:
        return _status()

    if not _have_cognee():
        print(
            "cognee_sync: SKIP — `cognee` not installed. "
            "Run `uv run python scripts/cognee_sync.py --status` for setup steps."
        )
        return 0

    if not _ollama_running():
        print(
            "cognee_sync: SKIP — Ollama not reachable on 127.0.0.1:11434. "
            "Start `ollama serve`. For cloud fallback, set LLM_PROVIDER + LLM_API_KEY env vars."
        )
        return 0

    # CRITICAL: set env vars BEFORE `import cognee`. Cognee reads
    # COGNEE_DATA_DIR, LLM_PROVIDER, etc. at import time and caches
    # them — setting them after the import has no effect. This is why
    # we cannot do `os.environ.setdefault(...)` after `import cognee`.
    COGNEE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("COGNEE_DATA_DIR", str(COGNEE_DATA_DIR))
    # Disable backend auth — solo-dev local use, no multi-tenant need.
    os.environ.setdefault("ENABLE_BACKEND_ACCESS_CONTROL", "false")
    # Default to Ollama for both LLM and embeddings — preserves LLM-agnostic axiom.
    os.environ.setdefault("LLM_PROVIDER", "ollama")
    os.environ.setdefault("LLM_ENDPOINT", "http://localhost:11434")
    os.environ.setdefault("LLM_MODEL", "qwen3:0.6b")
    os.environ.setdefault("EMBEDDING_PROVIDER", "ollama")
    os.environ.setdefault("EMBEDDING_MODEL", "mxbai-embed-large")
    os.environ.setdefault("VECTOR_DB_PROVIDER", "lancedb")

    # NOW it's safe to import — env vars are in place.
    try:
        import cognee
    except Exception as e:
        print(
            f"cognee_sync: SKIP — `cognee` installed but import failed: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 0

    if args.file:
        file_path = Path(args.file).resolve()
        # Containment guard: --file must resolve inside ROOT (no .. escape).
        if not file_path.is_relative_to(ROOT):
            print(
                f"cognee_sync: --file must be inside ROOT ({ROOT}), got {file_path}",
                file=sys.stderr,
            )
            return 1
        pages = [file_path]
    else:
        pages = _collect_pages()

    print(f"cognee_sync: {len(pages)} page(s) to sync {'(dry-run)' if args.dry_run else ''}")

    if args.dry_run:
        for p in pages:
            print(f"  WOULD SYNC: {p.relative_to(ROOT).as_posix()}")
        return 0

    ok, fail = 0, 0
    for p in pages:
        rel = p.relative_to(ROOT).as_posix()
        status = _sync_one(cognee, p)
        if status == "ok":
            ok += 1
            print(f"  OK:    {rel}")
        else:
            fail += 1
            print(f"  FAIL:  {rel} — {status}")

    print(f"\ncognee_sync: {ok} added, {fail} failed.")

    if args.skip_cognify:
        print("Skipping cognify (graph build) — run without --skip-cognify to build the graph.")
        return 0

    print("cognee_sync: building graph (cognify)... this may take several minutes.")
    cognify_status = _cognify(cognee)
    if cognify_status == "ok":
        print("cognee_sync: graph built. Use cognee.search() to query.")
    else:
        print(f"cognee_sync: cognify failed — {cognify_status}")
        return 2

    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
