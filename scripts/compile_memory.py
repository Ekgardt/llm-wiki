"""Compile memory/daily/*.md into memory/knowledge/* durable pages.

CLI:
    uv run python scripts/compile_memory.py              # compile changed daily logs
    uv run python scripts/compile_memory.py --all        # compile every daily log
    uv run python scripts/compile_memory.py --file PATH  # compile one daily log
    uv run python scripts/compile_memory.py --dry-run    # plan only, no writes
    uv run python scripts/compile_memory.py --trigger auto|manual
                                                         # records invocation source in
                                                         # state.json; `auto` is set by
                                                         # flush_memory.py when the 18:00
                                                         # hook spawns this compile, any
                                                         # direct CLI run defaults to
                                                         # `manual`. Surfaces as
                                                         # "Automated compile pass" vs
                                                         # "Manual compile pass" in
                                                         # memory/log.md.

Incrementality:
    SHA-256 hashes of each daily log are tracked in $LLM_WIKI_STATE_ROOT/memory-state/state.json under
    `compiled_daily_hashes`. Runs without --all/--file skip logs whose hash matches
    the last compile.

After writing, runs `scripts/rebuild_memory_index.py` and appends to memory/log.md.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT, STATE_ROOT, file_hash, load_state, update_state  # noqa: E402

MEMORY = ROOT / "memory"
DAILY_DIR = MEMORY / "daily"
KNOWLEDGE = MEMORY / "knowledge"
AGENTS = MEMORY / "AGENTS.md"
INDEX = MEMORY / "index.md"
LOG = MEMORY / "log.md"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--all", action="store_true")
    p.add_argument("--file", type=str, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--trigger",
        choices=["auto", "manual"],
        default="manual",
        help="Source of invocation. 'auto' is set by flush_memory when a hook "
        "fires the compile; any direct CLI run defaults to 'manual'.",
    )
    return p.parse_args()


def select_dailies(args: argparse.Namespace, state: dict) -> list[Path]:
    if args.file:
        return [Path(args.file).resolve()]
    all_dailies = sorted(DAILY_DIR.glob("*.md"))
    if args.all:
        return all_dailies
    compiled = state.get("compiled_daily_hashes", {})
    changed: list[Path] = []
    for p in all_dailies:
        if compiled.get(p.name) != file_hash(p):
            changed.append(p)
    return changed


def _extract_title_and_summary(path: Path) -> tuple[str, str]:
    """Parse first H1 and `One-sentence summary:` line from a knowledge page.

    Used to give the compiler enough context to detect semantic overlap,
    not just filename collisions. Falls back to (filename-stem, '') when
    the page lacks the conventional headers.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return path.stem, ""
    title = ""  # empty until we find an H1
    summary = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not title and stripped.startswith("# ") and not stripped.startswith("## "):
            title = stripped[2:].strip()
        elif stripped.lower().startswith("one-sentence summary:"):
            summary = stripped.split(":", 1)[1].strip()
        if title and summary:
            break
    # Fall back to filename stem if no H1 was found
    if not title:
        title = path.stem
    return title, summary


def existing_knowledge_snapshot() -> str:
    """Return existing knowledge pages WITH title + summary for dedup.

    Previously returned only filenames, which left the LLM unable to
    detect semantic overlap (a new page about "hook failure modes"
    would not match an existing "hook-scripts-defense-in-depth" by
    slug alone). The enriched snapshot lets the compiler satisfy the
    DEDUP-BEFORE-CREATE rule from the prompt.

    Format per entry:
        - <category>/<file>.md «<title>» — <summary>

    Falls back gracefully:
        - title only (no summary line) → «<title>»
        - summary only (no H1)          → — <summary>
        - neither                       → bare filename
    """
    lines: list[str] = []
    for cat in ("concepts", "decisions", "patterns", "debugging", "qa"):
        d = KNOWLEDGE / cat
        if not d.exists():
            continue
        for md in sorted(d.glob("*.md")):
            title, summary = _extract_title_and_summary(md)
            head = f"- {cat}/{md.name}"
            # Use «» around title to visually distinguish from summary
            # and to give the LLM a clear "title goes here" anchor.
            if title and title != md.stem and summary:
                head += f" — «{title}»: {summary}"
            elif title and title != md.stem:
                head += f" — «{title}»"
            elif summary:
                head += f" — {summary}"
            lines.append(head)
    return "\n".join(lines) or "(no pages yet)"


