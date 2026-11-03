"""The shared prompt and response grammar for session classification."""
from __future__ import annotations

# Tier sentinels — replace the legacy single FLUSH_OK. The classifier
# is asked to emit exactly one of these as the FIRST line of its
# response, followed (for MAJOR/MINOR) by the structured summary.
TIERS = ("FLUSH_MAJOR", "FLUSH_MINOR", "FLUSH_OK")

# Legacy sentinel still recognized for backward compat with any
# pre-Phase-0.5 summaries that may already be in flight or persisted.
LEGACY_SENTINELS = ("FLUSH_OK", "(no durable content)", "NO_DURABLE_CONTENT")


_NON_FLUSH_PREFIXES = ("(summary failed", '{"operations"', '{"audit"')


def _tier_token(first_line_raw: str) -> str:
    """The first line without the decoration a model likes to add."""
    token = first_line_raw.upper().rstrip(".")
    while token.startswith("`") and token.endswith("`") and len(token) > 1:
        token = token[1:-1]
    return token


def _legacy_ok(stripped: str) -> bool:
    """The old protocol: a bare FLUSH_OK, alone or on a line of its own.

    Both comparisons read the same upper-cased set. The per-line one used to test an
    upper-cased line against the mixed-case sentinels, so `(no durable content)` on a
    line of its own never matched. See
    `docs/research/2026-09-17-the-six-capture-corrections-the-first-round-left.md`.
    """
    sentinels = {sentinel.upper() for sentinel in LEGACY_SENTINELS}
    if stripped.strip(" .\n\t*`").upper() in sentinels:
        return True
    return any(line.strip().upper() in sentinels for line in stripped.splitlines())


def _untiered_response(stripped: str) -> tuple[str, str]:
    """No tier line: the old sentinel, a non-flush payload, or content to keep."""
    if _legacy_ok(stripped) or stripped.startswith(_NON_FLUSH_PREFIXES):
        return "ok", ""
    return "minor", stripped


def _classify_response(raw: str) -> tuple[str, str]:
    """Split the LLM response into (tier, body).

    Accepts the new protocol (first line is FLUSH_MAJOR / FLUSH_MINOR /
    FLUSH_OK) and the legacy protocol (single FLUSH_OK token anywhere).

    Returns:
        ("major" | "minor" | "ok", remaining_text)

    A recognized first-line tier retains the stripped following body, even
    for OK; empty or legacy OK responses have no body. An unrecognised
    answer becomes MINOR on purpose:
    better to keep content that may be useful than to lose it as OK.
    """
    stripped = (raw or "").strip()
    if not stripped:
        return "ok", ""
    first_line_raw = stripped.splitlines()[0].strip()
    token = _tier_token(first_line_raw)
    if token in TIERS:
        body = stripped[len(first_line_raw) :].strip(" \n\t*`")
        return token.lower().replace("flush_", ""), body
    return _untiered_response(stripped)


CLASSIFICATION_SYSTEM_PROMPT = (
    "You classify and distill Claude Code transcripts into a 3-tier "
    "memory scale. Your default bias is toward FLUSH_OK — most "
    "sessions are status chatter and should not pollute the daily "
    "log. You only emit FLUSH_MAJOR when you can point to a concrete "
    "decision or lesson in the transcript. No preamble, no apologies."
)


def build_classification_prompt(transcript_excerpt: str, event: str) -> str:
    """The one prompt that decides a session's tier.

    Extracted so the measurement stand in `benchmark/run_flush_classification.py`
    scores the prompt the product actually sends, not a copy of it.
    """
    return f"""You are classifying + distilling a Claude Code session transcript.

Event: {event}

=== STEP 1 — CLASSIFY ===
First, decide the tier of this session by scanning the transcript:

- FLUSH_MAJOR  — contains at least one of: a concrete DECISION with
  rationale, a reusable LESSON/pattern, or a non-obvious COMMAND/snippet
  worth remembering across sessions.

- FLUSH_MINOR  — contains only one or more of: a debug GOTCHA
  (symptom→cause), an OPEN QUESTION worth returning to, or a single
  useful observation — but no decisions or lessons. Worth saving but
  not worth auto-compiling.

- FLUSH_OK     — pure status/progress chatter ("we did X", "started Y",
  "fixed Z" without explanation). Nothing a future session would benefit
  from. Empty transcripts and pure-navigation turn here too.

Be strict. Status updates are FLUSH_OK even if they mention real work —
the bar is "would a future session in this project benefit from knowing
this?". When in doubt, choose the lower tier.

=== STEP 2 — DISTILL (skip for FLUSH_OK) ===
For FLUSH_MAJOR and FLUSH_MINOR, produce a Markdown block with ONLY
these sections that apply (skip empty sections):

- **Decisions made** — concrete choices with reasons (MAJOR only).
- **Lessons / patterns** — reusable insights (MAJOR only).
- **Commands / snippets** — non-obvious invocations (any tier).
- **Gotchas / debugging** — symptom → cause → fix (any tier).
- **Open questions** — unresolved, worth returning to (any tier).

Be terse. Each bullet should fit on one line. Do NOT narrate what was
done — that is status, not memory. Keep names exactly as the transcript
spells them — identifiers, file paths, versions, flags, numbers, commands:
a later search finds the bullet by those, and a bullet that renames or
drops them is lost.

=== OUTPUT FORMAT ===
Emit EXACTLY one of these tokens as the FIRST line of your response,
followed by a blank line, then (if MAJOR/MINOR) the distilled block:

For MAJOR:
FLUSH_MAJOR

<distilled markdown block>

For MINOR:
FLUSH_MINOR

<distilled markdown block>

For OK (no second line allowed):
FLUSH_OK

Do not add preamble, apologies, or trailing explanation. The first
non-blank line MUST be the tier token.

--- BEGIN TRANSCRIPT EXCERPT ---
{transcript_excerpt}
--- END TRANSCRIPT EXCERPT ---
"""
