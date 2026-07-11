"""Answer a question from memory knowledge.

Usage:
    uv run python scripts/query_memory.py "how do we handle preliminary flagging?"
    uv run python scripts/query_memory.py "..." --file-back

With --file-back, also writes the Q&A as `knowledge/notes/<slug>.md`,
regenerates the memory index, and appends to knowledge/log.md.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT  # noqa: E402
from secret_redact import redact_secrets  # noqa: E402

MEMORY = ROOT / "knowledge"
INDEX = MEMORY / "index.md"
LOG = MEMORY / "log.md"
QA_DIR = MEMORY / "notes"  # flat layout: all notes live directly under knowledge/notes/


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
    """Answer a question using only content under `knowledge/`.

    Uses the unified llm_client (Codex CLI / OpenCode / Claude CLI / OpenAI /
    Ollama, auto-detected). The LLM does not get tool use — it sees the
    knowledge index inline and reasons over what's described there. For
    deeper retrieval, the user should pre-fetch relevant pages and include
    their content in the question.
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
- `knowledge/notes/.../...md`
- ...

**Confidence:** high | medium | low — why.

--- knowledge/index.md ---
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
    question = redact_secrets(question)
    answer_text = redact_secrets(answer_text)
    QA_DIR.mkdir(parents=True, exist_ok=True)
    slug = slugify(question)
    out = QA_DIR / f"{slug}.md"
    today = datetime.now().strftime("%Y-%m-%d")
    summary_line = question.strip().rstrip("?").strip()
    title = str(question.strip().rstrip("?")).replace(
        chr(92), chr(92) + chr(92)
    ).replace(chr(34), chr(92) + chr(34)).replace(chr(10), " ").replace(chr(13), " ")
    summary_esc = str(summary_line).replace(
        chr(92), chr(92) + chr(92)
    ).replace(chr(34), chr(92) + chr(34)).replace(chr(10), " ").replace(chr(13), " ")
    page = (
        "---\n"
        f"type: qa\n"
        f'title: "{title}"\n'
        f'description: "Settled answer captured on {today}"\n'
        f"timestamp: {datetime.now().isoformat(timespec='seconds')}\n"
        f"confidence: medium\n"
        f"source_authority: ai-derived\n"
        "---\n\n"
        f"# {question.strip().rstrip('?')}?\n\n"
        f"One-sentence summary: Settled answer to \"{summary_esc}\" captured on {today}.\n\n"
        f"## Question\n"
        f"{question.strip()}\n\n"
        f"## Answer\n"
        f"{answer_text}\n\n"
        f"## Evidence\n"
        f"- Captured by `scripts/query_memory.py --file-back` on {today}.\n\n"
        f"## Related\n"
        f"-\n"
    )
    out.write_text(page, encoding="utf-8")
    return out


def rebuild_index() -> bool:
    """Run the memory index rebuild. Returns True on success.

    Callers should surface a warning if False — the page was written
    correctly, but `knowledge/index.md` is now stale until the next
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

    if answer_text.startswith("("):
        return 1

    if args.file_back:
        out = file_back(args.question, answer_text)
        index_ok = rebuild_index()
        suffix = "" if index_ok else " (WARN: knowledge/index.md rebuild failed — page written, index stale)"
        append_log(
            f"- {datetime.now().strftime('%Y-%m-%d')} — Filed Q&A `{out.relative_to(ROOT).as_posix()}` via `query_memory.py --file-back`.{suffix}"
        )
        print(f"\n[filed] {out.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