def run_compile(daily_paths: list[Path], dry_run: bool) -> tuple[list[str], str]:
    """Run the LLM compile pass via structured JSON protocol.

    Phase 4+ refactor: removed the claude_agent_sdk dependency (which
    required Claude API auth). Now uses the unified llm_client (Codex
    CLI / OpenAI / Ollama) and a JSON-based output protocol. The LLM
    returns a structured plan; Python performs the file writes and
    verifies citations deterministically.

    Returns (touched_paths, raw_audit_text).
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from llm_client import call_llm
    except ImportError:
        return [], "(llm_client not available)"

    agents_md = AGENTS.read_text(encoding="utf-8") if AGENTS.exists() else ""
    log_tail = ""
    if LOG.exists():
        log_tail = "\n".join(LOG.read_text(encoding="utf-8").splitlines()[-25:])
    knowledge_list = existing_knowledge_snapshot()
    daily_blob = "\n\n".join(
        f"### FILE: {p.relative_to(ROOT).as_posix()}\n{p.read_text(encoding='utf-8', errors='ignore')}"
        for p in daily_paths
    )

    prompt = f"""You are a SKEPTICAL memory editor for an LLM-wiki vault. Your default
action is to lift NOTHING. You lift a page only when you can quote the
exact source text that supports each claim. You prefer updating an
existing page 10:1 over creating a new one. You never embellish, never
infer beyond the source, and never silently rewrite "uncommitted" as
"committed" to make a cleaner narrative.

=== TASK ===
Read the daily logs below. Extract durable, reusable knowledge that
would help a future session in this project. Skip status chatter.

=== HARD RULES ===
1. VERIFY-BEFORE-WRITE: every evidence entry MUST include a `quoted_text`
   field with the EXACT text from the cited daily-log timestamp block.
   Python will verify this literal substring exists in the source —
   fabricated quotes will fail and the operation will be dropped.
2. DO-NOT-LIFT: status, task progress, file-path restatements, code-
   structure summaries, unvalidated speculation, raw/inbox summaries.
3. LIFT GATE: reusable across sessions AND not derivable from code
   AND specific enough ("when X, do Y because Z").
4. DEDUP-BEFORE-CREATE: check existing pages list (titles + summaries
   provided below). If overlap exists, use action="update" instead of
   "create".
5. SKIP-STUBS: daily-log blocks with only Trigger/slug/root metadata
   → skip silently, count as stub in audit.
6. LENGTH: target 150-400 words per page body.

=== CATEGORIES ===
- concepts (noun) / decisions (dated choice) / patterns (verb) /
  debugging (symptom→cause→fix) / qa (settled answer)
- Tiebreak: patterns > concepts; debugging > qa.

=== EXISTING PAGES (title + summary — for DEDUP) ===
{knowledge_list}

=== memory/AGENTS.md (full contract) ===
{agents_md}

=== memory/log.md (tail) ===
{log_tail}

=== DAILY LOGS TO COMPILE ===
{daily_blob}

=== OUTPUT: STRICT JSON (no markdown fences, no prose) ===
Return a single JSON object with this exact shape:

