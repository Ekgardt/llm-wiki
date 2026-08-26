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
    5. Last resort: append a deterministic 6-char SHA-256 suffix of the
       absolute project path — guaranteed unique.
    Ownership is determined by strict match of the `- Project root:` line
    in the existing `state.md`. A state.md without that line is treated
    as NOT ours (forces disambiguation; worst case is a hash-suffixed
    slug, safer than cross-contamination).
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import traceback
import unicodedata
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from markdown_transaction import mutate_knowledge, stable_operation_id
from project_journal import (
    ProjectStore,
    legacy_state_project_root,
    recover_project_handoff,
)
from secret_redact import redact_secrets

# Force utf-8 on stdout (Windows cp1252 mojibakes Cyrillic otherwise).
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

MAX_CONTEXT_CHARS = 2400  # keep the injection compact
MAX_PROJECT_ROOT_CHARS = 32 * 1024
SLUG_UNSAFE_RE = re.compile(r"[\s_/\\:*?\"<>|]+")

# Collision disambiguation cap — try this many candidate slugs before
# falling back to a path-hash suffix. Four covers: base, base-pop,
# base-owner-repo, base-grandparent. Any beyond that is pathological.
MAX_SLUG_CANDIDATES = 4

# How many hex chars from the project-dir hash to append when all other
# disambiguation strategies fail. 6 = 16.7M possibilities, plenty.
PATH_HASH_SUFFIX_LEN = 6

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


def _sanitize(text: str) -> str:
    """Lowercase + replace unsafe chars + strip hyphens. Preserve non-ASCII."""
    s = unicodedata.normalize("NFC", text).lower()
    s = SLUG_UNSAFE_RE.sub("-", s)
    s = s.strip("-")
    if not s or s in {".", ".."}:
        return ""
    return s


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
    url = _origin_remote_url(project_dir)
    if not url:
        return None
    return _owner_repo_slug(url)


def _origin_remote_url(project_dir: Path) -> str | None:
    """The `origin` URL recorded in `.git/config`, or None."""
    gitcfg = project_dir / ".git" / "config"
    if not gitcfg.is_file():
        return None
    try:
        text = gitcfg.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    # Find [remote "origin"] section and its url = ...
    # Format: [remote "origin"]\n\turl = <url>
    match = re.search(
        r'\[remote\s+"origin"\]\s*\n(?:\s+[^\n]+\n)*?\s+url\s*=\s*(\S+)',
        text,
    )
    if not match:
        return None
    return match.group(1).strip()


def _owner_repo_slug(url: str) -> str | None:
    """`owner-repo` from an SSH or HTTPS remote URL.

    Accepted forms:
      git@host:owner/repo(.git)
      https://host/owner/repo(.git)
      https://host/path/to/owner/repo(.git)
    """
    match = re.search(r"[:/]([^:/]+)/([^/]+?)(?:\.git)?/*$", url)
    if not match:
        return None
    owner = _sanitize(match.group(1))
    repo = _sanitize(match.group(2))
    if not owner or not repo:
        return None
    return f"{owner}-{repo}"


def _path_hash_suffix(project_dir: Path) -> str:
    """Deterministic short hash from the absolute project path.

    Guarantees uniqueness even when parent folder, grandparent folder,
    AND git owner-repo would all collide (pathological case). Short
    enough to stay readable: `backend-a3f7b2`.
    """
    import hashlib
    h = hashlib.sha256(str(project_dir.resolve()).encode("utf-8")).hexdigest()
    return h[:PATH_HASH_SUFFIX_LEN]


