"""User-level SessionStart hook — inject per-project state.md.

This hook fires on every Claude Code session start, regardless of cwd. It
resolves the current project's slug, reads (or creates) the corresponding
`knowledge/projects/<slug>/state.md`, and emits its content as additionalContext
so Claude starts the session knowing where we left off in this project.

Companion to the project-level `session_start_context.py` hook (which
injects general memory context when cwd=vault). Both can fire in the same
session without conflict — Claude Code runs all registered hooks and
concatenates their additionalContext output.

Contract (hard requirements):
    * Must exit 0 on ANY error. Breaking a session is worse than missing
      context. All exceptions are swallowed and logged to
      $LLM_WIKI_STATE_ROOT/hook-errors.log (best-effort).
    * Must no-op if LLM_WIKI_ROOT is unset or its knowledge/projects/ is missing.
    * Output: a single JSON object on stdout with the shape Claude Code
      expects (see schema: hookSpecificOutput.additionalContext).

Slug rule (mirrors `~/.claude/CLAUDE.md` and
[[Global Multi-Project Migration Plan]]):
    1. Base: lowercase basename of CLAUDE_PROJECT_DIR (or cwd) with
       whitespace and unsafe chars replaced by hyphens. Non-ASCII chars
       (e.g. Cyrillic) are preserved — NTFS and Obsidian both handle them.
    2. On collision (another project recorded a different root under the
       same slug): append parent-of-parent (e.g. `backend` + `your-app`
       → `backend-your-app`).
    3. On further collision: parse the origin URL from `.git/config` and
       use `owner-repo`.
    4. On further collision: append the grandparent folder name.
    5. Last resort: try deterministic SHA-256 suffixes of 6/12/24/40/64
       characters, then bounded UUIDv4 candidates while the claim lock is held.
    Ownership accepts only bounded native absolute paths. It prefers the strict
    JSON string in `- Project root JSON:` and falls back to the legacy backtick
    line only when JSON metadata is absent. `- Runtime slug JSON:` persists the
    safe alias independently of a legacy folder name.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

# Force utf-8 on stdout (Windows cp1252 mojibakes Cyrillic otherwise).
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

from memory_state import (  # noqa: E402
    _is_unicode_noncharacter,
    bind_atomic_writes_to_directory,
    parse_frontmatter_scalar,
    read_json_object_bounded,
)

MAX_CONTEXT_CHARS = 2400  # keep the injection compact
LINE_TRUNCATION_MARKER = "... (line truncated)"
CONTEXT_TRUNCATION_MARKER = "... (context truncated)"
MAX_BOOTSTRAP_READ_CHARS = 8192
HOOK_INPUT_MAX_BYTES = 64_000
HOOK_PROJECT_FIELDS = ("cwd", "project_dir")
PROJECT_DIRECTORY_ENV_VARS = (
    "CLAUDE_PROJECT_DIR",
    "CODEX_PROJECT_DIR",
    "OPENCODE_PROJECT_DIR",
)
MAX_PROJECT_SLUG_CHARS = 128
SLUG_SAFE_PUNCTUATION = frozenset(".-")
MAX_PROJECT_STATE_OWNERSHIP_CHARS = 64 * 1024
MAX_PROJECT_ROOT_CHARS = 32 * 1024
MAX_PROJECT_STATE_ENTRIES = 1024
MAX_GIT_CONFIG_BYTES = 64 * 1024

# Collision disambiguation cap — try this many candidate slugs before
# falling back to a path-hash suffix. Four covers: base, base-pop,
# base-owner-repo, base-grandparent. Any beyond that is pathological.
MAX_SLUG_CANDIDATES = 4

# Deterministic hash suffixes expand before bounded random collision retries.
PATH_HASH_SUFFIX_LEN = 6
PATH_HASH_SUFFIX_LENGTHS = (6, 12, 24, 40, 64)
MAX_RANDOM_SLUG_ATTEMPTS = 8

# Ownership metadata used to detect slug collisions. JSON is canonical;
# the backtick form remains a read-only fallback for existing state files.
JSON_STRING_VALUE_PATTERN = (
    r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9A-Fa-f]{4}))*"'
)
STATE_SOURCE_LINE_RE = re.compile(
    r"^- Project root[ \t]*:[ \t]*`([^`\r\n]+)`[ \t]*$", re.MULTILINE
)
STATE_SOURCE_PREFIX_RE = re.compile(
    r"^- Project root[ \t]*:[ \t]*`[^`\r\n]+`[ \t]*$", re.MULTILINE
)
STATE_SOURCE_JSON_DECLARATION_RE = re.compile(
    r"^- Project root JSON[ \t]*:", re.MULTILINE
)
STATE_SOURCE_JSON_PREFIX_RE = re.compile(
    rf"^- Project root JSON[ \t]*:[ \t]*{JSON_STRING_VALUE_PATTERN}[ \t]*$",
    re.MULTILINE,
)
STATE_SOURCE_JSON_LINE_RE = re.compile(
    rf"^- Project root JSON[ \t]*:[ \t]*({JSON_STRING_VALUE_PATTERN})[ \t]*$",
    re.MULTILINE,
)
STATE_RUNTIME_SLUG_JSON_PREFIX_RE = re.compile(
    rf"^- Runtime slug JSON[ \t]*:[ \t]*{JSON_STRING_VALUE_PATTERN}[ \t]*$",
    re.MULTILINE,
)
STATE_RUNTIME_SLUG_JSON_DECLARATION_RE = re.compile(
    r"^- Runtime slug JSON[ \t]*:", re.MULTILINE
)
STATE_RUNTIME_SLUG_JSON_LINE_RE = re.compile(
    rf"^- Runtime slug JSON[ \t]*:[ \t]*({JSON_STRING_VALUE_PATTERN})[ \t]*$",
    re.MULTILINE,
)
STATE_H1_HEADING_RE = re.compile(r"^ {0,3}#(?!#)(?:[ \t]+|$)")
STATE_H2_HEADING_RE = re.compile(r"^ {0,3}##(?!#)(?:[ \t]+|$)")
STATE_IDENTITY_SECTION_TITLES = frozenset(
    {"identity", "metadata", "project identity", "project metadata", "source"}
)
BOOTSTRAP_PROJECT_SLUG_JSON_PREFIX_RE = re.compile(
    r"^project_slug_json\b.*$", re.MULTILINE
)
BOOTSTRAP_PROJECT_SLUG_JSON_LINE_RE = re.compile(
    r"^project_slug_json:\s*(.+?)\s*$", re.MULTILINE
)
BOOTSTRAP_PROJECT_ROOT_JSON_PREFIX_RE = re.compile(
    r"^project_root_json\b.*$", re.MULTILINE
)
BOOTSTRAP_PROJECT_ROOT_JSON_LINE_RE = re.compile(
    r"^project_root_json:\s*(.+?)\s*$", re.MULTILINE
)
BOOTSTRAP_STATE_PATH_JSON_PREFIX_RE = re.compile(
    r"^project_state_path_json\b.*$", re.MULTILINE
)
BOOTSTRAP_STATE_PATH_JSON_LINE_RE = re.compile(
    r"^project_state_path_json:\s*(.+?)\s*$", re.MULTILINE
)
BOOTSTRAP_GIT_HEAD_JSON_PREFIX_RE = re.compile(
    r"^git_head_json\b.*$", re.MULTILINE
)
BOOTSTRAP_GIT_HEAD_JSON_LINE_RE = re.compile(
    r"^git_head_json:\s*(.+?)\s*$", re.MULTILINE
)
GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
SOURCE_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
BOOTSTRAP_SCHEMA_VERSION = 2
BOOTSTRAP_GIT_TIMEOUT_SECONDS = 2.0
MAX_GIT_IDENTITY_OUTPUT_CHARS = 32 * 1024
MAX_GIT_STDERR_BYTES = 32 * 1024
PROCESS_IO_CHUNK_BYTES = 8 * 1024
_GIT_EXECUTABLE_UNSET = object()
_TRUSTED_STATE_UNSET = object()
_GIT_EXECUTABLE_UNSET = object()
MAX_GIT_STDERR_BYTES = 32 * 1024
PROCESS_IO_CHUNK_BYTES = 8 * 1024
BOOTSTRAP_REQUIRED_FRONTMATTER_KEYS = frozenset(
    {
        "type",
        "project_slug_json",
        "project_root_json",
        "project_state_path_json",
        "git_head_json",
        "bootstrap_schema_json",
        "source_fingerprint_json",
    }
)
BOOTSTRAP_ALLOWED_FRONTMATTER_KEYS = BOOTSTRAP_REQUIRED_FRONTMATTER_KEYS | {
    "title",
    "description",
    "timestamp",
}
GIT_REPOSITORY_ROUTING_ENV = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_ATTR_SOURCE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_GRAFT_FILE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_INTERNAL_SUPER_PREFIX",
        "GIT_NAMESPACE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_QUARANTINE_PATH",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_SUPER_PREFIX",
        "GIT_WORK_TREE",
    }
)
STATE_TEMPLATE_PLACEHOLDERS = (
    "<Project Name>",
    "<what this project is, in one sentence>",
    "<absolute path JSON>",
    "<absolute path>",
    "<remote url>",
)
STATE_TEMPLATE_PLACEHOLDER_RE = re.compile(
    "|".join(
        re.escape(placeholder)
        for placeholder in sorted(STATE_TEMPLATE_PLACEHOLDERS, key=len, reverse=True)
    )
)
STATE_SECTION_TEMPLATE_PLACEHOLDERS = {
    "where we left off": (
        '<Most recent context — "we were working on X, stopped at Y, next step is Z". '
        "Keep to ≤ 5 bullets. This is what gets injected at session start, so it "
        "should read like a handoff note to future-you.>"
    ),
    "recent decisions": (
        "<Architectural or scope decisions specific to this project. Include the "
        "*why*. Cross-cutting decisions belong in `knowledge/notes/`, not here.>"
    ),
    "open threads": (
        "<Unresolved questions, pending investigations, TODOs that need context to "
        "understand. Close them when resolved.>"
    ),
    "links": (
        "<Wikilinks to related pages in this vault: concepts used, sibling projects, "
        "raw sources. Wikilinks only — external URLs belong inside the content above "
        "with context.>"
    ),
}
STATE_EDITORIAL_TEMPLATE = (
    "This page is per-project state — **read and auto-created** by the SessionStart "
    "hook when a markered folder is opened; its **content** is edited by Claude or "
    "the user during/after the session (the SessionEnd hook only tags the shared "
    "daily log, it does not write state.md). Content decisions (what to keep, what "
    "to archive) follow [[Global Multi-Project Migration Plan]] conventions. Keep "
    "this page to ≤ 1 screen; move detail into sibling pages under the same project "
    "folder."
)
STATE_PENDING_DESCRIPTION_RE = re.compile(
    r"^One-sentence summary:\s*"
    r"\(new project at .+, pending description\)\.?$"
)
STATE_FENCE_OPEN_RE = re.compile(r"^ {0,3}(?P<run>`{3,}|~{3,})(?P<rest>.*)$")
STATE_RAW_TYPE_1_RE = re.compile(
    r"^ {0,3}<(?P<tag>script|style|pre|textarea)(?=[ \t/>]|$)",
    re.IGNORECASE,
)
STATE_RAW_TYPE_6_RE = re.compile(
    r"^ {0,3}</?(?:address|article|aside|base|basefont|blockquote|body|caption|"
    r"center|col|colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|"
    r"figcaption|figure|footer|form|frame|frameset|h[1-6]|head|header|"
    r"hr|html|iframe|legend|li|link|main|menu|menuitem|nav|noframes|"
    r"ol|optgroup|option|p|param|search|section|summary|table|tbody|td|"
    r"tfoot|th|thead|title|tr|track|ul)(?=[ \t\f\r/>]|$)",
    re.IGNORECASE,
)
STATE_RAW_COMPLETE_TAG_RE = re.compile(
    r"^ {0,3}</?[A-Za-z][A-Za-z0-9-]*(?:[ \t]+[^<>]*)?/?>[ \t]*$"
)
STATE_HANDOFF_PROCESS_STATUS_RE = re.compile(
    r"^(?:[-*]\s*)?.*\bPID\s*[:#=]?\s*(?P<pid>\d+)\b.*\b"
    r"(?:is|was|remains?)\s+(?:still\s+)?(?:listed\s+as\s+)?"
    r"(?:active|alive|running)\.?$",
    re.IGNORECASE,
)
STATE_HANDOFF_BARE_PROCESS_PID_RE = re.compile(
    r"^(?:[-*]\s*)?"
    r"(?:FreeCAD(?:[ \t]+GUI)?|(?:[\w.-]+[ \t]+){1,4}"
    r"(?:process|server|daemon|application|app))"
    r"[ \t]+PID\s*[:#=]?\s*(?P<pid>\d+)\s*[.!]?$",
    re.IGNORECASE,
)
STATE_HANDOFF_STANDALONE_PID_RE = re.compile(
    r"^(?:[-*]\s*)?PID\s*:\s*(?P<pid>\d+)\s*[.!]?$",
    re.IGNORECASE,
)
STATE_HANDOFF_TIME_RE = re.compile(
    r"^(?:[-*]\s*)?(?:last\s+(?:updated|seen|active|activity)|updated\s+at|as\s+of)"
    r"\s*[:=-]\s*(?:\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2})(?:[^\r\n]*)$",
    re.IGNORECASE,
)
MAX_PROCESS_ID = 2_147_483_647
MAX_PROCESS_ID_DIGITS = 10

# Project markers — presence of ANY of these signals "this folder is a real
# project", gating auto-creation of state.md. Without a marker, the hook
# stays read-only: existing state.md is injected, but no new file is written.
# This filters out throwaway folders (casual `cd /tmp`) while remaining
# permissive for actual projects. Convention aligned with Claude Code's own
# /init gate (per 2026 hooks research).
PROJECT_MARKERS = (
    ".claude",        # strongest: project already has Claude Code config
    "CLAUDE.md",      # project-level instructions
    ".git",           # version-controlled project
    "package.json",   # Node/JS
    "pyproject.toml", # Python
    "Cargo.toml",     # Rust
    "go.mod",         # Go
    "pom.xml",        # Java/Maven
    "build.gradle",   # Java/Gradle
    "build.gradle.kts",
    "Gemfile",        # Ruby
    "composer.json",  # PHP
    ".csproj",        # C#
    "mix.exs",        # Elixir
)


@dataclass(frozen=True)
class ProjectStateEntry:
    state_path: Path
    project_root: Path | None
    runtime_slug: str | None


@dataclass(frozen=True)
class ProjectRootResolution:
    root: Path | None
    signal_present: bool


@dataclass(frozen=True)
class ProjectAliasResolution:
    slug: str
    project_root: Path
    state_path: Path


@dataclass(frozen=True)
class RuntimeSlugMetadata:
    present: bool
    value: str | None


@dataclass(frozen=True)
class BoundedProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class ProjectStateContextSnapshot:
    state_path: Path
    slug: str
    project_root: Path
    trusted_state_body: str | None
    trusted_state_parts: tuple[str, str, str] | None
    bootstrap: str


def _resolve_state_root() -> Path | None:
    """Return $LLM_WIKI_STATE_ROOT or the vault root as fallback.

    Mirrors `memory_state.py` convention: if the env var is unset, default
    to the vault itself (runtime dirs cache/logs/run live inside the vault).
    Returns None only if neither env var is available (hook should no-op).
    """
    raw = os.environ.get("LLM_WIKI_STATE_ROOT")
    if raw:
        return Path(raw)
    vault = os.environ.get("LLM_WIKI_ROOT")
    if vault:
        return Path(vault).resolve()
    return None


def _safe_write_error(err: str) -> None:
    """Best-effort error log. Silent on failure."""
    try:
        state_root = _resolve_state_root()
        if state_root is None:
            return
        log_path = state_root / "logs" / "hook-errors.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().isoformat(timespec="seconds")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] session_start_project_state: {err}\n")
    except Exception:  # noqa: BLE001
        pass


def _emit(additional_context: str) -> int:
    """Write the hook's JSON response and return 0."""
    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": additional_context,
        }
    }
    try:
        print(json.dumps(out, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        # Even stdout can fail (broken pipe, encoding); swallow.
        pass
    return 0


def _emit_empty() -> int:
    """No-op exit — emit empty additionalContext and return 0."""
    return _emit("")


def is_canonical_project_slug(slug: str) -> bool:
    """Return whether a slug is safe in paths, Markdown, and compact records."""
    return (
        bool(slug)
        and len(slug) <= MAX_PROJECT_SLUG_CHARS
        and slug[0].isalnum()
        and slug[-1].isalnum()
        and all(char.isalnum() or char in SLUG_SAFE_PUNCTUATION for char in slug)
    )


def _slug_identity_key(slug: str | None) -> str | None:
    """Return the platform-independent casefold key for one safe alias."""
    if not isinstance(slug, str) or not is_canonical_project_slug(slug):
        return None
    return slug.casefold()


def _sanitize(text: str) -> str:
    """Canonicalize to Unicode alphanumerics plus internal dots/hyphens."""
    chars = [
        char if char.isalnum() or char in SLUG_SAFE_PUNCTUATION else "-"
        for char in text.lower()
    ]
    slug = re.sub(r"-+", "-", "".join(chars)).strip(".-")
    if len(slug) > MAX_PROJECT_SLUG_CHARS:
        suffix = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:6]
        prefix = slug[: MAX_PROJECT_SLUG_CHARS - len(suffix) - 1].rstrip(".-")
        slug = f"{prefix}-{suffix}" if prefix else suffix
    return slug if is_canonical_project_slug(slug) else ""


def _join_slug(*parts: str) -> str:
    """Join already meaningful slug parts without exceeding the grammar."""
    return _sanitize("-".join(part for part in parts if part))


def _base_slug(project_dir: Path) -> str:
    """Preferred slug — parent folder name only. Fallback to `root`."""
    return _sanitize(project_dir.name) or "root"


def _git_remote_slug(project_dir: Path) -> str | None:
    """Extract an `owner-repo` slug from `project_dir/.git/config`.

    Returns None if no git dir, no `origin` remote, URL parse fails, or
    the resulting slug sanitizes to empty. This is a LAST-RESORT
    disambiguator — we only look at it after parent-folder attempts
    produce a collision.

    Intentionally does NOT shell out to `git` — avoids dependency on
    git being on PATH in hook context. Reads .git/config directly.
    """
    gitcfg = project_dir / ".git" / "config"
    if not gitcfg.is_file():
        return None
    try:
        with gitcfg.open("rb") as handle:
            raw = handle.read(MAX_GIT_CONFIG_BYTES + 1)
        if len(raw) > MAX_GIT_CONFIG_BYTES:
            return None
        text = raw.decode("utf-8")
    except (OSError, UnicodeError):
        return None
    # Find [remote "origin"] section and its url = ...
    # Format: [remote "origin"]\n\turl = <url>
    m = re.search(
        r'\[remote\s+"origin"\]\s*\n(?:\s+[^\n]+\n)*?\s+url\s*=\s*(\S+)',
        text,
    )
    if not m:
        return None
    url = m.group(1).strip()
    # Extract owner/repo from SSH or HTTPS forms:
    #   git@host:owner/repo(.git)
    #   https://host/owner/repo(.git)
    #   https://host/path/to/owner/repo(.git)
    m2 = re.search(r"[:/]([^:/]+)/([^/]+?)(?:\.git)?/*$", url)
    if not m2:
        return None
    owner = _sanitize(m2.group(1))
    repo = _sanitize(m2.group(2))
    if not owner or not repo:
        return None
    return _join_slug(owner, repo)


def _path_hash_suffix(project_dir: Path, length: int = PATH_HASH_SUFFIX_LEN) -> str:
    """Return a deterministic path-hash prefix for verified allocation."""
    h = hashlib.sha256(str(project_dir.resolve()).encode("utf-8")).hexdigest()
    return h[:length]


def _path_comparison_key(path: Path, platform: str | None = None) -> str:
    """Return a filesystem-appropriate key without erasing POSIX case."""
    if (platform or sys.platform) == "win32":
        return PureWindowsPath(str(path)).as_posix().casefold()
    return PurePosixPath(str(path)).as_posix()


def _same_native_project_root(first: object, second: object) -> bool:
    """Compare two validated absolute project roots using host semantics."""
    if (
        not isinstance(first, str)
        or not isinstance(second, str)
        or not _is_native_absolute_root(first)
        or not _is_native_absolute_root(second)
    ):
        return False
    try:
        return _path_comparison_key(Path(first).resolve()) == _path_comparison_key(
            Path(second).resolve()
        )
    except (OSError, RuntimeError, ValueError):
        return False


def _path_is_within(candidate: Path, root: Path) -> bool:
    """Compare resolved paths using the host filesystem's case semantics."""
    candidate_key = _path_comparison_key(candidate)
    root_key = _path_comparison_key(root).rstrip("/")
    return candidate_key == root_key or candidate_key.startswith(f"{root_key}/")


def _absolute_signal_path(value: object) -> Path | None:
    try:
        raw = str(value or "").strip()
        if not _is_native_absolute_root(raw):
            return None
        return Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def resolve_project_root(
    payload: Mapping[str, object] | None = None,
    *,
    explicit_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    fallback_cwd: str | Path | None = None,
) -> ProjectRootResolution:
    """Resolve trusted project-root and current-directory signals."""
    data = payload if isinstance(payload, Mapping) else {}
    environment = os.environ if env is None else env
    root_values = [
        value
        for value in (
            explicit_root,
            data.get("project_dir"),
            data.get("project_root"),
            *(environment.get(name) for name in PROJECT_DIRECTORY_ENV_VARS),
        )
        if str(value or "").strip()
    ]
    supplied_cwd = data.get("cwd") or data.get("new_cwd")
    # A process fallback is only meaningful when no agent supplied a project
    # root. Hook runners commonly execute from the vault itself.
    cwd_value = supplied_cwd if root_values else supplied_cwd or fallback_cwd
    signal_present = bool(
        root_values or str(cwd_value or "").strip()
    )
    if not signal_present:
        return ProjectRootResolution(None, False)

    roots = [_absolute_signal_path(value) for value in root_values]
    if any(root is None for root in roots):
        return ProjectRootResolution(None, True)
    resolved_roots = [root for root in roots if root is not None]
    if len({_path_comparison_key(root) for root in resolved_roots}) > 1:
        return ProjectRootResolution(None, True)
    root = resolved_roots[0] if resolved_roots else None
    cwd = _absolute_signal_path(cwd_value)
    if root is None:
        return ProjectRootResolution(cwd, True)
    if cwd_value and (cwd is None or not _path_is_within(cwd, root)):
        return ProjectRootResolution(None, True)
    return ProjectRootResolution(root, True)


def _resolve_under(candidate: Path, root: Path) -> Path:
    """Resolve a state path and reject links or traversal outside the root."""
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as error:
        raise ValueError("project state resolves outside knowledge/projects") from error
    return resolved_candidate


def _is_native_absolute_root(root: str, platform: str | None = None) -> bool:
    """Validate ownership lexically before resolution can consult process CWD."""
    if (
        not root
        or len(root) > MAX_PROJECT_ROOT_CHARS
        or any(
            ord(char) < 32
            or 127 <= ord(char) <= 159
            or ord(char) in {0x2028, 0x2029}
            or 0xD800 <= ord(char) <= 0xDFFF
            or _is_unicode_noncharacter(ord(char))
            for char in root
        )
    ):
        return False
    path_type = PureWindowsPath if (platform or sys.platform) == "win32" else PurePosixPath
    return path_type(root).is_absolute()


def _state_visible_lines(body: str, *, hide_fences: bool = True) -> list[str]:
    """Return same-length lines with non-visible Markdown constructs hidden."""
    lines = body.splitlines()
    visible = list(lines)
    start = 0
    if lines and lines[0].strip() == "---":
        closing = next(
            (index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"),
            None,
        )
        if closing is None:
            return [""] * len(lines)
        for index in range(closing + 1):
            visible[index] = ""
        start = closing + 1

    fence: tuple[str, int] | None = None
    in_comment = False
    raw_closer: str | None = None
    blank_terminated_raw = False
    for index in range(start, len(lines)):
        line = lines[index]
        if fence is not None:
            marker, width = fence
            closing_fence = re.fullmatch(
                rf" {{0,3}}{re.escape(marker)}{{{width},}}[ \t]*",
                line,
            )
            if closing_fence:
                fence = None
            visible[index] = "" if hide_fences or closing_fence else line
            continue
        if raw_closer is not None:
            if raw_closer.casefold() in line.casefold():
                raw_closer = None
            visible[index] = ""
            continue
        if blank_terminated_raw:
            if not line.strip(" \t"):
                blank_terminated_raw = False
            visible[index] = ""
            continue

        pieces: list[str] = []
        cursor = 0
        while cursor < len(line):
            if in_comment:
                comment_end = line.find("-->", cursor)
                if comment_end < 0:
                    cursor = len(line)
                    break
                in_comment = False
                cursor = comment_end + 3
                continue
            comment_start = line.find("<!--", cursor)
            if comment_start < 0:
                pieces.append(line[cursor:])
                break
            pieces.append(line[cursor:comment_start])
            in_comment = True
            cursor = comment_start + 4
        structural = "".join(pieces)
        opening = STATE_FENCE_OPEN_RE.fullmatch(structural)
        if opening is not None:
            run = opening.group("run")
            if not (run.startswith("`") and "`" in opening.group("rest")):
                fence = (run[0], len(run))
                structural = ""
        if structural:
            type_1 = STATE_RAW_TYPE_1_RE.match(structural)
            if type_1 is not None:
                closer = f"</{type_1.group('tag')}>"
                if closer.casefold() not in structural[type_1.end() :].casefold():
                    raw_closer = closer
                structural = ""
            else:
                stripped = structural.lstrip(" ")
                indent = len(structural) - len(stripped)
                if indent <= 3 and stripped.startswith("<?"):
                    if "?>" not in stripped[2:]:
                        raw_closer = "?>"
                    structural = ""
                elif indent <= 3 and stripped.startswith("<![CDATA["):
                    if "]]>" not in stripped[len("<![CDATA[") :]:
                        raw_closer = "]]>"
                    structural = ""
                elif indent <= 3 and re.match(r"<![A-Z]", stripped):
                    if ">" not in stripped[2:]:
                        raw_closer = ">"
                    structural = ""
                elif (
                    STATE_RAW_TYPE_6_RE.match(structural)
                    or STATE_RAW_COMPLETE_TAG_RE.fullmatch(structural)
                ):
                    blank_terminated_raw = True
                    structural = ""
        visible[index] = structural
    return visible


def _recorded_project_root(body: str) -> str | None:
    """Return strict JSON ownership, or legacy ownership only when absent."""
    body = "\n".join(_state_visible_lines(body))
    json_metadata = STATE_SOURCE_JSON_PREFIX_RE.findall(body)
    if json_metadata:
        matches = STATE_SOURCE_JSON_LINE_RE.findall(body)
        if len(json_metadata) != 1 or len(matches) != 1:
            return None
        try:
            recorded = json.loads(matches[0])
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(recorded, str) or not _is_native_absolute_root(recorded):
            return None
        return recorded
    if STATE_SOURCE_JSON_DECLARATION_RE.search(body):
        return None

    legacy_metadata = STATE_SOURCE_PREFIX_RE.findall(body)
    legacy_matches = STATE_SOURCE_LINE_RE.findall(body)
    if len(legacy_metadata) > 1:
        return None
    legacy = (
        legacy_matches[0].strip()
        if len(legacy_metadata) == len(legacy_matches) == 1
        else None
    )
    if legacy is not None and not _is_native_absolute_root(legacy):
        return None
    return legacy if len(legacy_metadata) == len(legacy_matches) == 1 else None


def _runtime_slug_metadata(body: str) -> RuntimeSlugMetadata:
    """Distinguish absent runtime metadata from a present-invalid claim."""
    body = "\n".join(_state_visible_lines(body))
    metadata = STATE_RUNTIME_SLUG_JSON_PREFIX_RE.findall(body)
    if not metadata:
        malformed_declaration = STATE_RUNTIME_SLUG_JSON_DECLARATION_RE.search(body)
        return RuntimeSlugMetadata(malformed_declaration is not None, None)
    matches = STATE_RUNTIME_SLUG_JSON_LINE_RE.findall(body)
    if len(metadata) != 1 or len(matches) != 1:
        return RuntimeSlugMetadata(True, None)
    try:
        slug = json.loads(matches[0])
    except (json.JSONDecodeError, TypeError):
        return RuntimeSlugMetadata(True, None)
    value = slug if isinstance(slug, str) and is_canonical_project_slug(slug) else None
    return RuntimeSlugMetadata(True, value)


def _recorded_runtime_slug(body: str) -> str | None:
    """Return one strict canonical runtime alias, or None when absent/invalid."""
    return _runtime_slug_metadata(body).value


def _render_runtime_slug_metadata(body: str, slug: str) -> str:
    """Set machine alias metadata without changing handoff or other state text."""
    metadata_line = f"- Runtime slug JSON: {json.dumps(slug, ensure_ascii=False)}"
    lines = body.splitlines(keepends=True)
    metadata_indexes = [
        index
        for index, line in enumerate(_state_visible_lines(body))
        if STATE_RUNTIME_SLUG_JSON_PREFIX_RE.fullmatch(line)
    ]
    if metadata_indexes:
        first = metadata_indexes[0]
        ending = lines[first][len(lines[first].rstrip("\r\n")) :]
        lines[first] = metadata_line + ending
        for index in reversed(metadata_indexes[1:]):
            del lines[index]
        return "".join(lines)

    owner_index = next(
        (
            index
            for index, line in enumerate(_state_visible_lines(body))
            if STATE_SOURCE_JSON_PREFIX_RE.fullmatch(line)
        ),
        None,
    )
    if owner_index is None:
        owner_index = next(
            (
                index
                for index, line in enumerate(_state_visible_lines(body))
                if STATE_SOURCE_LINE_RE.fullmatch(line)
            ),
            None,
        )
    if owner_index is None:
        return body
    owner_line = lines[owner_index]
    owner_content = owner_line.rstrip("\r\n")
    ending = owner_line[len(owner_content) :]
    if ending:
        lines.insert(owner_index + 1, metadata_line + ending)
    else:
        lines[owner_index] = owner_line + "\n"
        lines.insert(owner_index + 1, metadata_line)
    return "".join(lines)


def _is_reparse_point(metadata) -> bool:
    return bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _open_windows_state_descriptor(path: Path) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    path_text = os.path.abspath(path)
    if not path_text.startswith("\\\\?\\"):
        path_text = (
            "\\\\?\\UNC\\" + path_text[2:]
            if path_text.startswith("\\\\")
            else "\\\\?\\" + path_text
        )
    handle = create_file(
        path_text,
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in {None, invalid}:
        error = ctypes.get_last_error()
        raise OSError(error, f"CreateFileW failed with Windows error {error}")
    try:
        return msvcrt.open_osfhandle(
            int(handle),
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def _open_state_descriptor(state_path: Path, directory_bound) -> int:
    if os.name == "nt":
        return _open_windows_state_descriptor(state_path)
    if os.name != "posix" or directory_bound.descriptor is None:
        raise OSError("identity-bound no-follow state reads are unsupported")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    return os.open(state_path.name, flags, dir_fd=directory_bound.descriptor)


def _state_path_metadata(state_path: Path, directory_bound):
    if os.name == "posix" and directory_bound.descriptor is not None:
        return os.stat(
            state_path.name,
            dir_fd=directory_bound.descriptor,
            follow_symlinks=False,
        )
    return state_path.lstat()


def _state_metadata_is_regular(metadata) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and not _is_reparse_point(metadata)
    )


def _read_state_ownership_body(
    state_path: Path,
    *,
    _directory_bound=None,
    _expected_metadata=None,
) -> str | None:
    """Read state through a no-follow descriptor bound to its lexical parent."""
    state_path = Path(os.path.abspath(state_path))
    if _directory_bound is None:
        try:
            with bind_atomic_writes_to_directory(state_path.parent) as bound:
                return _read_state_ownership_body(
                    state_path,
                    _directory_bound=bound,
                    _expected_metadata=_expected_metadata,
                )
        except (OSError, RuntimeError, UnicodeError, ValueError):
            return None

    descriptor: int | None = None
    handle = None
    try:
        _directory_bound.validate_path()
        metadata = _state_path_metadata(state_path, _directory_bound)
        if (
            not _state_metadata_is_regular(metadata)
            or _expected_metadata is not None
            and not os.path.samestat(_expected_metadata, metadata)
        ):
            return None
        descriptor = _open_state_descriptor(state_path, _directory_bound)
        opened = os.fstat(descriptor)
        if not _state_metadata_is_regular(opened) or not os.path.samestat(
            metadata,
            opened,
        ):
            return None
        handle = os.fdopen(
            descriptor,
            "r",
            encoding="utf-8",
            errors="strict",
            closefd=True,
        )
        descriptor = None
        body = handle.read(MAX_PROJECT_STATE_OWNERSHIP_CHARS + 1)
        opened_after = os.fstat(handle.fileno())
        _directory_bound.validate_path()
        current = _state_path_metadata(state_path, _directory_bound)
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return None
    finally:
        if handle is not None:
            handle.close()
        elif descriptor is not None:
            os.close(descriptor)
    if len(body) > MAX_PROJECT_STATE_OWNERSHIP_CHARS:
        return None
    if (
        not _state_metadata_is_regular(opened_after)
        or not _state_metadata_is_regular(current)
        or not os.path.samestat(opened, opened_after)
        or not os.path.samestat(opened_after, current)
        or metadata.st_size != opened.st_size
        or opened.st_size != opened_after.st_size
        or opened_after.st_size != current.st_size
        or getattr(metadata, "st_mtime_ns", None)
        != getattr(opened_after, "st_mtime_ns", None)
        or getattr(opened_after, "st_mtime_ns", None)
        != getattr(current, "st_mtime_ns", None)
        or stat.S_IMODE(metadata.st_mode) != stat.S_IMODE(opened_after.st_mode)
        or stat.S_IMODE(opened_after.st_mode) != stat.S_IMODE(current.st_mode)
        or getattr(metadata, "st_file_attributes", 0)
        != getattr(opened_after, "st_file_attributes", 0)
        or getattr(opened_after, "st_file_attributes", 0)
        != getattr(current, "st_file_attributes", 0)
        or min(opened.st_nlink, opened_after.st_nlink, current.st_nlink) < 1
    ):
        return None
    return body


def _state_path_owns_project(
    state_path: Path,
    project_dir: Path,
    *,
    require_json: bool = False,
) -> bool:
    body = _read_state_ownership_body(state_path)
    if body is None or (require_json and not STATE_SOURCE_JSON_PREFIX_RE.search(body)):
        return False
    recorded = _recorded_project_root(body)
    if recorded is None:
        return False
    try:
        recorded_norm = _path_comparison_key(Path(recorded).resolve())
        current_norm = _path_comparison_key(project_dir.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return recorded_norm == current_norm


def _scan_project_states(projects_dir: Path) -> list[ProjectStateEntry]:
    """Return every contained state, or raise when inventory is incomplete."""
    try:
        projects_dir = Path(os.path.abspath(projects_dir))
        entries: list[ProjectStateEntry] = []
        seen: set[Path] = set()
        scanned = 0
        with bind_atomic_writes_to_directory(projects_dir) as projects_bound:
            projects_bound.validate_path()
            for directory in projects_dir.iterdir():
                scanned += 1
                if scanned > MAX_PROJECT_STATE_ENTRIES:
                    raise RuntimeError("project state inventory entry limit exceeded")
                if directory.name == "_template":
                    continue
                projects_bound.validate_path()
                try:
                    lexical = directory.lstat()
                except FileNotFoundError:
                    continue
                lexical_identity = (
                    lexical.st_dev,
                    lexical.st_ino,
                    stat.S_IFMT(lexical.st_mode),
                )
                if stat.S_ISLNK(lexical.st_mode) or _is_reparse_point(lexical):
                    raise OSError("project state directory is a link or reparse point")
                if not stat.S_ISDIR(lexical.st_mode):
                    continue
                with bind_atomic_writes_to_directory(directory) as directory_bound:
                    if directory_bound.identity != lexical_identity:
                        raise OSError("project state directory changed while binding")
                    projects_bound.validate_path()
                    state_path = directory / "state.md"
                    if state_path in seen:
                        continue
                    try:
                        state_metadata = _state_path_metadata(
                            state_path,
                            directory_bound,
                        )
                    except FileNotFoundError:
                        continue
                    if not _state_metadata_is_regular(state_metadata):
                        raise OSError(
                            "project state inventory entry is not a regular file"
                        )
                    body = _read_state_ownership_body(
                        state_path,
                        _directory_bound=directory_bound,
                        _expected_metadata=state_metadata,
                    )
                    if body is None:
                        raise OSError(
                            f"project state inventory unreadable: {state_path}"
                        )
                    recorded = _recorded_project_root(body)
                    recorded_root = (
                        Path(recorded).resolve() if recorded is not None else None
                    )
                    runtime_metadata = _runtime_slug_metadata(body)
                    if runtime_metadata.present and runtime_metadata.value is None:
                        raise RuntimeError(
                            f"project state runtime alias is invalid: {state_path}"
                        )
                    seen.add(state_path)
                    entries.append(
                        ProjectStateEntry(
                            state_path=state_path,
                            project_root=recorded_root,
                            runtime_slug=runtime_metadata.value,
                        )
                    )
            projects_bound.validate_path()
        runtime_claims: dict[str, Path] = {}
        for entry in entries:
            key = _slug_identity_key(entry.runtime_slug)
            if key is None:
                continue
            if key in runtime_claims:
                raise RuntimeError("duplicate casefold project alias")
            runtime_claims[key] = entry.state_path
        return entries
    except (OSError, RuntimeError, ValueError) as error:
        raise RuntimeError("project state inventory is incomplete") from error


def _reserved_runtime_slugs(
    entries: list[ProjectStateEntry],
    current_project: Path | None = None,
) -> set[str]:
    current_key = (
        _path_comparison_key(current_project.resolve())
        if current_project is not None
        else None
    )
    reserved: set[str] = set()
    for entry in entries:
        if (
            current_key is not None
            and entry.project_root is not None
            and _path_comparison_key(entry.project_root) == current_key
        ):
            continue
        runtime_key = _slug_identity_key(entry.runtime_slug)
        if runtime_key:
            reserved.add(runtime_key)
        else:
            folder = entry.state_path.parent.name
            folder_alias = folder if is_canonical_project_slug(folder) else _sanitize(folder)
            folder_key = _slug_identity_key(folder_alias)
            if folder_key:
                reserved.add(folder_key)
    return reserved


def _slug_owns_dir(slug: str, project_dir: Path, projects_dir: Path) -> bool:
    """True ONLY if `projects_dir/slug/state.md` either doesn't exist or
    explicitly records `project_dir` as its Project root.

    Strict (hardened per colleague review): a state.md that exists but lacks
    parseable JSON or legacy project-root metadata is treated as NOT ours.
    Previously we assumed
    "probably hand-edited — treat as ours", but that allowed a collision
    where deleting the Source section let a second project silently adopt
    the first's state.md. Now we force disambiguation (the caller tries
    the next candidate slug) and the worst case is a deterministic
    verified hash or UUID slug — safer than cross-contamination.

    Safe read-side behavior: on read (no auto-create fired), a state.md
    missing its Source line will still be read correctly — we just won't
    WRITE over it. The read path in main() doesn't consult this function.
    """
    try:
        state_path = _resolve_under(projects_dir / slug / "state.md", projects_dir)
    except (OSError, RuntimeError, ValueError):
        return False
    if not state_path.exists():
        return True  # unused slug — free to take
    return _state_path_owns_project(state_path, project_dir)


def _candidate_slugs(project_dir: Path) -> Iterator[str]:
    """Yield candidates lazily so remote config is a true fallback."""
    base = _base_slug(project_dir)
    seen = {_slug_identity_key(base)}
    predictable = 1
    yield base

    # parent-of-parent: e.g. <your-projects-dir>/your-app\backend → backend-your-app
    parent_of_parent = project_dir.parent.name if project_dir.parent else ""
    pop = _sanitize(parent_of_parent)
    if pop and pop != base:
        candidate = _join_slug(base, pop)
        key = _slug_identity_key(candidate)
        if key not in seen and predictable < MAX_SLUG_CANDIDATES:
            seen.add(key)
            predictable += 1
            yield candidate

    # git owner-repo is read only after the earlier yielded candidates collide.
    if predictable < MAX_SLUG_CANDIDATES:
        remote = _git_remote_slug(project_dir)
        remote_key = _slug_identity_key(remote)
        if remote and remote_key not in seen:
            seen.add(remote_key)
            predictable += 1
            yield remote

    # grandparent-of-parent as extra fallback before hash
    grand = project_dir.parent.parent.name if project_dir.parent and project_dir.parent.parent else ""
    gp = _sanitize(grand)
    if gp and gp != base and gp != pop and predictable < MAX_SLUG_CANDIDATES:
        candidate = _join_slug(base, gp)
        key = _slug_identity_key(candidate)
        if key not in seen:
            seen.add(key)
            yield candidate

    for length in PATH_HASH_SUFFIX_LENGTHS:
        fallback = _join_slug(base, _path_hash_suffix(project_dir, length))
        key = _slug_identity_key(fallback)
        if key not in seen:
            seen.add(key)
            yield fallback


def _allocate_slug(
    project_dir: Path,
    projects_dir: Path,
    reserved: set[str],
) -> str:
    def available(candidate: str) -> bool:
        candidate_key = _slug_identity_key(candidate)
        return (
            candidate_key is not None
            and candidate_key not in reserved
            and _slug_owns_dir(candidate, project_dir, projects_dir)
        )

    for candidate in _candidate_slugs(project_dir):
        if available(candidate):
            return candidate

    base = _base_slug(project_dir)
    for _attempt in range(MAX_RANDOM_SLUG_ATTEMPTS):
        candidate = _join_slug(base, uuid4().hex)
        if available(candidate):
            return candidate
    raise RuntimeError("unable to allocate a free project slug")


def _compute_slug(project_dir: Path, projects_dir: Path) -> str:
    """Compute the slug for a project, resolving collisions.

    Strategy (documented in `knowledge/notes/Global Multi-Project Migration
    Plan.md`):
      1. Parent folder name, sanitized.
      2. On collision: parent + parent-of-parent (e.g. `backend-your-app`).
      3. On further collision: git `owner-repo` from origin remote.
      4. On further collision: expanding path-hash suffixes, then UUIDv4.

    Returns the first candidate that either doesn't exist or already
    belongs to `project_dir` (same recorded Project root).
    """
    entries = _scan_project_states(projects_dir)
    reserved = _reserved_runtime_slugs(entries, project_dir)
    return _allocate_slug(project_dir, projects_dir, reserved)


def _runtime_slug_for_owned_state(
    project_dir: Path,
    owned_entry: ProjectStateEntry,
    projects_dir: Path,
    entries: list[ProjectStateEntry],
) -> str:
    """Choose safe metadata identity independently of a legacy folder name."""
    reserved = _reserved_runtime_slugs(entries, project_dir)

    def available(candidate: str) -> bool:
        candidate_key = _slug_identity_key(candidate)
        return (
            candidate_key is not None
            and candidate_key not in reserved
            and _slug_owns_dir(candidate, project_dir, projects_dir)
        )

    if owned_entry.runtime_slug:
        normalized = _sanitize(owned_entry.runtime_slug)
        normalized_key = _slug_identity_key(normalized)
        if normalized_key is not None and normalized_key not in reserved:
            return normalized

    existing_slug = owned_entry.state_path.parent.name
    if is_canonical_project_slug(existing_slug):
        normalized = _sanitize(existing_slug)
        if normalized and available(normalized):
            return normalized

    return _allocate_slug(project_dir, projects_dir, reserved)


def resolve_project_state(
    project_dir: Path,
    projects_dir: Path,
    *,
    _entries: list[ProjectStateEntry] | None = None,
) -> tuple[str, Path]:
    """Return a safe runtime slug and the exact owned or allocatable state path."""
    resolved = project_dir.resolve()
    entries = _scan_project_states(projects_dir) if _entries is None else _entries
    current_key = _path_comparison_key(resolved)
    owned = sorted(
        (
            entry
            for entry in entries
            if entry.project_root is not None
            and _path_comparison_key(entry.project_root) == current_key
        ),
        key=lambda entry: entry.state_path.as_posix().casefold(),
    )
    if len(owned) > 1:
        raise RuntimeError("multiple project states claim the same root")
    if owned:
        owned_entry = owned[0]
        slug = _runtime_slug_for_owned_state(
            resolved,
            owned_entry,
            projects_dir,
            entries,
        )
        return slug, owned_entry.state_path
    reserved = _reserved_runtime_slugs(entries, resolved)
    slug = _allocate_slug(resolved, projects_dir, reserved)
    state_path = _resolve_under(projects_dir / slug / "state.md", projects_dir)
    return slug, state_path


def resolve_project_alias(
    slug: str,
    projects_dir: Path,
) -> ProjectAliasResolution | None:
    """Resolve one unique persisted alias without synthesizing a state path."""
    requested_key = _slug_identity_key(slug)
    if requested_key is None:
        return None

    from memory_state import advisory_file_lock

    try:
        state_root = _resolve_state_root() or projects_dir.parent.parent
        claim_lock = state_root / "run" / "project-state-claim.lock"
        with advisory_file_lock(
            claim_lock,
            timeout=5.0,
            description="project state claim",
        ):
            matches: list[tuple[str, ProjectStateEntry]] = []
            for entry in _scan_project_states(projects_dir):
                entry_slug = (
                    _sanitize(entry.runtime_slug)
                    if entry.runtime_slug is not None
                    else ""
                )
                if _slug_identity_key(entry_slug) == requested_key:
                    matches.append((entry_slug, entry))
            if len(matches) != 1 or matches[0][1].project_root is None:
                return None
            resolved_slug, entry = matches[0]
            if _read_trusted_state_body(
                entry.state_path,
                resolved_slug,
                entry.project_root,
            ) is None:
                return None
            return ProjectAliasResolution(
                slug=resolved_slug,
                project_root=entry.project_root,
                state_path=entry.state_path,
            )
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return None


def _read_hook_payload(stream) -> dict | None:
    """Read bounded hook JSON without blocking an interactive invocation."""
    try:
        if stream.isatty():
            return {}
    except (AttributeError, OSError, io.UnsupportedOperation):
        return None
    return read_json_object_bounded(stream, max_bytes=HOOK_INPUT_MAX_BYTES)


def _resolve_project_dir(
    payload: Mapping[str, object],
    env: Mapping[str, str] = os.environ,
) -> Path | None:
    """Resolve project root without promoting a nested hook cwd."""
    return resolve_project_root(
        payload,
        env=env,
        fallback_cwd=os.getcwd(),
    ).root


def _has_project_marker(project_dir: Path) -> bool:
    """True if the directory contains at least one project marker.

    Markers like `.git/`, `CLAUDE.md`, `package.json` are evidence that this
    folder is a durable project worth tracking, not a scratch directory.
    Without a marker, we skip auto-creation — but still read an existing
    state.md if the user previously opted in manually.

    Suffix-based markers (e.g. `.csproj`) use glob matching.

    **Excludes $HOME itself.** The user's home directory typically contains
    `.claude/` (user-level Claude Code config, a PROJECT_MARKER from our
    list) and sometimes `.git/` (dotfiles repo). Launching Claude Code from
    $HOME would otherwise auto-create a nonsense `user/` slug in the vault.
    """
    try:
        # Guard against $HOME false-positive: ~/.claude/ is user-level, not
        # project-level. Compare resolved paths to handle symlinks/casing.
        home = Path.home().resolve()
        if project_dir.resolve() == home:
            return False
    except (OSError, RuntimeError):
        # Path.home() can raise RuntimeError on truly exotic environments.
        # Fall through to marker detection; worst case is one nonsense slug.
        pass
    try:
        for marker in PROJECT_MARKERS:
            if marker.startswith("."):
                # Check both exact file/dir and glob (for patterns like .csproj)
                if (project_dir / marker).exists():
                    return True
                if any(project_dir.glob(f"*{marker}")):
                    return True
            else:
                if (project_dir / marker).exists():
                    return True
    except OSError:
        return False
    return False


def _render_new_state(state_template: Path, slug: str, project_dir: Path) -> str:
    """Return template content with placeholders filled for a new project."""
    tmpl = state_template.read_text(encoding="utf-8")
    root_json = json.dumps(str(project_dir), ensure_ascii=False)
    replacements = {
        "<Project Name>": slug,
        "<what this project is, in one sentence>": (
            f"(new project at `{project_dir}`, pending description)"
        ),
        "<absolute path JSON>": root_json,
        "<absolute path>": str(project_dir),
        "<remote url>": "(unknown — set manually if applicable)",
    }
    filled = STATE_TEMPLATE_PLACEHOLDER_RE.sub(
        lambda match: replacements[match.group(0)],
        tmpl,
    )
    visible_lines = _state_visible_lines(filled)
    if not STATE_SOURCE_JSON_PREFIX_RE.search("\n".join(visible_lines)):
        json_line = f"- Project root JSON: {root_json}\n"
        legacy_index = next(
            (
                index
                for index, visible in enumerate(visible_lines)
                if STATE_SOURCE_LINE_RE.fullmatch(visible)
            ),
            None,
        )
        if legacy_index is not None:
            lines = filled.splitlines(keepends=True)
            lines.insert(legacy_index, json_line)
            filled = "".join(lines)
        else:
            filled = filled.rstrip() + "\n\n## Source\n" + json_line
    if STATE_SOURCE_JSON_PREFIX_RE.search("\n".join(_state_visible_lines(filled))):
        lines = filled.splitlines(keepends=True)
        filled = "".join(
            line
            for line, visible in zip(lines, _state_visible_lines(filled), strict=False)
            if not STATE_SOURCE_PREFIX_RE.fullmatch(visible)
        )
    return _render_runtime_slug_metadata(filled, slug)


def _rendered_state_claim_is_valid(body: str, slug: str, project_dir: Path) -> bool:
    """Require exact canonical ownership and alias metadata before publication."""
    visible = "\n".join(_state_visible_lines(body))
    if len(STATE_SOURCE_JSON_PREFIX_RE.findall(visible)) != 1:
        return False
    recorded_root = _recorded_project_root(body)
    runtime_metadata = _runtime_slug_metadata(body)
    if recorded_root is None or not runtime_metadata.present:
        return False
    if runtime_metadata.value is None:
        return False
    try:
        root_matches = _path_comparison_key(Path(recorded_root).resolve()) == (
            _path_comparison_key(project_dir.resolve())
        )
    except (OSError, RuntimeError, ValueError):
        return False
    return root_matches and _slug_identity_key(runtime_metadata.value) == (
        _slug_identity_key(slug)
    )


def confirm_project_identity(
    project_dir: Path,
    projects_dir: Path,
) -> tuple[str, Path, bool] | None:
    """Return a proven project state, claiming one atomically when allowed.

    Existing state is usable only when its recorded root matches. For a marked
    project with a usable template, allocation is serialized under a runtime
    lock and a complete new state is published atomically.
    """
    from memory_state import advisory_file_lock, atomic_write

    try:
        resolved_project = project_dir.resolve()
        state_root = _resolve_state_root() or projects_dir.parent.parent
        claim_lock = state_root / "run" / "project-state-claim.lock"
        with advisory_file_lock(
            claim_lock,
            timeout=5.0,
            description="project state claim",
        ):
            entries = _scan_project_states(projects_dir)
            runtime_slug, owned_state_path = resolve_project_state(
                resolved_project,
                projects_dir,
                _entries=entries,
            )
            if owned_state_path.exists() and _state_path_owns_project(
                owned_state_path,
                resolved_project,
            ):
                body = _read_state_ownership_body(owned_state_path)
                if body is None:
                    return None
                updated = _render_runtime_slug_metadata(body, runtime_slug)
                if updated != body:
                    atomic_write(owned_state_path, updated)
                return runtime_slug, owned_state_path, False

            template = projects_dir / "_template" / "state.md"
            if not _has_project_marker(resolved_project) or not template.is_file():
                return None
            state_path = _resolve_under(
                projects_dir / runtime_slug / "state.md",
                projects_dir,
            )
            if state_path.exists():
                return None
            content = _render_new_state(template, runtime_slug, resolved_project)
            if not _rendered_state_claim_is_valid(
                content,
                runtime_slug,
                resolved_project,
            ):
                return None
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path = _resolve_under(state_path, projects_dir)
            atomic_write(state_path, content)
            return runtime_slug, state_path, True
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return None


def _project_git_marker_status(project_root: Path) -> bool | None:
    """Return True for a safe .git entry, False when absent, None when unsafe."""
    marker = project_root / ".git"
    try:
        metadata = marker.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    attributes = getattr(metadata, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode))
    ):
        return None
    return True


def _absolute_path_entries(environment: Mapping[str, str]) -> tuple[str, ...]:
    raw_path = environment.get("PATH", os.defpath)
    entries: list[str] = []
    for raw_entry in raw_path.split(os.pathsep):
        if not raw_entry:
            continue
        try:
            entry = Path(raw_entry)
            if entry.is_absolute():
                entries.append(str(entry))
        except (OSError, RuntimeError, ValueError):
            continue
    return tuple(entries)


def _path_is_regular_non_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and not attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _resolve_git_executable(
    environment: Mapping[str, str] | None = None,
) -> Path | None:
    """Resolve Git without consulting relative PATH entries or project cwd."""
    source = os.environ if environment is None else environment
    if os.name == "nt":
        raw_extensions = source.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        extensions = tuple(
            extension.casefold()
            for extension in raw_extensions.split(os.pathsep)
            if extension.startswith(".")
            and "/" not in extension
            and "\\" not in extension
        )
        names = tuple(f"git{extension}" for extension in extensions)
    else:
        names = ("git",)

    for raw_directory in _absolute_path_entries(source):
        for name in names:
            candidate = Path(raw_directory) / name
            if not _path_is_regular_non_reparse(candidate):
                continue
            if os.name != "nt" and not os.access(candidate, os.X_OK):
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                continue
            if resolved.is_absolute() and _path_is_regular_non_reparse(resolved):
                return resolved
    return None


def _run_bounded_process(
    command: list[str | Path],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
) -> BoundedProcessResult | None:
    """Run a child while draining both pipes without unbounded buffering."""
    if (
        not command
        or timeout <= 0
        or max_stdout_bytes < 0
        or max_stderr_bytes < 0
    ):
        return None
    run_kwargs: dict[str, object] = {}
    if os.name == "nt":
        run_kwargs["creationflags"] = getattr(
            subprocess,
            "CREATE_NO_WINDOW",
            0x08000000,
        )
    try:
        process = subprocess.Popen(
            [str(part) for part in command],
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            **run_kwargs,
        )
    except (OSError, ValueError):
        return None
    if process.stdout is None or process.stderr is None:
        try:
            process.kill()
        except OSError:
            pass
        return None

    stdout = bytearray()
    stderr = bytearray()
    failed = threading.Event()

    def terminate() -> None:
        if failed.is_set():
            return
        failed.set()
        try:
            process.kill()
        except OSError:
            pass

    def drain(stream, destination: bytearray, limit: int) -> None:
        try:
            while not failed.is_set():
                chunk = stream.read(
                    min(PROCESS_IO_CHUNK_BYTES, max(1, limit - len(destination) + 1))
                )
                if not chunk:
                    return
                remaining = limit - len(destination)
                if len(chunk) > remaining:
                    if remaining > 0:
                        destination.extend(chunk[:remaining])
                    terminate()
                    return
                destination.extend(chunk)
        except (OSError, ValueError):
            terminate()
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    threads = (
        threading.Thread(
            target=drain,
            args=(process.stdout, stdout, max_stdout_bytes),
            name="llm-wiki-process-stdout",
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=(process.stderr, stderr, max_stderr_bytes),
            name="llm-wiki-process-stderr",
            daemon=True,
        ),
    )
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + timeout
    try:
        returncode = process.wait(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        terminate()
        returncode = -1
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            terminate()
    if failed.is_set():
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return None
    return BoundedProcessResult(returncode, bytes(stdout), bytes(stderr))


def _git_subprocess_environment() -> dict[str, str]:
    """Preserve executable context while removing ambient repository routing."""
    environment = dict(os.environ)
    for name in tuple(environment):
        canonical = name.upper()
        if (
            canonical in GIT_REPOSITORY_ROUTING_ENV
            or canonical.startswith("GIT_CONFIG_KEY_")
            or canonical.startswith("GIT_CONFIG_VALUE_")
        ):
            del environment[name]
    environment["PATH"] = os.pathsep.join(_absolute_path_entries(environment))
    environment["NoDefaultCurrentDirectoryInExePath"] = "1"
    return environment


def _run_git_text(
    project_root: Path,
    *args: str,
    git_executable: Path | None | object = _GIT_EXECUTABLE_UNSET,
) -> tuple[int, str] | None:
    executable = (
        _resolve_git_executable()
        if git_executable is _GIT_EXECUTABLE_UNSET
        else git_executable
    )
    if not isinstance(executable, Path):
        return None
    try:
        result = _run_bounded_process(
            [executable, *args],
            cwd=project_root,
            env=_git_subprocess_environment(),
            timeout=BOOTSTRAP_GIT_TIMEOUT_SECONDS,
            max_stdout_bytes=MAX_GIT_IDENTITY_OUTPUT_CHARS,
            max_stderr_bytes=MAX_GIT_STDERR_BYTES,
        )
        if result is None:
            return None
        stdout = result.stdout.decode("utf-8", errors="strict")
    except (UnicodeError, ValueError):
        return None
    return result.returncode, stdout


def _run_git_identity(
    project_root: Path,
    *args: str,
    git_executable: Path | None | object = _GIT_EXECUTABLE_UNSET,
) -> str | None:
    result = _run_git_text(
        project_root,
        *args,
        git_executable=git_executable,
    )
    if result is None or result[0] != 0:
        return None
    lines = result[1].strip().splitlines()
    return lines[0] if len(lines) == 1 and lines[0] else None


def _current_project_git_head(
    project_root: Path,
    git_executable: Path | None | object = _GIT_EXECUTABLE_UNSET,
) -> tuple[bool, str | None]:
    """Return a verified exact-repository HEAD or explicit non-Git status."""
    try:
        resolved_root = project_root.resolve()
    except (OSError, RuntimeError, ValueError):
        return False, None
    if not _is_native_absolute_root(str(resolved_root)):
        return False, None
    marker_status = _project_git_marker_status(resolved_root)
    if marker_status is False:
        if any(
            _project_git_marker_status(parent) is not False
            for parent in resolved_root.parents
        ):
            return False, None
        return True, None
    if marker_status is None:
        return False, None
    executable = (
        _resolve_git_executable()
        if git_executable is _GIT_EXECUTABLE_UNSET
        else git_executable
    )
    if not isinstance(executable, Path):
        return False, None
    top_level = _run_git_identity(
        resolved_root,
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
        git_executable=executable,
    )
    if top_level is None or not _same_native_project_root(
        top_level,
        str(resolved_root),
    ):
        return False, None
    head_result = _run_git_text(
        resolved_root,
        "rev-parse",
        "--verify",
        "--quiet",
        "--end-of-options",
        "HEAD^{commit}",
        git_executable=executable,
    )
    if head_result is None:
        return False, None
    returncode, head_output = head_result
    if returncode == 1 and not head_output.strip():
        return True, None
    lines = head_output.strip().splitlines()
    if (
        returncode != 0
        or len(lines) != 1
        or GIT_OBJECT_ID_RE.fullmatch(lines[0]) is None
    ):
        return False, None
    head = lines[0]
    return True, head


def _bootstrap_project_state(
    vault: Path,
    project_dir: Path,
    state_path: Path,
    slug: str | None = None,
    *,
    bootstrap_context: str | object = _TRUSTED_STATE_UNSET,
) -> None:
    """Best-effort detached bootstrap, retried until output exists."""
    try:
        bootstrap_path = state_path.parent / "bootstrap.md"
        if bootstrap_context is not _TRUSTED_STATE_UNSET:
            if bootstrap_context:
                return
        elif (
            slug is not None
            and bootstrap_path.exists()
            and _read_bootstrap_context(state_path, slug, project_dir)
        ):
            return
        import memory_state

        memory_state.spawn_detached(
            [
                sys.executable,
                str(vault / "scripts" / "bootstrap_project.py"),
                "--cwd",
                str(project_dir),
                "--apply",
            ],
            cwd=vault,
        )
    except Exception:
        pass  # never block session start on bootstrap failure


def _strict_bootstrap_frontmatter(
    body: str,
) -> tuple[dict[str, object], int] | None:
    """Parse the writer's bounded flat YAML mapping and return its body offset."""
    if not body.startswith("---\n"):
        return None
    closing = body.find("\n---\n", 4)
    if closing < 0:
        return None
    frontmatter = body[: closing + 5]
    lines = body[4:closing].splitlines()
    raw_values: dict[str, str] = {}
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        match = re.fullmatch(
            r"(?P<key>[a-z][a-z0-9_]*):(?:[ \t]+(?P<value>.*)|[ \t]*)",
            line,
        )
        if match is None:
            return None
        key = match.group("key")
        if key not in BOOTSTRAP_ALLOWED_FRONTMATTER_KEYS or key in raw_values:
            return None
        raw_values[key] = match.group("value") or ""
    if not BOOTSTRAP_REQUIRED_FRONTMATTER_KEYS.issubset(raw_values):
        return None

    parsed: dict[str, object] = {}
    for key, raw_value in raw_values.items():
        if key.endswith("_json"):
            try:
                value = json.loads(raw_value)
            except (json.JSONDecodeError, TypeError):
                return None
            if key == "git_head_json":
                if value is not None and not isinstance(value, str):
                    return None
            elif key == "bootstrap_schema_json":
                if isinstance(value, bool) or not isinstance(value, int):
                    return None
            elif not isinstance(value, str):
                return None
            parsed[key] = value
            continue
        scalar = parse_frontmatter_scalar(
            frontmatter,
            key,
            max_chars=MAX_BOOTSTRAP_READ_CHARS,
        )
        if not scalar.present or scalar.value is None:
            return None
        parsed[key] = scalar.value
    return parsed, closing + 5


def _bootstrap_provenance_matches(
    body: str,
    state_path: Path,
    expected_slug: str,
    expected_project_root: Path,
    *,
    trusted_state_body: str | None | object = _TRUSTED_STATE_UNSET,
) -> bool:
    """Require current bootstrap provenance to match the confirmed state."""
    trusted_state = (
        _read_trusted_state_body(
            state_path,
            expected_slug,
            expected_project_root,
        )
        if trusted_state_body is _TRUSTED_STATE_UNSET
        else trusted_state_body
    )
    if not isinstance(trusted_state, str) or not _trusted_state_body_matches_identity(
        trusted_state,
        expected_slug,
        expected_project_root,
    ):
        return False
    frontmatter = _strict_bootstrap_frontmatter(body)
    if frontmatter is None:
        return False
    values, _body_offset = frontmatter
    recorded_slug = values["project_slug_json"]
    recorded_project_root = values["project_root_json"]
    recorded_state_path = values["project_state_path_json"]
    recorded_fingerprint = values["source_fingerprint_json"]
    if (
        values["type"] != "bootstrap-context"
        or values["bootstrap_schema_json"] != BOOTSTRAP_SCHEMA_VERSION
        or not isinstance(recorded_slug, str)
        or recorded_slug != expected_slug
        or not isinstance(recorded_project_root, str)
        or not isinstance(recorded_state_path, str)
        or not _is_native_absolute_root(recorded_project_root)
        or not _is_native_absolute_root(recorded_state_path)
        or not isinstance(recorded_fingerprint, str)
        or SOURCE_FINGERPRINT_RE.fullmatch(recorded_fingerprint) is None
    ):
        return False

    recorded_git_head = values["git_head_json"]
    if recorded_git_head is not None and (
        not isinstance(recorded_git_head, str)
        or GIT_OBJECT_ID_RE.fullmatch(recorded_git_head) is None
    ):
        return False
    try:
        provenance_matches = recorded_project_root == str(
            expected_project_root.resolve()
        ) and recorded_state_path == str(state_path.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    if not provenance_matches:
        return False
    git_executable = (
        _resolve_git_executable()
        if _project_git_marker_status(expected_project_root) is True
        else None
    )
    git_status, current_git_head = _current_project_git_head(
        expected_project_root,
        git_executable=git_executable,
    )
    if not git_status or current_git_head != recorded_git_head:
        return False
    try:
        from bootstrap_project import _bootstrap_source_fingerprint

        current_fingerprint = _bootstrap_source_fingerprint(
            expected_project_root,
            current_git_head,
            git_executable=git_executable,
        )
    except (ImportError, OSError, RuntimeError, ValueError):
        return False
    return current_fingerprint == recorded_fingerprint


def _read_bootstrap_context(
    state_path: Path,
    slug: str | None = None,
    project_root: Path | None = None,
    *,
    trusted_state_body: str | None | object = _TRUSTED_STATE_UNSET,
) -> str:
    """Read a bounded sibling bootstrap file without following path escapes."""
    if slug is None or project_root is None:
        return ""
    try:
        candidate = state_path.with_name("bootstrap.md")
        if not _state_file_is_regular(candidate):
            return ""
        bootstrap_path = _resolve_under(
            candidate,
            state_path.parent,
        )
        with bootstrap_path.open(
            "r",
            encoding="utf-8",
            errors="strict",
        ) as handle:
            text = handle.read(MAX_BOOTSTRAP_READ_CHARS + 1)
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return ""

    truncated = len(text) > MAX_BOOTSTRAP_READ_CHARS
    text = text[:MAX_BOOTSTRAP_READ_CHARS]
    frontmatter = _strict_bootstrap_frontmatter(text)
    if truncated or frontmatter is None or not _bootstrap_provenance_matches(
        text,
        state_path,
        slug,
        project_root,
        trusted_state_body=trusted_state_body,
    ):
        return ""
    text = text[frontmatter[1]:]

    lines = text.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    if lines and lines[0].startswith(
        "One-sentence summary: Auto-generated project context for "
    ):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    excerpt = "\n".join(lines).strip()
    return excerpt


def _state_h2_title(line: str) -> str | None:
    if not STATE_H2_HEADING_RE.match(line):
        return None
    title = line.lstrip(" ")[2:].strip()
    title = re.sub(r"[ \t]+#+[ \t]*$", "", title)
    return title.casefold()


def _split_state_identity(body: str) -> tuple[str, str]:
    """Lift the state heading and strongest ownership line ahead of detail."""
    lines = body.splitlines()
    heading_index = next(
        (index for index, line in enumerate(lines) if STATE_H1_HEADING_RE.match(line)),
        None,
    )
    json_root_index = next(
        (
            index
            for index, line in enumerate(lines)
            if STATE_SOURCE_JSON_PREFIX_RE.fullmatch(line)
        ),
        None,
    )
    legacy_root_index = next(
        (
            index
            for index, line in enumerate(lines)
            if STATE_SOURCE_LINE_RE.fullmatch(line)
        ),
        None,
    )
    ownership_index = (
        json_root_index if json_root_index is not None else legacy_root_index
    )
    identity_indexes = {
        index for index in (heading_index, ownership_index) if index is not None
    }
    heading_indexes = [
        index for index, line in enumerate(lines) if _state_h2_title(line) is not None
    ]
    for position, section_start in enumerate(heading_indexes):
        title = _state_h2_title(lines[section_start])
        if title not in STATE_IDENTITY_SECTION_TITLES:
            break
        section_end = (
            heading_indexes[position + 1]
            if position + 1 < len(heading_indexes)
            else len(lines)
        )
        identity_indexes.update(range(section_start, section_end))
    identity = "\n".join(lines[index] for index in sorted(identity_indexes))
    remainder = "\n".join(
        line for index, line in enumerate(lines) if index not in identity_indexes
    ).strip()
    return identity, remainder


def _split_state_handoff(body: str) -> tuple[str, str]:
    """Lift the current handoff section ahead of lower-priority state detail."""
    lines = body.splitlines()
    start = None
    for index, line in enumerate(lines):
        title = _state_h2_title(line)
        if title is None:
            continue
        if title in STATE_IDENTITY_SECTION_TITLES:
            continue
        if title == "where we left off":
            start = index
        break
    if start is None:
        return "", body.strip()
    end = next(
        (
            index
            for index, line in enumerate(lines[start + 1 :], start + 1)
            if STATE_H2_HEADING_RE.match(line)
        ),
        len(lines),
    )
    handoff = "\n".join(lines[start:end]).strip()
    remainder = "\n".join([*lines[:start], *lines[end:]]).strip()
    return handoff, remainder


def _state_file_is_regular(state_path: Path) -> bool:
    try:
        metadata = state_path.lstat()
    except OSError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and not bool(
            attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
    )


def _state_metadata_text(body: str) -> str:
    """Return metadata-visible lines, excluding frontmatter, comments, and fences."""
    return "\n".join(line for line in _state_visible_lines(body) if line.strip())


def _trusted_state_body_matches_identity(
    body: str,
    slug: str,
    project_root: Path,
) -> bool:
    """Validate cached state ownership without another filesystem read."""
    if (
        not isinstance(body, str)
        or _slug_identity_key(slug) is None
        or not _is_native_absolute_root(str(project_root))
    ):
        return False
    metadata_text = _state_metadata_text(body)
    json_metadata = STATE_SOURCE_JSON_PREFIX_RE.findall(metadata_text)
    json_matches = STATE_SOURCE_JSON_LINE_RE.findall(metadata_text)
    runtime_metadata = _runtime_slug_metadata(metadata_text)
    if (
        len(json_metadata) != 1
        or len(json_matches) != 1
        or not runtime_metadata.present
        or runtime_metadata.value is None
        or _slug_identity_key(runtime_metadata.value) != _slug_identity_key(slug)
    ):
        return False
    recorded_root = _recorded_project_root(metadata_text)
    return recorded_root is not None and _same_native_project_root(
        recorded_root,
        str(project_root),
    )


def _read_trusted_state_body(
    state_path: Path,
    slug: str,
    project_root: Path,
) -> str | None:
    """Read state only when canonical ownership matches the confirmed request."""
    if (
        _slug_identity_key(slug) is None
        or not _is_native_absolute_root(str(project_root))
        or not _state_file_is_regular(state_path)
    ):
        return None
    body = _read_state_ownership_body(state_path)
    return (
        body
        if body is not None
        and _trusted_state_body_matches_identity(body, slug, project_root)
        else None
    )


def _state_lines_without_frontmatter(body: str) -> list[str]:
    lines = body.splitlines()
    if not lines or lines[0].strip() != "---":
        return lines
    closing = next(
        (index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"),
        None,
    )
    return lines[closing + 1 :] if closing is not None else lines


def _trim_state_lines(lines: list[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _bounded_process_id(raw_pid: str | int) -> int | None:
    if isinstance(raw_pid, bool):
        return None
    if isinstance(raw_pid, int):
        return raw_pid if 1 <= raw_pid <= MAX_PROCESS_ID else None
    if (
        not isinstance(raw_pid, str)
        or not raw_pid
        or len(raw_pid) > MAX_PROCESS_ID_DIGITS
        or not raw_pid.isascii()
        or not raw_pid.isdigit()
    ):
        return None
    try:
        pid = int(raw_pid)
    except (ValueError, OverflowError):
        return None
    return pid if 1 <= pid <= MAX_PROCESS_ID else None


def _process_id_is_alive(raw_pid: str | int) -> bool:
    pid = _bounded_process_id(raw_pid)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (ValueError, OverflowError, OSError):
        return False
    return True


def _state_line_is_dead_process_metadata(line: str) -> bool:
    for pattern in (
        STATE_HANDOFF_PROCESS_STATUS_RE,
        STATE_HANDOFF_BARE_PROCESS_PID_RE,
    ):
        match = pattern.fullmatch(line)
        if match is not None:
            return not _process_id_is_alive(match.group("pid"))
    return False


def _state_section_lines(
    title: str,
    lines: list[str],
    project_root: Path,
) -> list[str]:
    """Remove only exact template forms and non-handoff runtime metadata."""
    output: list[str] = []
    fence: tuple[str, int] | None = None
    expected_placeholder = STATE_SECTION_TEMPLATE_PLACEHOLDERS.get(title)
    for line in lines:
        if fence is not None:
            output.append(line)
            marker, width = fence
            if re.fullmatch(
                rf" {{0,3}}{re.escape(marker)}{{{width},}}[ \t]*",
                line,
            ):
                fence = None
            continue
        opening = STATE_FENCE_OPEN_RE.fullmatch(line)
        if opening is not None:
            run = opening.group("run")
            if not (run.startswith("`") and "`" in opening.group("rest")):
                fence = (run[0], len(run))
            output.append(line)
            continue

        stripped = line.strip()
        if expected_placeholder is not None and stripped == expected_placeholder:
            continue
        if title == "" and STATE_PENDING_DESCRIPTION_RE.fullmatch(stripped):
            continue
        if title == "where we left off" and (
            _state_line_is_dead_process_metadata(stripped)
            or STATE_HANDOFF_TIME_RE.fullmatch(stripped)
        ):
            continue
        if (
            STATE_SOURCE_JSON_PREFIX_RE.fullmatch(stripped)
            or STATE_SOURCE_PREFIX_RE.fullmatch(stripped)
            or STATE_RUNTIME_SLUG_JSON_PREFIX_RE.fullmatch(stripped)
        ):
            continue
        output.append(line)
    cleaned = _trim_state_lines(output)
    if title == "where we left off" and len(cleaned) == 1:
        standalone_pid = STATE_HANDOFF_STANDALONE_PID_RE.fullmatch(cleaned[0].strip())
        if standalone_pid is not None and not _process_id_is_alive(
            standalone_pid.group("pid")
        ):
            return []
    return cleaned


def _state_lines_are_useful(lines: list[str]) -> bool:
    visible = _state_visible_lines("\n".join(lines), hide_fences=False)
    return any(any(char.isalnum() for char in line) for line in visible)


def _trusted_state_parts(
    body: str,
    slug: str,
    project_root: Path,
) -> tuple[str, str, str]:
    lines = body.splitlines()
    visible_lines = _state_visible_lines(body)
    if lines and lines[0].strip() == "---":
        closing = next(
            (index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"),
            None,
        )
        start = len(lines) if closing is None else closing + 1
        lines = lines[start:]
        visible_lines = visible_lines[start:]
    heading_index = next(
        (
            index
            for index, line in enumerate(visible_lines)
            if STATE_H1_HEADING_RE.match(line)
        ),
        None,
    )
    heading = visible_lines[heading_index].strip() if heading_index is not None else ""
    identity_lines = [
        f"- Project root JSON: {json.dumps(str(project_root.resolve()), ensure_ascii=False)}",
        f"- Runtime slug JSON: {json.dumps(slug, ensure_ascii=False)}",
    ]
    section_starts = [
        index
        for index, line in enumerate(visible_lines)
        if _state_h2_title(line) is not None
    ]
    preamble_end = section_starts[0] if section_starts else len(lines)
    preamble = [
        line
        for index, line in enumerate(lines[:preamble_end])
        if index != heading_index
    ]
    detail_chunks: list[str] = []
    cleaned_preamble = _state_section_lines("", preamble, project_root)
    if _state_lines_are_useful(cleaned_preamble):
        detail_chunks.append("\n".join(cleaned_preamble))

    handoff_sections: list[str] = []
    for position, start in enumerate(section_starts):
        end = section_starts[position + 1] if position + 1 < len(section_starts) else len(lines)
        title = _state_h2_title(visible_lines[start])
        if title is None:
            continue
        cleaned = _state_section_lines(title, lines[start + 1 : end], project_root)
        if title in STATE_IDENTITY_SECTION_TITLES:
            if title != "source" and _state_lines_are_useful(cleaned):
                detail_chunks.append(
                    "\n".join([visible_lines[start].strip(), *cleaned])
                )
            continue
        template_editorial = (
            title == "editorial note" and cleaned == [STATE_EDITORIAL_TEMPLATE]
        )
        if template_editorial or not _state_lines_are_useful(cleaned):
            continue
        rendered = "\n".join([visible_lines[start].strip(), *cleaned])
        if title == "where we left off":
            handoff_sections.append(rendered)
        else:
            detail_chunks.append(rendered)

    handoff = handoff_sections[0] if len(handoff_sections) == 1 else ""
    if heading and (handoff or detail_chunks):
        detail_chunks.insert(0, heading)
    return (
        "\n".join(identity_lines).strip(),
        handoff,
        "\n\n".join(detail_chunks).strip(),
    )


def _read_trusted_state_parts(
    state_path: Path,
    slug: str,
    project_root: Path,
) -> tuple[str, str, str] | None:
    body = _read_trusted_state_body(state_path, slug, project_root)
    return _trusted_state_parts(body, slug, project_root) if body is not None else None


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = f"\n{CONTEXT_TRUNCATION_MARKER}\n"
    if limit <= len(marker):
        return CONTEXT_TRUNCATION_MARKER[:max(0, limit)]
    prefix_limit = limit - len(marker)
    prefix = text[:prefix_limit]
    if (
        prefix
        and prefix_limit < len(text)
        and not prefix.endswith(("\n", "\r"))
        and text[prefix_limit] not in "\n\r"
        and prefix_limit > len(LINE_TRUNCATION_MARKER)
    ):
        prefix = (
            text[: prefix_limit - len(LINE_TRUNCATION_MARKER)].rstrip()
            + LINE_TRUNCATION_MARKER
        )
    return prefix.rstrip() + marker


def _state_context_source(state_path: Path) -> str:
    resolved = state_path.resolve()
    projects_dir = state_path.parent.parent.resolve()
    contained = _resolve_under(resolved, projects_dir)
    if projects_dir.name == "projects" and projects_dir.parent.name == "knowledge":
        return contained.relative_to(projects_dir.parent.parent).as_posix()
    return contained.relative_to(projects_dir.parent).as_posix()


def _load_project_context_snapshot(
    state_path: Path,
    slug: str,
    project_root: Path,
) -> ProjectStateContextSnapshot:
    trusted_body = _read_trusted_state_body(state_path, slug, project_root)
    trusted_parts = (
        _trusted_state_parts(trusted_body, slug, project_root)
        if trusted_body is not None
        else None
    )
    bootstrap = _read_bootstrap_context(
        state_path,
        slug,
        project_root,
        trusted_state_body=trusted_body,
    )
    return ProjectStateContextSnapshot(
        state_path=state_path,
        slug=slug,
        project_root=project_root,
        trusted_state_body=trusted_body,
        trusted_state_parts=trusted_parts,
        bootstrap=bootstrap,
    )


def _build_context(
    state_path: Path,
    slug: str,
    is_new: bool,
    project_root: Path | None = None,
    *,
    snapshot: ProjectStateContextSnapshot | None = None,
) -> str:
    """Build the additionalContext payload around the state.md content."""
    header = (
        f"# Per-project state — `{slug}`\n"
        f"\n"
        f"(Auto-injected from `{_state_context_source(state_path)}`"
        + (" — freshly created for this project." if is_new else ".")
        + ")\n\n"
    )
    if project_root is None:
        current_snapshot = None
    elif (
        snapshot is not None
        and snapshot.state_path == state_path
        and snapshot.slug == slug
        and snapshot.project_root == project_root
    ):
        current_snapshot = snapshot
    else:
        current_snapshot = _load_project_context_snapshot(
            state_path,
            slug,
            project_root,
        )
    trusted = (
        current_snapshot.trusted_state_parts
        if current_snapshot is not None
        else None
    )
    if trusted is None:
        return _clip(header + "(saved project handoff unavailable)\n", MAX_CONTEXT_CHARS)

    identity, handoff, detail = trusted
    bootstrap = current_snapshot.bootstrap
    mandatory = "\n\n".join((header.rstrip(), identity.strip()))
    secondary: list[str] = []
    if handoff:
        secondary.append(handoff)
    if bootstrap:
        secondary.append(
            "## Project bootstrap (UNTRUSTED project-derived data)\n\n"
            + bootstrap
        )
    if detail:
        secondary.append("## Saved project state\n\n" + detail)
    if not secondary:
        return mandatory + "\n" if len(mandatory) + 1 <= MAX_CONTEXT_CHARS else ""
    secondary_text = "\n\n".join(secondary)
    secondary_floor = (
        secondary_text
        if len(secondary_text) <= len(CONTEXT_TRUNCATION_MARKER)
        else CONTEXT_TRUNCATION_MARKER
    )
    if len(f"{mandatory}\n\n{secondary_floor}\n") > MAX_CONTEXT_CHARS:
        return ""
    secondary_budget = MAX_CONTEXT_CHARS - len(mandatory) - 3
    bounded_secondary = _clip(secondary_text, secondary_budget).rstrip()
    return (
        f"{mandatory}\n\n{bounded_secondary}\n"
        if bounded_secondary
        else mandatory + "\n"
    )


def main() -> int:
    try:
        payload = _read_hook_payload(sys.stdin)
        if payload is None:
            return _emit_empty()

        # 1. Locate the vault. If not configured, silently skip.
        vault_root = os.environ.get("LLM_WIKI_ROOT")
        if not vault_root:
            return _emit_empty()
        vault = Path(vault_root)
        projects_dir = vault / "knowledge" / "projects"
        if not projects_dir.is_dir():
            _safe_write_error(
                f"projects dir missing: {projects_dir}"
            )
            return _emit_empty()

        # 2. Resolve current project and prove or atomically claim ownership.
        project_dir = _resolve_project_dir(payload)
        if project_dir is None:
            return _emit_empty()
        claimed = confirm_project_identity(project_dir, projects_dir)
        if claimed is None:
            return _emit_empty()
        slug, state_path, is_new = claimed

        # 3. Validate state/bootstrap once, then reuse it for launch and rendering.
        snapshot = _load_project_context_snapshot(state_path, slug, project_dir)
        _bootstrap_project_state(
            vault,
            project_dir,
            state_path,
            slug,
            bootstrap_context=snapshot.bootstrap,
        )

        # 4. Build and emit context.
        return _emit(
            _build_context(
                state_path,
                slug,
                is_new,
                project_dir,
                snapshot=snapshot,
            )
        )

    except Exception:  # noqa: BLE001 — hook MUST exit 0
        _safe_write_error("unhandled:\n" + traceback.format_exc())
        return _emit_empty()


if __name__ == "__main__":
    raise SystemExit(main())