{{
  "operations": [
    {{
      "action": "create" | "update",
      "category": "concepts" | "decisions" | "patterns" | "debugging" | "qa",
      "slug": "<kebab-case-filename-without-extension>",
      "title": "<page H1 title>",
      "summary": "<one-sentence summary>",
      "body_section": "Lesson" | "Decision" | "Symptom / Cause / Resolution" | "Answer",
      "body_markdown": "<150-400 words of lesson/decision/symptom content>",
      "evidence": [
        {{
          "daily_date": "<YYYY-MM-DD>",
          "timestamp": "<HH:MM:SS>",
          "quoted_text": "<EXACT substring from the cited block>",
          "claim": "<one-line statement of what this evidence supports>"
        }}
      ],
      "related": ["[[<slug>]]", "[[<slug>]]"]
    }}
  ],
  "audit": {{
    "verified": <int count of evidence citations the LLM checked>,
    "dedup": <int count of existing pages scanned for overlap>,
    "stubs": <int count of daily-log blocks skipped as metadata-only>,
    "contradictions": <int count of conflicts with existing pages>,
    "rejected": <int count of candidate pages dropped as below-threshold>
  }}
}}

If nothing is worth lifting, return:
{{"operations": [], "audit": {{"verified": 0, "dedup": 0, "stubs": <count>, "contradictions": 0, "rejected": 0}}}}

