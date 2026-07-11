"""Print the recommended `/knowledge-lookup` retrieval tier for the current vault size.

Thresholds are deliberately conservative — Karpathy's thread claims direct-read
works to ~100 articles / ~400K words, and coleam00 places the RAG-necessary
breakpoint at ~2000 articles. We pick three tiers inside that range:

  DIRECT  (< 50 wiki pages)    — read knowledge/index.md + target pages; skip QMD.
  HYBRID  (50–300 wiki pages)  — wiki-first, fall back to `qmd search`
                                  (BM25) and `qmd query` (hybrid lex+vec)
                                  only when the direct read is unconvincing.
  QMD     (> 300 wiki pages)   — `qmd query` primary for retrieval, then
                                  Read the top-k results; index.md becomes
                                  navigation, not the retrieval surface.

Usage:
    python scripts/lookup_mode.py            # print tier + counts
    python scripts/lookup_mode.py --json     # machine-readable output
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Force utf-8 on stdout so the en-dash / em-dash don't mojibake on Windows cp1252.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT  # noqa: E402
from vault_editorial import EDITORIAL_NAMES, editorial_parents_to_skip  # noqa: E402

WIKI = ROOT / "knowledge" / "notes"

TIERS = [
    (50, "DIRECT"),
    (301, "HYBRID"),
    (float("inf"), "QMD"),
]


def count_wiki_pages() -> int:
    """Count curated content pages under `knowledge/notes/`.

    Exempts editorial metadata (index/log/state/etc. — see
    `vault_editorial.EDITORIAL_NAMES`) and skeleton directories like
    `knowledge/projects/_template/`. The resulting count drives the retrieval
    tier recommendation.
    """
    if not WIKI.exists():
        return 0
    skip_parents = editorial_parents_to_skip(WIKI)
    return sum(
        1 for p in WIKI.rglob("*.md")
        if p.is_file()
        and p.name not in EDITORIAL_NAMES
        and not any(sp in p.parents for sp in skip_parents)
    )


def tier_for(count: int) -> str:
    for cap, name in TIERS:
        if count < cap:
            return name
    return "QMD"


def qmd_status() -> dict:
    """Best-effort: inspect QMD index state.

    Strategy:
      1. Look for the index file. Locations, in priority order:
         - `$QMD_INDEX` env var (explicit override)
         - `$LLM_WIKI_STATE_ROOT/cache/index.sqlite` (explicit state root)
         - `<vault>/cache/index.sqlite` (default — runtime inside vault)
         - legacy `<vault>/qmd/index.sqlite` (pre-restructure)
         The file's mtime tells us when QMD last wrote.
      2. Try `qmd status` for richer detail. On Windows the `qmd` binary
         is often a shim only visible to bash/Git-Bash, not cmd.exe — so
         failure here is expected and non-fatal.
    """
    import os

    info: dict = {"available": False}

    # 1. Direct inspection of the index file.
    #    All path candidates derive from env vars or the vault location —
    #    no machine-specific absolute paths hardcoded here.
    state_root_env = os.environ.get("LLM_WIKI_STATE_ROOT")
    candidates: list[Path] = []
    qmd_index_env = os.environ.get("QMD_INDEX")
    if qmd_index_env:
        candidates.append(Path(qmd_index_env))
    if state_root_env:
        candidates.append(Path(state_root_env) / "cache" / "index.sqlite")
        candidates.append(Path(state_root_env) / "qmd" / "index.sqlite")  # legacy
    # Vault-root default (matches memory_state.py convention — runtime
    # cache/ lives inside the vault, gitignored).
    candidates.append(ROOT / "cache" / "index.sqlite")
    candidates.append(ROOT / "qmd" / "index.sqlite")  # legacy
    for c in candidates:
        try:
            if c and c.exists() and c.is_file():
                mtime = datetime.fromtimestamp(c.stat().st_mtime, tz=timezone.utc)
                age_h = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600
                info.update({
                    "available": True,
                    "index_path": str(c),
                    "index_size_mb": round(c.stat().st_size / (1024 * 1024), 2),
                    "index_age_hours": round(age_h, 1),
                    "index_stale": age_h > 24,
                })
                break
        except OSError:
            continue

    # 2. Best-effort CLI parse (ignored on Windows/cmd.exe where qmd is a bash shim).
    #    List-args only — never shell=True (security invariant).
    try:
        out = subprocess.run(
            ["qmd", "status"], capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        if out.returncode == 0 and out.stdout:
            for i, ln in enumerate(out.stdout.splitlines()):
                if "wiki (qmd://knowledge/notes/" in ln:
                    for look in out.stdout.splitlines()[i : i + 4]:
                        if "Files:" in look:
                            info["wiki_files_indexed"] = look.split("Files:")[-1].strip()
                        if "updated" in look.lower():
                            info["wiki_updated"] = look.strip()
                    break
    except (OSError, subprocess.TimeoutExpired):
        pass
    return info


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    count = count_wiki_pages()
    tier = tier_for(count)
    qmd = qmd_status()

    payload = {
        "wiki_pages": count,
        "recommended_tier": tier,
        "thresholds": {"DIRECT": "<50", "HYBRID": "50–300", "QMD": ">300"},
        "qmd": qmd,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"Wiki pages (curated, excl. editorial): {count}")
    print(f"Recommended tier: {tier}")
    print("Thresholds: DIRECT < 50  |  HYBRID 50–300  |  QMD > 300")
    if qmd.get("available"):
        print(f"QMD index: {qmd.get('index_path')} ({qmd.get('index_size_mb')} MB)")
        age_h = qmd.get("index_age_hours")
        if age_h is not None:
            stale = " [STALE >24h]" if qmd.get("index_stale") else ""
            print(f"QMD index age: {age_h} hours{stale}")
        if "wiki_files_indexed" in qmd:
            print(f"QMD wiki files indexed: {qmd['wiki_files_indexed']}")
        if qmd.get("index_stale") and tier != "DIRECT":
            print("Tip: run `qmd update` then `qmd embed` to refresh before querying.")
    else:
        print("QMD index not found — staying on DIRECT tier.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