def _slug_owns_dir(slug: str, project_dir: Path, projects_dir: Path) -> bool:
    """True ONLY if `projects_dir/slug/state.md` either doesn't exist or
    explicitly records `project_dir` as its Project root.

    Strict (hardened per colleague review — "state.md without Project root
    line"): a state.md that exists but lacks a parseable
    `- Project root:` line is treated as NOT ours. Previously we assumed
    "probably hand-edited — treat as ours", but that allowed a collision
    where deleting the Source section let a second project silently adopt
    the first's state.md. Now we force disambiguation (the caller tries
    the next candidate slug) and the worst case is a deterministic
    hash-suffixed slug — safer than cross-contamination.

    Safe read-side behavior: on read (no auto-create fired), a state.md
    missing its Source line will still be read correctly — we just won't
    WRITE over it. The read path in main() doesn't consult this function.
    """
    state_path = projects_dir / slug / "state.md"
    if not state_path.exists():
        return True  # unused slug — free to take
    try:
        body = state_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False  # unreadable → treat as collision, disambiguate away
    recorded = legacy_state_project_root(body)
    if recorded is None:
        # STRICT: state.md without `- Project root:` is ambiguous. We
        # cannot prove it belongs to `project_dir`, so treat as taken
        # and move on. Caller will try parent-of-parent, git remote, etc.
        return False
    # Normalize both sides for a fair comparison. Windows paths use
    # backslashes in state.md; resolve() + as_posix() for comparison.
    try:
        recorded_norm = Path(recorded).resolve().as_posix().lower()
        current_norm = project_dir.resolve().as_posix().lower()
    except (OSError, ValueError):
        return recorded == str(project_dir)
    return recorded_norm == current_norm


def _compute_slug(project_dir: Path, projects_dir: Path) -> str:
    """Compute the slug for a project, resolving collisions.

    Strategy (documented in `knowledge/notes/Global Multi-Project Migration
    Plan.md`):
      1. Parent folder name, sanitized.
      2. On collision: parent + parent-of-parent (e.g. `backend-your-app`).
      3. On further collision: git `owner-repo` from origin remote.
      4. On further collision: base + path-hash suffix (always unique).

    An agent worktree resolves to the checkout that owns it before any of this
    runs, so a subagent's temporary copy does not mint a project of its own.

    Returns the first candidate that either doesn't exist or already
    belongs to `project_dir` (same recorded Project root).
    """
    project_dir = owning_checkout(project_dir)
    base = _base_slug(project_dir)
    for cand in _slug_candidates(project_dir, base)[:MAX_SLUG_CANDIDATES]:
        if _slug_owns_dir(cand, project_dir, projects_dir):
            return cand
    # All predictable slugs are taken by other projects — fall back to
    # a deterministic hash suffix. Guaranteed unique per path.
    return f"{base}-{_path_hash_suffix(project_dir)}"


def _ancestor_name(project_dir: Path, generations: int) -> str:
    """The sanitized name of an ancestor directory, or empty.

    Indexed rather than walked, and that is load bearing. The walk rebound one
    name twice — `current = project_dir` and then `current = parent` — and the
    producer-boundary guard in `tests/test_context_compiler.py` cannot settle on
    a name that carries two different values: its alias fixpoint flips the
    binding every pass and never terminates, so the whole test session hangs
    instead of failing. That is the guard's defect, not this function's, and
    eight lines of ordinary Python reproduce it; it is recorded in
    `knowledge/log.md`. Until the guard is fixed, no producer it reads may
    contain the shape.
    """
    ancestors = (project_dir, *project_dir.parents)
    if generations >= len(ancestors):
        return ""
    return _sanitize(ancestors[generations].name)


def _compound_slug(base: str, part: str) -> tuple[str, ...]:
    """`base-part`, unless the part is empty or just repeats the base."""
    if not part or part == base:
        return ()
    return (f"{base}-{part}",)


def _remote_slug(project_dir: Path) -> tuple[str, ...]:
    """The git `owner-repo` slug, when the checkout declares an origin."""
    remote = _git_remote_slug(project_dir)
    if not remote:
        return ()
    return (remote,)


def _slug_candidates(project_dir: Path, base: str) -> list[str]:
    """The predictable slugs for this directory, strongest first, deduplicated.

    Order: the folder name, then folder + parent (`backend-your-app`), then the
    git `owner-repo`, then folder + grandparent.
    """
    proposed = [
        base,
        *_compound_slug(base, _ancestor_name(project_dir, 1)),
        *_remote_slug(project_dir),
        *_compound_slug(base, _ancestor_name(project_dir, 2)),
    ]
    unique: list[str] = []
    for candidate in proposed:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def _is_control_code(code: int) -> bool:
    return code < 32 or 127 <= code <= 159


def _is_unsafe_code_point(code: int) -> bool:
    return (
        code in {0x2028, 0x2029}
        or 0xD800 <= code <= 0xDFFF
        or (code & 0xFFFE) == 0xFFFE
    )


def _is_forbidden_root_char(char: str) -> bool:
    code = ord(char)
    return _is_control_code(code) or _is_unsafe_code_point(code)