Output ONLY the JSON object. No markdown fences, no commentary, no
"here is the JSON" preamble.
"""

    system_prompt = (
        "You are a skeptical memory editor for a personal LLM-wiki vault. "
        "Your output is parsed as JSON by a strict parser — any non-JSON "
        "content causes the whole compile to fail. Be conservative: when "
        "in doubt, return fewer operations. Empty operations list is a "
        "valid and acceptable response."
    )
    raw = call_llm(prompt, system_prompt, max_tokens=4000)

    if not raw or not raw.strip():
        return [], "(no LLM response)"

    # Extract JSON from the response (LLM may wrap in fences despite
    # instructions — handle both bare JSON and ```json-fenced JSON).
    json_text = _extract_json_block(raw)
    if not json_text:
        return [], f"(no JSON found in response; first 200 chars: {raw[:200]})"

    try:
        plan = json.loads(json_text)
    except json.JSONDecodeError as e:
        return [], f"(JSON parse failed: {e}; first 200 chars: {json_text[:200]})"

    # Execute the plan deterministically — this is where Python writes
    # files and verifies citations. Returns (touched, audit_text).
    return _execute_plan(plan, daily_paths, dry_run)


def _extract_json_block(text: str) -> str:
    """Pull the JSON object out of a possibly-fenced response."""
    s = text.strip()
    # Strip markdown code fences if present.
    if s.startswith("```"):
        lines = s.splitlines()
        # Remove first fence line.
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        # Remove trailing fence line.
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    # Find the outermost { ... } block.
    if "{" in s:
        start = s.index("{")
        end = s.rindex("}")
        if end > start:
            return s[start : end + 1]
    return ""


def _verify_evidence(
    evidence_entries: list[dict],
    daily_paths: list[Path],
) -> tuple[int, int]:
    """Deterministic citation check. Returns (verified_count, failed_count).

    For each evidence entry, locate the cited daily log + timestamp
    block, then check that `quoted_text` literally appears in that
    block. This is the Python-side enforcement of VERIFY-BEFORE-WRITE
    — the LLM cannot fake this check.
    """
    daily_by_date: dict[str, str] = {}
    for p in daily_paths:
        # filename like "2026-04-19.md"
        daily_by_date[p.stem] = p.read_text(encoding="utf-8", errors="ignore")

    verified = 0
    failed = 0
    for entry in evidence_entries or []:
        date = entry.get("daily_date", "")
        ts = entry.get("timestamp", "")
        quoted = entry.get("quoted_text", "")
        if not (date and ts and quoted):
            failed += 1
            continue
        body = daily_by_date.get(date)
        if not body:
            failed += 1
            continue
        # Locate the [HH:MM:SS] block in the daily log.
        # Block headers look like "## [17:24:33] session-end | ..."
        # Find the [ts] marker and grab text until the next ## [ header.
        marker = f"[{ts}]"
        marker_pos = body.find(marker)
        if marker_pos < 0:
            failed += 1
            continue
        # Block extends from marker_pos to next "## [" or end of file.
        next_header = body.find("\n## [", marker_pos + 1)
        block_text = (
            body[marker_pos : next_header]
            if next_header > 0
            else body[marker_pos:]
        )
        # Verify the quoted_text literally appears in this block.
        # Tolerate whitespace differences by comparing on a single-line basis.
        quoted_clean = " ".join(quoted.split())
        block_clean = " ".join(block_text.split())
        if quoted_clean and quoted_clean in block_clean:
            verified += 1
        else:
            failed += 1
    return verified, failed


def _check_contradictions_pre_write(
    category: str,
    new_slug: str,
    new_title: str,
    new_body: str,
) -> list[Path]:
    """Check if a new page would contradict existing pages in the same category.

    Simple heuristic: if an existing page in the same category has a
    similar title or summary AND the new body contains negation patterns
    ("instead of", "not anymore", "replaced by", "superseded"),
    treat it as a potential contradiction and return the old page path
    for auto-superseding.

    This is a lightweight real-time check — the full LLM contradiction
    detection still runs in nightly lint. This catches the obvious cases
    without an LLM call.
    """
    target_dir = KNOWLEDGE / category
    if not target_dir.exists():
        return []

    contradictions = []
    new_title_lower = new_title.lower()

    # Negation patterns indicating the new page supersedes an old one
    negation_patterns = [
        r"instead\s+of",
        r"not\s+anymore",
        r"replaced\s+by",
        r"superseded?\s+by",
        r"no\s+longer",
        r"changed\s+from",
        r"migrated?\s+from",
        r"switched?\s+(from|to)",
    ]
    has_negation = any(
        re.search(p, new_body, re.IGNORECASE) for p in negation_patterns
    )

    for existing in target_dir.glob("*.md"):
        if existing.stem == new_slug:
            continue  # same page, skip
        try:
            existing_content = existing.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        # Skip already-superseded pages
        if "superseded_by" in existing_content or "status: superseded" in existing_content:
            continue

        # Check title similarity
        existing_title_match = re.search(r"^#\s+(.+?)\s*$", existing_content, re.MULTILINE)
        if not existing_title_match:
            continue
        existing_title = existing_title_match.group(1).lower()

        # Simple word overlap check
        new_words = set(new_title_lower.split())
        old_words = set(existing_title.split())
        common = new_words & old_words
        # Need at least 2 meaningful words in common (skip stop words)
        stop = {"the", "a", "an", "for", "of", "to", "in", "and", "with", "mode", "hook"}
        meaningful = common - stop
        if len(meaningful) >= 2 and has_negation:
            contradictions.append(existing)

    return contradictions


def _execute_plan(
    plan: dict,
    daily_paths: list[Path],
    dry_run: bool,
) -> tuple[list[str], str]:
    """Apply the LLM's plan to disk. Returns (touched_paths, audit_text).

    For each operation:
    - Verify every evidence citation (deterministic). If any fails,
      DROP the operation entirely (safer than writing unverified claims).
    - Build the page markdown with OKF frontmatter.
    - For action="create": write if file doesn't exist.
    - For action="update": append a new section to existing file.
    """
    import re as _re

    operations = plan.get("operations", []) or []
    audit_in = plan.get("audit", {}) or {}
    touched: list[str] = []
    dropped: list[dict] = []
    citations_verified = 0
    citations_failed = 0

    for op in operations:
        action = op.get("action", "create")
        category = op.get("category", "patterns")
        slug = op.get("slug", "")
        if not slug:
            continue
        # Sanitize slug: lowercase, kebab-case, no extension.
        slug = _re.sub(r"[^a-z0-9-]", "-", slug.lower()).strip("-")
        if not slug:
            continue

        target_dir = KNOWLEDGE / category
        target_path = target_dir / f"{slug}.md"

        # VERIFY evidence for this operation.
        ev_entries = op.get("evidence", []) or []
        v, f = _verify_evidence(ev_entries, daily_paths)
        citations_verified += v
        citations_failed += f
        if f > 0:
            # Any failed citation → drop the page. This is the
            # Python-side enforcement of VERIFY-BEFORE-WRITE.
            dropped.append(
                {
                    "slug": slug,
                    "reason": f"{f} citation(s) failed verification",
                }
            )
            continue

        # REAL-TIME CONTRADICTION CHECK (Dorabotka B):
        # Before writing, check if this new page contradicts an existing
        # page. If the body_markdown contains claims that directly oppose
        # an existing knowledge page with the same category, flag it.
        body_md = op.get("body_markdown", "")
        title = op.get("title", slug)
        contradictions = _check_contradictions_pre_write(
            category, slug, title, body_md
        )
        if contradictions:
            # Auto-supersede: mark existing page as superseded by the new one
            for old_path in contradictions:
                try:
                    old_content = old_path.read_text(encoding="utf-8")
                    # Add supersede marker if not already present
                    if "superseded_by" not in old_content:
                        supersede_note = (
                            f"\n\n## Superseded ({datetime.now().strftime('%Y-%m-%d')})\n"
                            f"This page has been superseded by "
                            f"[[memory/knowledge/{category}/{slug}]].\n"
                        )
                        old_path.write_text(
                            old_content.rstrip() + supersede_note,
                            encoding="utf-8",
                        )
                except OSError:
                    pass

        if dry_run:
            touched.append(str(target_path.relative_to(ROOT).as_posix()))
            continue

        target_dir.mkdir(parents=True, exist_ok=True)

        # Build the page markdown.
        title = op.get("title", slug.replace("-", " ").title())
        summary = op.get("summary", "")
        body_section = op.get("body_section", "Lesson")
        body_md = op.get("body_markdown", "")
        related = op.get("related", []) or []

        # Evidence section: list each citation with its claim.
        evidence_lines: list[str] = []
        for ev in ev_entries:
            daily_date = ev.get("daily_date", "")
            ts = ev.get("timestamp", "")
            claim = ev.get("claim", "")
            evidence_lines.append(
                f"- `memory/daily/{daily_date}.md [{ts}]` — {claim}"
            )
        evidence_section = (
            "\n\n## Evidence\n" + "\n".join(evidence_lines)
            if evidence_lines
            else ""
        )

        # Related section.
        related_section = ""
        if related:
            related_section = "\n\n## Related\n" + "\n".join(f"- {r}" for r in related)

        frontmatter = (
            "---\n"
            f"type: {category.rstrip('s') if category.endswith('s') else category}\n"
            f'title: "{title}"\n'
            f'description: "{summary}"\n'
            f"timestamp: {datetime.now().isoformat(timespec='seconds')}\n"
            "---\n\n"
        )

        page_content = (
            frontmatter
            + f"# {title}\n\n"
            + f"One-sentence summary: {summary}\n\n"
            + f"## {body_section}\n{body_md}"
            + evidence_section
            + related_section
            + "\n"
        )

        if action == "create" and not target_path.exists():
            target_path.write_text(page_content, encoding="utf-8")
            touched.append(str(target_path.relative_to(ROOT).as_posix()))
        elif action == "update" and target_path.exists():
            # Append a new "## Update (YYYY-MM-DD)" section to existing.
            existing = target_path.read_text(encoding="utf-8")
            update_block = (
                f"\n\n## Update ({datetime.now().strftime('%Y-%m-%d')})\n"
                f"{body_md}{evidence_section}\n"
            )
            target_path.write_text(existing.rstrip() + update_block, encoding="utf-8")
            touched.append(str(target_path.relative_to(ROOT).as_posix()))
        elif action == "create" and target_path.exists():
            # File exists — convert to update instead.
            existing = target_path.read_text(encoding="utf-8")
            update_block = (
                f"\n\n## Update ({datetime.now().strftime('%Y-%m-%d')})\n"
                f"{body_md}{evidence_section}\n"
            )
            target_path.write_text(existing.rstrip() + update_block, encoding="utf-8")
            touched.append(str(target_path.relative_to(ROOT).as_posix()))

    # Build the audit text in the legacy COMPILE_DONE / COMPILE_AUDIT
    # format so existing parsers continue to work.
    stubs_count = int(audit_in.get("stubs", 0))
    rejected_count = int(audit_in.get("rejected", 0)) + len(dropped)
    dedup_count = int(audit_in.get("dedup", 0))
    contradictions_count = int(audit_in.get("contradictions", 0))

    audit_text = (
        f"COMPILE_DONE: {len(touched)} page(s) touched: {', '.join(touched)}\n"
        f"COMPILE_AUDIT: verified {citations_verified} evidence citations; "
        f"{dedup_count} dedup checks performed; {stubs_count} stubs skipped; "
        f"{contradictions_count} contradictions handled; "
        f"{rejected_count} pages rejected as below-threshold"
    )
    if dropped:
        audit_text += "\n\nDropped operations (citation verification failed):"
        for d in dropped:
            audit_text += f"\n  - {d['slug']}: {d['reason']}"

    return touched, audit_text


def parse_compile_audit(raw: str) -> dict:
    """Extract structured audit counts from a COMPILE_AUDIT line.

    The new prompt emits a self-audit sentinel alongside COMPILE_DONE:
        COMPILE_AUDIT: verified <a> evidence citations; <b> dedup checks
        performed; <c> stubs skipped; <d> contradictions handled;
        <e> pages rejected as below-threshold

    Returns a dict with keys verified/dedup/stubs/contradictions/rejected
    (ints) or empty dict if the line is absent (e.g. legacy compiles,
    pre-upgrade runs). Tolerant of missing fields — accepts partial
    audits. Used by `_run` to surface audit signal in state.json so
    operators can detect regressions ("verified=0 but touched=5" is a
    red flag the LLM skipped the verify step).
    """
    if not raw or not raw.strip():
        return {}
    audit_line = ""
    for line in raw.splitlines()[::-1]:
        stripped = line.strip()
        if stripped.startswith("COMPILE_AUDIT:"):
            audit_line = stripped
            break
    if not audit_line:
        return {}
    body = audit_line.split(":", 1)[1]
    out: dict[str, int] = {}
    # Format emitted by the new prompt (number comes BEFORE the descriptor):
    #   "verified 7 evidence citations; 12 dedup checks performed; 2 stubs
    #    skipped; 1 contradictions handled; 0 pages rejected as below-threshold"
    mappings = [
        ("verified", r"verified\s+(\d+)\s+evidence"),
        ("dedup", r"(\d+)\s+dedup checks"),
        ("stubs", r"(\d+)\s+stubs skipped"),
        ("contradictions", r"(\d+)\s+contradictions handled"),
        ("rejected", r"(\d+)\s+pages rejected"),
    ]
    import re

    for key, pattern in mappings:
        m = re.search(pattern, body, re.IGNORECASE)
        if m:
            out[key] = int(m.group(1))
    return out


def rebuild_index() -> bool:
    """Run the index rebuild. Returns True on success.

    Previously called with `check=False` and the return value was
    ignored, so a failing rebuild (e.g. hardcoded-path regression)
    would silently leave `memory/index.md` stale while the compile
    flow claimed success.
    """
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "rebuild_memory_index.py")],
        check=False,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:500]
        print(f"compile_memory: rebuild_memory_index FAILED (rc={result.returncode}): {err}")
        return False
    return True


def _compile_succeeded(raw: str) -> bool:
    """Did the LLM compile run complete with a valid COMPILE_DONE marker?

    Distinguishes three cases:
      - SDK missing: `raw == "(claude-agent-sdk not available)"` → False.
      - Runtime exception: `raw` starts with `"(compile failed:"` → False.
      - LLM produced output but never emitted the COMPILE_DONE marker
        (truncated / rate-limited / crashed mid-response) → False.
      - LLM produced output with a COMPILE_DONE marker → True.

    Used to gate writes to `compiled_daily_hashes`: if a run failed,
    the caller MUST NOT mark the daily as compiled, or the next run
    will skip it and we silently lose pending content.
    """
    if not raw or raw.startswith("("):
        return False
    return "COMPILE_DONE:" in raw


def append_log(entry: str) -> None:
    if not LOG.exists():
        LOG.write_text("# Session Memory Log\n\n", encoding="utf-8")

    content = LOG.read_text(encoding="utf-8")
    line = entry if entry.endswith("\n") else entry + "\n"

    # If an editorial note footer exists, insert before it to preserve
    # the footer's position at the end of the file. Otherwise, simple append.
    marker = "\n## Editorial note"
    if marker in content:
        head, sep, tail = content.partition(marker)
        head_trimmed = head.rstrip() + "\n"
        LOG.write_text(head_trimmed + line + sep + tail, encoding="utf-8")
    else:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line)


def _mark_started(trigger: str) -> None:
    started_iso = datetime.now().isoformat(timespec="seconds")

    def _mutate(s: dict) -> None:
        s["last_compile_started_at"] = started_iso
        s["last_compile_started_trigger"] = trigger
        s["last_compile_status"] = "running"
        s.pop("last_compile_error", None)

    update_state(_mutate)


def _mark_finished(trigger: str, status: str, error: str | None = None) -> None:
    finished_iso = datetime.now().isoformat(timespec="seconds")

    def _mutate(s: dict) -> None:
        s["last_compile_finished_at"] = finished_iso
        s["last_compile_finished_trigger"] = trigger
        s["last_compile_status"] = status
        if error is not None:
            s["last_compile_error"] = error[:500]
        else:
            s.pop("last_compile_error", None)

    update_state(_mutate)
    # Clear the maybe_compile lock so the next trigger knows we're done.
    # Without this, the lock auto-expires after MAX_COMPILE_DURATION_S
    # (30 min) — clearing it explicitly means the next session-end can
    # spawn compile immediately instead of waiting for stale-lock timeout.
    _clear_compile_lock()


def _clear_compile_lock() -> None:
    """Best-effort clear of the maybe_compile PID lock."""
    try:
        lock_file = STATE_ROOT / "memory-state" / "compile.pid"
        if lock_file.exists():
            lock_file.unlink()
    except OSError:
        pass


def main() -> int:
    args = parse_args()
    _mark_started(args.trigger)
    try:
        return _run(args)
    except BaseException as e:  # noqa: BLE001
        _mark_finished(args.trigger, "error", f"{type(e).__name__}: {e}")
        raise


def _run(args: argparse.Namespace) -> int:
    state = load_state()
    dailies = select_dailies(args, state)
    if not dailies:
        print("compile_memory: no changed daily logs; nothing to do.")
        _mark_finished(args.trigger, "ok")
        return 0

    print(f"compile_memory: compiling {len(dailies)} daily log(s){' (dry-run)' if args.dry_run else ''}:")
    for p in dailies:
        print(f"  - {p.relative_to(ROOT).as_posix()}")

    touched, raw = run_compile(dailies, args.dry_run)
    print("--- compile output ---")
    print(raw[-2000:] if raw else "(no output)")

    # Surface the structured self-audit (new in Phase 0). Empty dict
    # means the LLM didn't emit COMPILE_AUDIT — either a legacy
    # behavior or an LLM that skipped the verify step. Either way,
    # the operator gets a visible signal.
    audit = parse_compile_audit(raw)
    if audit:
        print(f"compile_memory: audit — {audit}")
        # Soft warning: pages touched > 0 but verified citations == 0
        # is a strong signal the LLM skipped the VERIFY-BEFORE-WRITE
        # step. We don't fail the run on this (the LLM may have
        # legitimately updated existing pages with already-verified
        # evidence) but we surface it loudly.
        if (
            touched
            and audit.get("verified", 0) == 0
            and not args.dry_run
        ):
            print(
                "compile_memory: WARNING — pages touched but 0 evidence "
                "citations verified. LLM may have skipped VERIFY-BEFORE-WRITE. "
                "Inspect output above before trusting this compile."
            )
    else:
        print(
            "compile_memory: no COMPILE_AUDIT line in output — LLM used "
            "legacy protocol (pre-Phase-0). Consider re-running."
        )

    if args.dry_run:
        print("compile_memory: dry-run, not rebuilding index or updating state.")
        _mark_finished(args.trigger, "ok")
        return 0

    # Gate hash recording on actual compile success. If the LLM call
    # failed (SDK missing, exception, or no COMPILE_DONE marker), the
    # daily MUST NOT be marked as compiled — otherwise the next run
    # will skip it and we lose pending content silently.
    if not _compile_succeeded(raw):
        error_preview = (raw[:300] if raw else "(no output)")
        print(
            f"compile_memory: FAILED — not marking dailies as compiled. "
            f"First 300 chars of output: {error_preview}"
        )
        _mark_finished(
            args.trigger, "error", f"compile_failed: {error_preview}"
        )
        return 1

    # Rebuild index — but don't block hash recording if rebuild fails.
    # The pages ARE written correctly at this point; the index is just
    # a derived navigation artifact that can be re-generated next run.
    # Downgrade the finished status to "warning" so the operator notices.
    index_ok = rebuild_index()

    now_iso = datetime.now().isoformat(timespec="seconds")

    def _mutate(s: dict) -> None:
        s.setdefault("compiled_daily_hashes", {})
        for p in dailies:
            s["compiled_daily_hashes"][p.name] = file_hash(p)
        s["last_compile_at"] = now_iso
        s["last_compile_trigger"] = args.trigger
        s["last_compiled_files"] = [p.name for p in dailies]
        s["last_compiled_touched"] = touched
        s["last_index_rebuild_ok"] = index_ok
        # Phase 0: store the LLM's structured self-audit so operators
        # can track verify-rate over time and detect regression. Empty
        # dict if the LLM didn't emit COMPILE_AUDIT (legacy/old run).
        s["last_compile_audit"] = audit

    update_state(_mutate)

    # Only append to memory/log.md when the compile actually produced durable
    # output. A "no-op compile" (hash changed but nothing worth lifting, or
    # COMPILE_DONE: 0 pages touched) is a runtime event — record it in
    # state.json but do not pollute the knowledge changelog with heartbeat
    # entries. Manual runs still log unconditionally so the operator sees a
    # confirmation in the canonical log.
    sources = ", ".join(p.name for p in dailies)
    if touched:
        touched_str = ", ".join(touched)
        label = "Automated" if args.trigger == "auto" else "Manual"
        append_log(
            f"- {datetime.now().strftime('%Y-%m-%d')} — {label} compile pass over {sources}. Touched: {touched_str}."
        )
    elif args.trigger == "manual":
        append_log(
            f"- {datetime.now().strftime('%Y-%m-%d')} — Manual compile pass over {sources}. No durable content to lift (runtime heartbeat)."
        )
    # Status: "ok" if compile + index both succeeded, "warning" if index
    # rebuild failed (pages are written but memory/index.md is stale;
    # next run will re-attempt rebuild).
    finished_status = "ok" if index_ok else "warning"
    finished_error = None if index_ok else "index_rebuild_failed"
    _mark_finished(args.trigger, finished_status, finished_error)
    print("compile_memory: done." if index_ok else "compile_memory: done (index rebuild FAILED — state marked `warning`).")
    return 0 if index_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
