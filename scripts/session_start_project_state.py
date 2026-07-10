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
from datetime import datetime
from pathlib import Path

# Force utf-8 on stdout (Windows cp1252 mojibakes Cyrillic otherwise).
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

MAX_CONTEXT_CHARS = 2400  # keep the injection compact
SLUG_UNSAFE_RE = re.compile(r"[\s_/\\:*?\"<>|]+")

# Collision disambiguation cap — try this many candidate slugs before
# falling back to a path-hash suffix. Four covers: base, base-pop,
# base-owner-repo, base-grandparent. Any beyond that is pathological.
MAX_SLUG_CANDIDATES = 4

# How many hex chars from the project-dir hash to append when all other
# disambiguation strategies fail. 6 = 16.7M possibilities, plenty.
PATH_HASH_SUFFIX_LEN = 6

# Regex matching a `Source:` line in state.md that records the project
# root. Used to detect slug collisions (existing state.md pointing at
# a different project dir).
STATE_SOURCE_LINE_RE = re.compile(
    r"^- Project root:\s*`([^`]+)`", re.MULTILINE
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
    s = text.lower()
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
    gitcfg = project_dir / ".git" / "config"
    if not gitcfg.is_file():
        return None
    try:
        text = gitcfg.read_text(encoding="utf-8", errors="ignore")
    except OSError:
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
    m = STATE_SOURCE_LINE_RE.search(body)
    if not m:
        # STRICT: state.md without `- Project root:` is ambiguous. We
        # cannot prove it belongs to `project_dir`, so treat as taken
        # and move on. Caller will try parent-of-parent, git remote, etc.
        return False
    recorded = m.group(1).strip()
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

    Returns the first candidate that either doesn't exist or already
    belongs to `project_dir` (same recorded Project root).
    """
    base = _base_slug(project_dir)

    candidates: list[str] = [base]

    # parent-of-parent: e.g. <your-projects-dir>/your-app\backend → backend-your-app
    parent_of_parent = project_dir.parent.name if project_dir.parent else ""
    pop = _sanitize(parent_of_parent)
    if pop and pop != base:
        candidates.append(f"{base}-{pop}")

    # git owner-repo (may be absent or same as parent — dedup)
    gr = _git_remote_slug(project_dir)
    if gr and gr not in candidates:
        candidates.append(gr)

    # grandparent-of-parent as extra fallback before hash
    grand = project_dir.parent.parent.name if project_dir.parent and project_dir.parent.parent else ""
    gp = _sanitize(grand)
    if gp and gp != base and gp != pop:
        candidate = f"{base}-{gp}"
        if candidate not in candidates:
            candidates.append(candidate)

    for cand in candidates[:MAX_SLUG_CANDIDATES]:
        if _slug_owns_dir(cand, project_dir, projects_dir):
            return cand

    # All predictable slugs are taken by other projects — fall back to
    # a deterministic hash suffix. Guaranteed unique per path.
    return f"{base}-{_path_hash_suffix(project_dir)}"


def _resolve_project_dir() -> Path:
    """CLAUDE_PROJECT_DIR if set, else cwd."""
    raw = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return Path(raw).resolve()


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
    filled = (
        tmpl
        .replace("<Project Name>", slug)
        .replace("<what this project is, in one sentence>",
                 f"(new project at `{project_dir}`, pending description)")
        .replace("<absolute path>", str(project_dir))
        .replace("<remote url>", "(unknown — set manually if applicable)")
    )
    return filled


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 30].rstrip() + "\n… (truncated for hook injection)\n"


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

        # 2. Resolve current project and compute a collision-safe slug.
        project_dir = _resolve_project_dir()
        slug = _compute_slug(project_dir, projects_dir)
        state_path = projects_dir / slug / "state.md"

        # 3. Ensure state.md exists — creation gated on project markers.
        is_new = False
        if not state_path.exists():
            # Without a project marker, stay read-only and skip. This avoids
            # cluttering the vault with throwaway cwd dirs.
            if not _has_project_marker(project_dir):
                return _emit_empty()
            template = projects_dir / "_template" / "state.md"
            if not template.exists():
                _safe_write_error(f"template missing: {template}")
                return _emit_empty()
            try:
                state_path.parent.mkdir(parents=True, exist_ok=True)
                # Atomic write: two concurrent Claude Code instances in the
                # same project on first visit would otherwise race and
                # interleave content. Write to `.tmp` then rename — on
                # POSIX and modern Windows (NTFS), rename over an existing
                # file is atomic, and mkdir exist_ok races resolve fine.
                tmp_path = state_path.with_suffix(".md.tmp")
                tmp_path.write_text(
                    _render_new_state(template, slug, project_dir),
                    encoding="utf-8",
                )
                os.replace(tmp_path, state_path)
                is_new = True

                # Bootstrap: auto-generate context from git + README.
                # Only on first discovery — gives the new project immediate
                # context without manual state.md editing.
                try:
                    bootstrap_path = state_path.parent / "bootstrap.md"
                    if not bootstrap_path.exists():
                        import subprocess as _sp
                        _sp.run(
                            [sys.executable, str(vault / "scripts" / "bootstrap_project.py"),
                             "--cwd", str(project_dir), "--apply"],
                            capture_output=True, timeout=30, check=False,
                            cwd=str(vault),
                        )
                except Exception:
                    pass  # never block session start on bootstrap failure
            except OSError as e:
                _safe_write_error(
                    f"failed to create state.md at {state_path}: {e}"
                )
                return _emit_empty()

        # 4. Build and emit context.
        return _emit(_build_context(state_path, slug, is_new))

    except Exception:  # noqa: BLE001 — hook MUST exit 0
        _safe_write_error("unhandled:\n" + traceback.format_exc())
        return _emit_empty()


if __name__ == "__main__":
    raise SystemExit(main())
