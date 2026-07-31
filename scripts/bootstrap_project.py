"""Bootstrap a new project into the vault from its existing git history.

When you start tracking a new project, this script auto-generates
seed knowledge pages from:
- git log (key commits → timeline of decisions)
- README.md (project description)
- docs/ directory (existing documentation)
- Directory structure (architecture overview)

This replaces the manual process of writing state.md from scratch.
One command → the project has context for the first SessionStart.

Usage:
    uv run python scripts/bootstrap_project.py --cwd /path/to/project
    uv run python scripts/bootstrap_project.py --cwd /path/to/project --apply
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import (  # noqa: E402
    ROOT,
    STATE_DIR,
    advisory_file_lock,
    atomic_write,
    bounded_path_inventory,
)
from secret_redact import redact_secrets  # noqa: E402

PROJECTS_DIR = ROOT / "knowledge" / "projects"
TEMPLATE = PROJECTS_DIR / "_template" / "state.md"
MAX_README_BYTES = 64 * 1024
MAX_PACKAGE_JSON_BYTES = 256 * 1024
MAX_DOC_FILES = 15
MAX_DOC_ENTRIES_SCANNED = 256
BOOTSTRAP_TRUNCATION_MARKER = "... (bootstrap truncated)"


def _read_text_prefix(
    path: Path,
    max_bytes: int,
    *,
    project_root: Path,
    reject_oversized: bool,
) -> str | None:
    metadata = _regular_project_file(path, project_root)
    if metadata is None:
        return None
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not os.path.samestat(metadata, opened)
                or not stat.S_ISREG(opened.st_mode)
                or _is_reparse_point(opened)
            ):
                return None
            raw = handle.read(max_bytes + 1)
    except OSError:
        return None
    if len(raw) > max_bytes and reject_oversized:
        return None
    try:
        return raw[:max_bytes].decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None


def _is_reparse_point(metadata) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _resolved_project_root(cwd: str) -> Path | None:
    try:
        root = Path(cwd).resolve(strict=True)
        return root if root.is_dir() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _regular_project_file(path: Path, project_root: Path):
    try:
        metadata = path.lstat()
    except OSError:
        return None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(metadata)
    ):
        return None
    try:
        path.resolve(strict=True).relative_to(project_root)
    except (OSError, RuntimeError, ValueError):
        return None
    return metadata


def _run_git(cwd: str, *args: str) -> str:
    """Run a git command and return stdout."""
    try:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _extract_git_timeline(cwd: str, max_commits: int = 30) -> list[str]:
    """Extract key commits as a timeline."""
    log = _run_git(cwd, "log", "--oneline", f"-{max_commits}", "--no-merges")
    if not log:
        return []
    lines = log.splitlines()
    # Filter to meaningful commits (skip pure formatting/merge)
    meaningful = []
    for line in lines:
        msg = line.split(":", 1)[-1].strip() if " " in line else line
        lower = msg.lower()
        if any(skip in lower for skip in ("formatting", "merge branch", "bump version", "update .gitignore")):
            continue
        meaningful.append(f"- `{line.strip()}`")
    return meaningful[:20]


def _extract_readme_summary(cwd: str) -> str:
    """Extract project description from README."""
    project_root = _resolved_project_root(cwd)
    if project_root is None:
        return "(no README found)"
    for name in ("README.md", "README.rst", "README.txt", "README", "readme.md"):
        p = project_root / name
        content = _read_text_prefix(
            p,
            MAX_README_BYTES,
            project_root=project_root,
            reject_oversized=False,
        )
        if content is None:
            continue
        # Extract first meaningful paragraph (after title)
        lines = content.splitlines()
        summary_lines = []
        in_content = False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                if in_content and summary_lines:
                    break  # end of first paragraph
                continue
            if stripped.startswith("#"):
                in_content = True
                continue
            if in_content or not summary_lines:
                summary_lines.append(stripped)
            if len(summary_lines) >= 5:
                break
        return "\n".join(summary_lines) if summary_lines else content[:500]
    return "(no README found)"


def _extract_tech_stack(cwd: str) -> list[str]:
    """Detect tech stack from marker files."""
    project_root = _resolved_project_root(cwd)
    if project_root is None:
        return []
    markers = {
        "package.json": "Node.js / JavaScript",
        "pyproject.toml": "Python",
        "requirements.txt": "Python",
        "Cargo.toml": "Rust",
        "go.mod": "Go",
        "pom.xml": "Java / Maven",
        "build.gradle": "Java / Gradle",
        "Gemfile": "Ruby",
        "composer.json": "PHP",
        "mix.exs": "Elixir",
        "docker-compose.yml": "Docker",
        "Dockerfile": "Docker",
        ".gitlab-ci.yml": "GitLab CI",
        "Makefile": "Make",
    }
    stack = []
    for marker, tech in markers.items():
        if _regular_project_file(project_root / marker, project_root) is not None:
            stack.append(f"- {tech} (`{marker}`)")
    # Detect frameworks from package.json
    pkg = project_root / "package.json"
    if _regular_project_file(pkg, project_root) is not None:
        try:
            content = _read_text_prefix(
                pkg,
                MAX_PACKAGE_JSON_BYTES,
                project_root=project_root,
                reject_oversized=True,
            )
            data = json.loads(content) if content is not None else None
            if not isinstance(data, dict):
                return stack
            deps = {}
            for field in ("dependencies", "devDependencies"):
                candidate = data.get(field)
                if isinstance(candidate, dict):
                    deps.update(candidate)
            if "next" in deps:
                stack.append("- Next.js")
            if "react" in deps:
                stack.append("- React")
            if "vue" in deps:
                stack.append("- Vue.js")
            if "express" in deps:
                stack.append("- Express")
            if "typescript" in deps:
                stack.append("- TypeScript")
        except (json.JSONDecodeError, OSError):
            pass
    return stack


def _extract_docs_structure(cwd: str) -> list[str] | None:
    """List docs/ directory structure if it exists."""
    project_root = _resolved_project_root(cwd)
    if project_root is None:
        return None
    docs = project_root / "docs"
    try:
        metadata = docs.lstat()
    except FileNotFoundError:
        return []
    except OSError:
        return None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(metadata)
    ):
        return None
    try:
        docs.resolve(strict=True).relative_to(project_root)
    except (OSError, RuntimeError, ValueError):
        return None
    inventory = bounded_path_inventory(
        docs,
        "*.md",
        MAX_DOC_ENTRIES_SCANNED,
        recursive=True,
        kind="file",
    )
    if inventory.incomplete:
        return None
    if any(_regular_project_file(path, project_root) is None for path in inventory.paths):
        return None
    return [
        f"- `{path.relative_to(project_root).as_posix()}`"
        for path in inventory.paths[:MAX_DOC_FILES]
    ]


def _bounded_bootstrap_document(
    frontmatter: str,
    content: str,
    max_chars: int,
) -> str | None:
    prefix = frontmatter.rstrip() + "\n\n"
    body = content + "\n"
    complete = prefix + body
    if len(complete) <= max_chars:
        return complete

    marker = BOOTSTRAP_TRUNCATION_MARKER + "\n"
    if len(prefix) + len(marker) > max_chars:
        return None
    kept: list[str] = []
    used = len(prefix) + len(marker)
    for line in content.splitlines():
        line_size = len(line) + 1
        if used + line_size > max_chars:
            break
        kept.append(line)
        used += line_size
    return prefix + "".join(f"{line}\n" for line in kept) + marker


def bootstrap(cwd: str, apply: bool = False) -> str:
    """Generate a bootstrap context for a new project."""
    try:
        from session_start_project_state import (
            MAX_BOOTSTRAP_READ_CHARS,
            _base_slug,
            _read_bootstrap_context,
            confirm_project_identity,
        )
    except Exception:
        return "Skipped: bootstrap helpers unavailable"
    try:
        project_root = Path(cwd).resolve()
        if not project_root.is_dir():
            return "Skipped: project path unavailable"
    except (OSError, RuntimeError, ValueError):
        return "Skipped: project path unavailable"
    try:
        confirmed = (
            confirm_project_identity(project_root, PROJECTS_DIR)
            if apply
            else None
        )
    except Exception:
        confirmed = None
    if apply and confirmed is None:
        return "Skipped: project identity unavailable"
    if confirmed is None:
        slug = _base_slug(project_root)
        state_path = None
        bootstrap_path = None
    else:
        slug, state_path, _is_new = confirmed
        bootstrap_path = state_path.with_name("bootstrap.md")
    lock = (
        advisory_file_lock(
            STATE_DIR / "bootstrap-project" / f"{slug}.lock",
            description=f"bootstrap for {slug}",
        )
        if apply
        else nullcontext()
    )

    with lock:
        if (
            apply
            and bootstrap_path is not None
            and state_path is not None
            and bootstrap_path.exists()
            and _read_bootstrap_context(state_path)
        ):
            return f"Already exists: {bootstrap_path.relative_to(ROOT)}"

        # Redact every field before it lands in a vault file that may later
        # be exported or shared, matching the capture-hook contract.
        canonical_cwd = str(project_root)
        timeline = [redact_secrets(t) for t in _extract_git_timeline(canonical_cwd)]
        readme_summary = redact_secrets(_extract_readme_summary(canonical_cwd))
        tech_stack = [redact_secrets(t) for t in _extract_tech_stack(canonical_cwd)]
        raw_docs_structure = _extract_docs_structure(canonical_cwd)
        docs_structure = (
            None
            if raw_docs_structure is None
            else [redact_secrets(item) for item in raw_docs_structure]
        )
        git_remote = redact_secrets(_run_git(canonical_cwd, "remote", "get-url", "origin"))
        last_commit = redact_secrets(_run_git(canonical_cwd, "log", "-1", "--format=%ci"))

        parts = [
            f"# {slug} — Bootstrap Context",
            "",
            f"One-sentence summary: Auto-generated project context for {slug}.",
            "",
            "## Project description",
            readme_summary,
            "",
        ]

        if tech_stack:
            parts.append("## Tech stack")
            parts.extend(tech_stack)
            parts.append("")

        if timeline:
            parts.append(f"## Recent git history ({len(timeline)} commits)")
            parts.extend(timeline)
            parts.append("")

        if docs_structure is None:
            parts.append("## Existing documentation")
            parts.append("- (documentation inventory unavailable)")
            parts.append("")
        elif docs_structure:
            parts.append("## Existing documentation")
            parts.extend(docs_structure)
            parts.append("")

        if git_remote:
            parts.append("## Git remote")
            parts.append(f"- `{git_remote}`")
            parts.append("")

        if last_commit:
            parts.append(f"## Last commit: {last_commit}")

        content = "\n".join(parts)

        if apply:
            if state_path is None or bootstrap_path is None:
                return "Skipped: project identity unavailable"
            reconfirmed = confirm_project_identity(project_root, PROJECTS_DIR)
            if (
                reconfirmed is None
                or reconfirmed[0] != slug
                or reconfirmed[1].resolve() != state_path.resolve()
            ):
                return "Skipped: project identity changed before publication"
            frontmatter = (
                "---\n"
                "type: bootstrap-context\n"
                f'title: "{slug} bootstrap"\n'
                'description: "Auto-generated from git history + README"\n'
                f"timestamp: {datetime.now().isoformat(timespec='seconds')}\n"
                f"project_slug_json: {json.dumps(slug, ensure_ascii=False)}\n"
                f"project_root_json: {json.dumps(str(project_root), ensure_ascii=False)}\n"
                f"project_state_path_json: "
                f"{json.dumps(str(state_path.resolve()), ensure_ascii=False)}\n"
                "---"
            )
            serialized = _bounded_bootstrap_document(
                frontmatter,
                content,
                MAX_BOOTSTRAP_READ_CHARS,
            )
            if serialized is None:
                return "Skipped: mandatory bootstrap provenance exceeds publication limit"
            atomic_write(
                bootstrap_path,
                serialized,
            )
            return f"Written: {bootstrap_path.relative_to(ROOT)}"
        return content


def main() -> int:
    p = argparse.ArgumentParser(description="Bootstrap a project into the vault.")
    p.add_argument("--cwd", required=True, help="Project directory")
    p.add_argument("--apply", action="store_true", help="Write to vault (default: dry-run)")
    args = p.parse_args()

    result = bootstrap(args.cwd, args.apply)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
