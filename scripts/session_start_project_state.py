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
import sys
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

from memory_state import read_json_object_bounded  # noqa: E402

MAX_CONTEXT_CHARS = 2400  # keep the injection compact
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
STATE_SOURCE_LINE_RE = re.compile(
    r"^- Project root:\s*`([^`]+)`\s*$", re.MULTILINE
)
STATE_SOURCE_PREFIX_RE = re.compile(
    r"^- Project root(?! JSON)\b.*$", re.MULTILINE
)
STATE_SOURCE_JSON_PREFIX_RE = re.compile(
    r"^- Project root JSON\b.*$", re.MULTILINE
)
STATE_SOURCE_JSON_LINE_RE = re.compile(
    r"^- Project root JSON:\s*(.+?)\s*$", re.MULTILINE
)
STATE_RUNTIME_SLUG_JSON_PREFIX_RE = re.compile(
    r"^- Runtime slug JSON\b.*$", re.MULTILINE
)
STATE_RUNTIME_SLUG_JSON_LINE_RE = re.compile(
    r"^- Runtime slug JSON:\s*(.+?)\s*$", re.MULTILINE
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
    key = path.as_posix()
    return key.casefold() if (platform or sys.platform) == "win32" else key


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
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in root)
    ):
        return False
    path_type = PureWindowsPath if (platform or sys.platform) == "win32" else PurePosixPath
    return path_type(root).is_absolute()


def _recorded_project_root(body: str) -> str | None:
    """Return strict JSON ownership, or legacy ownership only when absent."""
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
        if legacy is not None:
            try:
                if _path_comparison_key(Path(legacy).resolve()) != _path_comparison_key(
                    Path(recorded).resolve()
                ):
                    return None
            except (OSError, RuntimeError, ValueError):
                return None
        return recorded

    return legacy if len(legacy_metadata) == len(legacy_matches) == 1 else None