def _native_path_type(platform: str | None):
    """The path flavour of the platform this identity will be read on."""
    if (platform or sys.platform) == "win32":
        return PureWindowsPath
    return PurePosixPath


def _is_native_absolute_root(root: str, platform: str | None = None) -> bool:
    """Validate project identity before resolution can consult process cwd."""
    if not _is_bounded_root_text(root):
        return False
    return _native_path_type(platform)(root).is_absolute()


def _is_bounded_root_text(root: object) -> bool:
    """A non-empty, length-bounded string with no forbidden code points."""
    return (
        isinstance(root, str)
        and bool(root)
        and len(root) <= MAX_PROJECT_ROOT_CHARS
        and not any(_is_forbidden_root_char(char) for char in root)
    )


AGENT_WORKTREE_MARKER = (".claude", "worktrees")


def owning_checkout(project_dir: Path) -> Path:
    """The checkout a directory belongs to, seeing through an agent worktree.

    An agent's worktree under `<checkout>/.claude/worktrees/<name>` is a
    temporary copy of one project, not a project of its own. Reading its folder
    name as the project mints a journal per subagent run: measured on this vault
    on 2026-08-26, 46 of 61 project journals were named `agent-<hash>`, and they
    take answer slots from the pages that answer the question — nine of the
    twelve candidates for "как устроен повтор после карантина" were project
    journals, seven of them from agent worktrees.

    Only this exact layout is unwrapped. A worktree the owner made anywhere else
    stays a project of its own: nothing here can tell whether that was
    deliberate, and guessing would silently merge journals the owner separated.
    """
    parts = project_dir.parts
    width = len(AGENT_WORKTREE_MARKER)
    marks = [
        index
        for index in range(len(parts) - width + 1)
        if parts[index : index + width] == AGENT_WORKTREE_MARKER
    ]
    if not marks:
        return project_dir
    return Path(*parts[: marks[-1]])


def _resolve_project_dir() -> Path:
    """Resolve a bounded native absolute CLAUDE_PROJECT_DIR or cwd."""
    raw = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    if not _is_native_absolute_root(raw):
        raise ValueError("project root must be a bounded native absolute path")
    return owning_checkout(Path(raw).resolve())


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
    if _is_home_directory(project_dir):
        return False
    try:
        return any(_marker_present(project_dir, marker) for marker in PROJECT_MARKERS)
    except OSError:
        return False


def _is_home_directory(project_dir: Path) -> bool:
    """$HOME itself is never a project, whatever markers it happens to carry."""
    try:
        # Compare resolved paths to handle symlinks and casing.
        return project_dir.resolve() == Path.home().resolve()
    except (OSError, RuntimeError):
        # Path.home() can raise RuntimeError on truly exotic environments.
        # Fall through to marker detection; worst case is one nonsense slug.
        return False


def _marker_present(project_dir: Path, marker: str) -> bool:
    """One marker, by exact name and — for dotted suffixes — by glob."""
    if (project_dir / marker).exists():
        return True
    if not marker.startswith("."):
        return False
    # Suffix-based markers such as `.csproj` only ever match by glob.
    return any(project_dir.glob(f"*{marker}"))


def _render_new_state(state_template: Path, slug: str, project_dir: Path) -> str:
    """Return template content with placeholders filled for a new project."""
    tmpl = state_template.read_text(encoding="utf-8")
    filled = (
        tmpl
        .replace("<project-slug>", slug)
        .replace("<absolute-path>", str(project_dir))
        .replace("<Project Name>", slug)
        .replace("<what this project is, in one sentence>",
                 f"(new project at `{project_dir}`, pending description)")
        .replace("<absolute path>", str(project_dir))
        .replace("<remote url>", "(unknown — set manually if applicable)")
    )
    return filled


def _clip(text: str, limit: int) -> str:
    from context_budget import (
        DEFAULT_CONTEXT_BUDGET,
        BudgetExceededError,
        ContextItem,
    )
    from context_compiler import compile_context_items

    item = ContextItem(
        item_id="project-state",
        text=text,
        source="project-state",
        priority=3,
        relevance=1.0,
        confidence="high",
        freshness="fresh",
        token_cost=len(text.encode("utf-8")),
        mandatory=True,
        representation="l2",
        parent_id="project-state",
        priority_class="handoff",
    )
    try:
        return compile_context_items(
            [item], budget=DEFAULT_CONTEXT_BUDGET, emergency_byte_cap=limit
        ).text
    except BudgetExceededError as error:
        return error.failure.render(max_bytes=limit)


