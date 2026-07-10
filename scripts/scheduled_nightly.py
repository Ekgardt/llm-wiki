"""Nightly consolidation — runs at 03:00 via Windows Task Scheduler.

What it does:
1. Drain any pending queue tasks (deferred LLM work).
2. Force-spawn compile to process all uncompiled daily logs.
3. Run lint and append to a rolling log file.

Designed to be invoked by Task Scheduler; never requires user interaction.
All output goes to $LLM_WIKI_STATE_ROOT\\logs\\nightly-YYYY-MM-DD.log.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import maybe_compile  # noqa: E402
from memory_state import REPORTS_DIR, ROOT  # noqa: E402


def _wait_for_compile_idle(log_fn) -> None:
    """If a compile is already running, wait (up to 3 retries × 10 s).

    Scheduled passes run unattended and must not skip compile just because
    a previous compile (triggered by a hook) is still running.
    """
    for attempt in range(3):
        st = maybe_compile.status()
        if not st["compile_running"]:
            return
        log_fn(f"  compile running ({st['reason']}), waiting 10s (attempt {attempt + 1}/3)...")
        time.sleep(10)


def main() -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = REPORTS_DIR / f"nightly-{today}.md"
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
        print(line)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    log(f"=== Nightly consolidation pass — {today} ===")
    failures = 0

    # Step 1: drain deferred queue.
    log("Step 1: draining deferred memory queue...")
    import subprocess
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "memory_queue.py"), "drain"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600,
    )
    log(f"  drain result: {r.stdout.strip() or r.stderr.strip() or '(empty)'}")
    if r.returncode != 0:
        failures += 1

    # Step 2: maybe_compile (will spawn compile if there's pending work).
    _wait_for_compile_idle(log)
    log("Step 2: triggering compile (if needed)...")
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "maybe_compile.py")],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60,
    )
    log(f"  maybe_compile: {r.stdout.strip()}")
    if r.returncode != 0:
        failures += 1

    # Step 3: structural lint (cheap, no LLM).
    log("Step 3: structural lint...")
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "lint_memory.py")],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120,
    )
    for line in r.stdout.splitlines()[-5:]:
        log(f"  lint: {line}")
    if r.returncode != 0:
        failures += 1

    # Step 3b: rebuild FTS5 search index (cheap, no LLM, <1s for 100 pages).
    log("Step 3b: rebuilding FTS5 search index...")
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "search_memory.py"), "--rebuild"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60,
    )
    log(f"  search: {r.stdout.strip()}")
    if r.returncode != 0:
        failures += 1

    # Step 3c: rebuild graph-neighbor link cache (for 3rd retrieval signal).
    log("Step 3c: rebuilding wikilink graph cache...")
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from graph_neighbors import rebuild_graph_cache
        edges = rebuild_graph_cache()
        log(f"  graph: {edges} edges cached")
    except Exception as e:
        log(f"  graph: failed ({e})")
        failures += 1

    # Step 4: prune old nightly logs (>30 days).
    log("Step 4: pruning old nightly reports...")
    pruned = 0
    cutoff = datetime.now().timestamp() - (30 * 86400)
    for p in REPORTS_DIR.glob("nightly-*.md"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                pruned += 1
        except OSError:
            pass
    log(f"  pruned {pruned} old report(s)")

    log(f"=== Nightly pass complete (failures={failures}) ===")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