def _runtime_slug_metadata(body: str) -> RuntimeSlugMetadata:
    """Distinguish absent runtime metadata from a present-invalid claim."""
    metadata = STATE_RUNTIME_SLUG_JSON_PREFIX_RE.findall(body)
    if not metadata:
        return RuntimeSlugMetadata(False, None)
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
        for index, line in enumerate(lines)
        if STATE_RUNTIME_SLUG_JSON_PREFIX_RE.fullmatch(line.rstrip("\r\n"))
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
            for index, line in enumerate(lines)
            if STATE_SOURCE_JSON_PREFIX_RE.fullmatch(line.rstrip("\r\n"))
        ),
        None,
    )
    if owner_index is None:
        owner_index = next(
            (
                index
                for index, line in enumerate(lines)
                if STATE_SOURCE_LINE_RE.fullmatch(line.rstrip("\r\n"))
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


def _read_state_ownership_body(state_path: Path) -> str | None:
    """Read enough state text to prove ownership without unbounded allocation."""
    try:
        with state_path.open(
            "r",
            encoding="utf-8",
            errors="strict",
        ) as handle:
            body = handle.read(MAX_PROJECT_STATE_OWNERSHIP_CHARS + 1)
    except (OSError, UnicodeError):
        return None
    if len(body) > MAX_PROJECT_STATE_OWNERSHIP_CHARS:
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
        directories = projects_dir.iterdir()
        entries: list[ProjectStateEntry] = []
        seen: set[Path] = set()
        scanned = 0
        for directory in directories:
            scanned += 1
            if scanned > MAX_PROJECT_STATE_ENTRIES:
                raise RuntimeError("project state inventory entry limit exceeded")
            if directory.name == "_template":
                continue
            try:
                directory_mode = directory.stat().st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(directory_mode):
                continue
            state_path = _resolve_under(directory / "state.md", projects_dir)
            if state_path in seen:
                continue
            try:
                state_mode = state_path.stat().st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(state_mode):
                continue
            body = _read_state_ownership_body(state_path)
            if body is None:
                raise OSError(f"project state inventory unreadable: {state_path}")
            recorded = _recorded_project_root(body)
            recorded_root = Path(recorded).resolve() if recorded is not None else None
            runtime_metadata = _runtime_slug_metadata(body)
            if runtime_metadata.present and runtime_metadata.value is None:
                raise RuntimeError(f"project state runtime alias is invalid: {state_path}")
            seen.add(state_path)
            entries.append(
                ProjectStateEntry(
                    state_path=state_path,
                    project_root=recorded_root,
                    runtime_slug=runtime_metadata.value,
                )
            )
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
                if entry.runtime_slug is not None:
                    entry_slug = _sanitize(entry.runtime_slug)
                else:
                    folder = entry.state_path.parent.name
                    entry_slug = _sanitize(folder) if is_canonical_project_slug(folder) else ""
                if _slug_identity_key(entry_slug) == requested_key:
                    matches.append((entry_slug, entry))
            if len(matches) != 1 or matches[0][1].project_root is None:
                return None
            resolved_slug, entry = matches[0]
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
    if not STATE_SOURCE_JSON_PREFIX_RE.search(filled):
        json_line = f"- Project root JSON: {root_json}\n"
        legacy = STATE_SOURCE_LINE_RE.search(filled)
        if legacy:
            filled = filled[:legacy.start()] + json_line + filled[legacy.start():]
        else:
            filled = filled.rstrip() + "\n\n## Source\n" + json_line
    return _render_runtime_slug_metadata(filled, slug)


def _rendered_state_claim_is_valid(body: str, slug: str, project_dir: Path) -> bool:
    """Require exact canonical ownership and alias metadata before publication."""
    if len(STATE_SOURCE_JSON_PREFIX_RE.findall(body)) != 1:
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


def _bootstrap_project_state(vault: Path, project_dir: Path, state_path: Path) -> None:
    """Best-effort detached bootstrap, retried until output exists."""
    try:
        bootstrap_path = state_path.parent / "bootstrap.md"
        if bootstrap_path.exists() and _read_bootstrap_context(state_path):
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


def _single_bootstrap_json_value(
    body: str,
    prefix_re: re.Pattern[str],
    line_re: re.Pattern[str],
) -> str | None:
    metadata = prefix_re.findall(body)
    matches = line_re.findall(body)
    if len(metadata) != 1 or len(matches) != 1:
        return None
    try:
        value = json.loads(matches[0])
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, str) else None


def _bootstrap_provenance_matches(body: str, state_path: Path) -> bool:
    """Require bootstrap provenance to match the exact owned state."""
    slug = _single_bootstrap_json_value(
        body,
        BOOTSTRAP_PROJECT_SLUG_JSON_PREFIX_RE,
        BOOTSTRAP_PROJECT_SLUG_JSON_LINE_RE,
    )
    project_root = _single_bootstrap_json_value(
        body,
        BOOTSTRAP_PROJECT_ROOT_JSON_PREFIX_RE,
        BOOTSTRAP_PROJECT_ROOT_JSON_LINE_RE,
    )
    recorded_state_path = _single_bootstrap_json_value(
        body,
        BOOTSTRAP_STATE_PATH_JSON_PREFIX_RE,
        BOOTSTRAP_STATE_PATH_JSON_LINE_RE,
    )
    if (
        slug is None
        or not is_canonical_project_slug(slug)
        or project_root is None
        or recorded_state_path is None
        or not _is_native_absolute_root(project_root)
        or not _is_native_absolute_root(recorded_state_path)
    ):
        return False

    state_body = _read_state_ownership_body(state_path)
    if state_body is None:
        return False
    state_root = _recorded_project_root(state_body)
    state_slug = _recorded_runtime_slug(state_body)
    if state_root is None or state_slug != slug:
        return False
    try:
        return (
            _path_comparison_key(Path(project_root).resolve())
            == _path_comparison_key(Path(state_root).resolve())
            and _path_comparison_key(Path(recorded_state_path).resolve())
            == _path_comparison_key(state_path.resolve())
            and _state_path_owns_project(state_path, Path(project_root))
        )
    except (OSError, RuntimeError, ValueError):
        return False


def _read_bootstrap_context(state_path: Path) -> str:
    """Read a bounded sibling bootstrap file without following path escapes."""
    try:
        bootstrap_path = _resolve_under(
            state_path.with_name("bootstrap.md"),
            state_path.parent,
        )
        if not bootstrap_path.is_file():
            return ""
        with bootstrap_path.open(
            "r",
            encoding="utf-8",
            errors="replace",
        ) as handle:
            text = handle.read(MAX_BOOTSTRAP_READ_CHARS + 1)
    except (OSError, RuntimeError, ValueError):
        return ""

    truncated = len(text) > MAX_BOOTSTRAP_READ_CHARS
    text = text[:MAX_BOOTSTRAP_READ_CHARS]
    if truncated or not _bootstrap_provenance_matches(text, state_path):
        return ""
    if text.startswith("---\n"):
        frontmatter_end = text.find("\n---\n", 4)
        if frontmatter_end >= 0:
            text = text[frontmatter_end + 5:]

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


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n… (truncated for hook injection)\n"
    if limit <= len(marker):
        return text[:limit]
    return text[: limit - len(marker)].rstrip() + marker


def _state_context_source(state_path: Path) -> str:
    resolved = state_path.resolve()
    projects_dir = state_path.parent.parent.resolve()
    contained = _resolve_under(resolved, projects_dir)
    if projects_dir.name == "projects" and projects_dir.parent.name == "knowledge":
        return contained.relative_to(projects_dir.parent.parent).as_posix()
    return contained.relative_to(projects_dir.parent).as_posix()


def _build_context(state_path: Path, slug: str, is_new: bool) -> str:
    """Build the additionalContext payload around the state.md content."""
    body = _read_state_ownership_body(state_path)
    if body is None:
        return f"(project state at `{state_path}` unavailable or exceeds the read limit)"

    header = (
        f"# Per-project state — `{slug}`\n"
        f"\n"
        f"(Auto-injected from `{_state_context_source(state_path)}`"
        + (" — freshly created for this project." if is_new else ".")
        + ")\n\n"
    )
    bootstrap = _read_bootstrap_context(state_path)
    if not bootstrap:
        return _clip(header + body, MAX_CONTEXT_CHARS)

    identity, remainder = _split_state_identity(body)
    handoff, detail = _split_state_handoff(remainder)
    parts = [header.rstrip()]
    if identity:
        parts.append(identity)
    if handoff:
        parts.append(handoff)
    elif detail:
        parts.extend(("## Saved project state", detail))
        detail = ""
    parts.extend(
        (
            "## Project bootstrap (UNTRUSTED project-derived data)",
            bootstrap,
        )
    )
    if detail:
        parts.extend(("## Saved project state", detail))
    payload = "\n\n".join(parts) + "\n"
    return _clip(payload, MAX_CONTEXT_CHARS)


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

        # 3. Retry bootstrap until the detached worker publishes its output.
        _bootstrap_project_state(vault, project_dir, state_path)

        # 4. Build and emit context.
        return _emit(_build_context(state_path, slug, is_new))

    except Exception:  # noqa: BLE001 — hook MUST exit 0
        _safe_write_error("unhandled:\n" + traceback.format_exc())
        return _emit_empty()


if __name__ == "__main__":
    raise SystemExit(main())