def _build_context(state_path: Path, slug: str, is_new: bool) -> str:
    """Build the additionalContext payload around the state.md content."""
    try:
        body = state_path.read_text(encoding="utf-8")
    except OSError as e:
        return f"(project state at `{state_path}` unreadable: {type(e).__name__})"

    header = (
        f"# Per-project state — `{slug}`\n"
        f"\n"
        f"(Auto-injected from `knowledge/projects/{slug}/state.md`"
        + (" — freshly created for this project." if is_new else ".")
        + ")\n\n"
    )
    payload = header + body
    return _clip(payload, MAX_CONTEXT_CHARS)


def main() -> int:
    try:
        return _run_session_start()
    except Exception:  # noqa: BLE001 — hook MUST exit 0
        _safe_write_error("unhandled:\n" + traceback.format_exc())
        return _emit_empty()


def _run_session_start() -> int:
    """1. Locate the vault. 2. Identify the project. 3. Emit its context."""
    vault_root = os.environ.get("LLM_WIKI_ROOT")
    if not vault_root:
        return _emit_empty()
    vault = Path(vault_root)
    projects_dir = vault / "knowledge" / "projects"
    if not projects_dir.is_dir():
        _safe_write_error(f"projects dir missing: {projects_dir}")
        return _emit_empty()
    project_dir = _resolve_project_dir()
    slug = _compute_slug(project_dir, projects_dir)
    return _emit_project_context(vault, projects_dir, project_dir, slug)


def _emit_project_context(
    vault: Path, projects_dir: Path, project_dir: Path, slug: str
) -> int:
    """The recovered handoff when there is one, else the project's own state."""
    state_root = _resolve_state_root()
    if state_root is None:
        return _emit_empty()
    # Recover reservations even before the first journal file is published.
    handoff = recover_project_handoff(
        ProjectStore(vault, state_root),
        slug,
        max_chars=MAX_CONTEXT_CHARS,
        project_root=project_dir,
    )
    journal_path = projects_dir / slug / "journal.md"
    if journal_path.is_file() or handoff.degraded or handoff.legacy:
        return _emit(handoff.context)
    return _emit_state_context(vault, projects_dir, project_dir, slug)


def _emit_state_context(
    vault: Path, projects_dir: Path, project_dir: Path, slug: str
) -> int:
    """Ensure state.md exists — creation is gated on project markers."""
    state_path = projects_dir / slug / "state.md"
    if state_path.exists():
        return _emit(_build_context(state_path, slug, False))
    if not _create_project_state(vault, projects_dir, project_dir, slug, state_path):
        return _emit_empty()
    return _emit(_build_context(state_path, slug, True))


def _create_project_state(
    vault: Path, projects_dir: Path, project_dir: Path, slug: str, state_path: Path
) -> bool:
    """Write the new state.md. False means "skip and emit nothing"."""
    # Without a project marker, stay read-only and skip. This avoids
    # cluttering the vault with throwaway cwd dirs.
    if not _has_project_marker(project_dir):
        return False
    template = projects_dir / "_template" / "state.md"
    if not template.exists():
        _safe_write_error(f"template missing: {template}")
        return False
    try:
        content = redact_secrets(_render_new_state(template, slug, project_dir))
        encoded = content.encode("utf-8")
        mutate_knowledge(
            stable_operation_id("project-state", slug, encoded),
            {state_path: encoded},
        )
    except OSError as e:
        _safe_write_error(f"failed to create state.md at {state_path}: {e}")
        return False
    _bootstrap_new_project(vault, project_dir, state_path)
    return True


def _bootstrap_new_project(vault: Path, project_dir: Path, state_path: Path) -> None:
    """Auto-generate context from git + README, on first discovery only.

    Gives the new project immediate context without manual state.md editing.
    """
    try:
        bootstrap_path = state_path.parent / "bootstrap.md"
        if bootstrap_path.exists():
            return
        import subprocess as _sp
        _sp.run(
            [sys.executable, str(vault / "scripts" / "bootstrap_project.py"),
             "--cwd", str(project_dir), "--apply"],
            capture_output=True, timeout=30, check=False,
            cwd=str(vault),
        )
    except Exception:  # noqa: BLE001
        pass  # never block session start on bootstrap failure


if __name__ == "__main__":
    raise SystemExit(main())
