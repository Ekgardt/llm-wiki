"""Answer a question from memory knowledge.

Usage:
    uv run python scripts/query_memory.py "how do we handle preliminary flagging?"
    uv run python scripts/query_memory.py "..." --file-back

With --file-back, also writes the Q&A as `memory/knowledge/qa/<slug>.md`,
regenerates the memory index, and appends to memory/log.md.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT  # noqa: E402

MEMORY = ROOT / "memory"
INDEX = MEMORY / "index.md"
LOG = MEMORY / "log.md"
QA_DIR = MEMORY / "knowledge" / "qa"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("question", type=str)
    p.add_argument("--file-back", action="store_true")
    return p.parse_args()


def slugify(s: str, max_len: int = 60) -> str:
    """Produce a filesystem-safe slug from a natural-language question.

    Unicode-aware: preserves Cyrillic, Latin, digits, and any other
    alphanumerics (via `\\w`). Previously `[^a-z0-9]+` stripped
    everything non-ASCII, collapsing every Russian question to
    ``"question"`` and causing QA pages to overwrite each other.

    Collision guard: if multiple questions sanitize to the same slug
    (e.g. differing only by punctuation), append a short hash of the
    original question to keep pages distinct. The hash is
    deterministic so the same question maps to the same slug across
    runs.
    """
    import hashlib
    s_norm = s.lower().strip()
    # \w = [A-Za-z0-9_] in ASCII mode, but with re.UNICODE (Python 3
    # default for str patterns) it matches any letter/digit in any
    # script. Unsafe chars become hyphens; collapse runs.
    slug = re.sub(r"[^\w]+", "-", s_norm, flags=re.UNICODE).strip("-_")
    # Always append a deterministic hash so that questions differing
    # only by punctuation / emoji / non-word chars still get distinct
    # filenames. Without this, "???" / "!!!" / "💥" would all collapse
    # to `question` and overwrite each other.
    short_hash = hashlib.sha256(s_norm.encode("utf-8")).hexdigest()[:6]
    if not slug:
        # Pure-punctuation / emoji-only input — no usable prefix. Still
        # disambiguate via the hash alone.
        return f"question-{short_hash}"
    head = slug[: max(0, max_len - 7)]  # -7 for "-<6hex>"
    return f"{head}-{short_hash}" if head else f"question-{short_hash}"


def answer(question: str) -> str:
    """Answer a question using only content under `memory/`.

    Uses the unified llm_client (Codex CLI / OpenAI / Ollama). The
    LLM does not get tool use — it sees the memory index inline and
    reasons over what's described there. For deeper retrieval, the
    user should pre-fetch relevant pages and include their content
    in the question.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from llm_client import call_llm
    except ImportError:
        return "(llm_client not available)"

    index_txt = INDEX.read_text(encoding="utf-8") if INDEX.exists() else ""
    prompt = f"""Answer the question below using ONLY the memory index
provided. The index lists all available knowledge pages with their
one-sentence summaries. If a relevant page exists, mention its path
and summarize what it says based on the summary. If no relevant page
exists, say so plainly.

Respond in this shape:

**Answer:** ...

**Sources:**
- `memory/knowledge/.../...md`
- ...

**Confidence:** high | medium | low — why.

--- memory/index.md ---
{index_txt}

--- question ---
{question}
"""
    text = call_llm(
        prompt,
        system_prompt="Answer from project memory. Cite paths. If memory is silent, say so.",
        max_tokens=1500,
    )
    return text.strip() if text else "(no LLM response)"


def file_back(question: str, answer_text: str) -> Path:
    QA_DIR.mkdir(parents=True, exist_ok=True)
    slug = slugify(question)
    out = QA_DIR / f"{slug}.md"
    today = datetime.now().strftime("%Y-%m-%d")
    summary_line = question.strip().rstrip("?").strip()
    page = f"""# {question.strip().rstrip('?')}?

One-sentence summary: Settled answer to "{summary_line}" captured on {today}.

## Question
{question.strip()}

## Answer
{answer_text}

## Evidence
- Captured by `scripts/query_memory.py --file-back` on {today}.

## Related
-
"""
    out.write_text(page, encoding="utf-8")
    return out


def rebuild_index() -> bool:
    """Run the memory index rebuild. Returns True on success.

    Callers should surface a warning if False — the page was written
    correctly, but `memory/index.md` is now stale until the next
    successful rebuild.
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
        print(f"query_memory: rebuild_memory_index FAILED (rc={result.returncode}): {err}")
        return False
    return True


def append_log(entry: str) -> None:
    if not LOG.exists():
        LOG.write_text("# Session Memory Log\n\n", encoding="utf-8")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(entry if entry.endswith("\n") else entry + "\n")


def main() -> int:
    args = parse_args()
    answer_text = answer(args.question)
    print(answer_text)

    if args.file_back:
        out = file_back(args.question, answer_text)
        index_ok = rebuild_index()
        suffix = "" if index_ok else " (WARN: memory/index.md rebuild failed — page written, index stale)"
        append_log(
            f"- {datetime.now().strftime('%Y-%m-%d')} — Filed Q&A `{out.relative_to(ROOT).as_posix()}` via `query_memory.py --file-back`.{suffix}"
        )
        print(f"\n[filed] {out.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
