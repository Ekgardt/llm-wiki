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
import hashlib
import json
import os
import stat
import sys
from contextlib import nullcontext
from dataclasses import dataclass
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
BOOTSTRAP_SCHEMA_VERSION = 2
MAX_BOOTSTRAP_SOURCE_FILES = 32
MAX_BOOTSTRAP_SOURCE_FILE_BYTES = 512 * 1024
MAX_BOOTSTRAP_SOURCE_TOTAL_BYTES = 2 * 1024 * 1024
README_CANDIDATES = ("README.md", "README.rst", "README.txt", "README", "readme.md")
TECH_MARKERS = {
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
_GIT_EXECUTABLE_UNSET = object()


@dataclass(frozen=True)
class BootstrapSourceFile:
    role: str
    relative_path: str
    mode: int
    size: int
    mtime_ns: int
    sha256: str
    content: bytes


@dataclass(frozen=True)
class BootstrapSourceSnapshot:
    project_root: str
    repository_kind: str
    git_head: str | None
    git_timeline: tuple[str, ...]
    git_remote: str
    last_commit: str
    source_files: tuple[BootstrapSourceFile, ...]
    fingerprint: str
    readme_summary: str
    tech_stack: tuple[str, ...]
    docs_structure: tuple[str, ...] | None


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


def _read_snapshot_source(
    path: Path,
    project_root: Path,
    role: str,
    remaining_bytes: int,
) -> BootstrapSourceFile | None:
    metadata = _regular_project_file(path, project_root)
    if metadata is None or metadata.st_size > min(
        MAX_BOOTSTRAP_SOURCE_FILE_BYTES,
        remaining_bytes,
    ):
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
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = handle.read(
                    min(64 * 1024, max(1, metadata.st_size - total + 1))
                )
                if not chunk:
                    break
                total += len(chunk)
                if total > metadata.st_size or total > remaining_bytes:
                    return None
                chunks.append(chunk)
            final = os.fstat(handle.fileno())
    except OSError:
        return None
    if (
        not os.path.samestat(opened, final)
        or total != final.st_size
        or opened.st_size != final.st_size
        or opened.st_mtime_ns != final.st_mtime_ns
        or opened.st_mode != final.st_mode
    ):
        return None
    content = b"".join(chunks)
    try:
        relative_path = path.resolve(strict=True).relative_to(project_root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return None
    return BootstrapSourceFile(
        role=role,
        relative_path=relative_path,
        mode=stat.S_IMODE(final.st_mode),
        size=final.st_size,
        mtime_ns=final.st_mtime_ns,
        sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )


def _readme_summary_from_content(content: str) -> str:
    lines = content.splitlines()
    summary_lines: list[str] = []
    in_content = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_content and summary_lines:
                break
            continue
        if stripped.startswith("#"):
            in_content = True
            continue
        if in_content or not summary_lines:
            summary_lines.append(stripped)
        if len(summary_lines) >= 5:
            break
    return "\n".join(summary_lines) if summary_lines else content[:500]


def _snapshot_repository_kind(project_root: Path, git_head: str | None) -> str:
    try:
        marker = (project_root / ".git").lstat()
    except OSError:
        return "non-git"
    if stat.S_ISLNK(marker.st_mode) or _is_reparse_point(marker):
        return "unsafe-git"
    return "git-head" if git_head is not None else "git-no-head"


def _collect_bootstrap_source_snapshot(
    project_root: Path,
    git_head: str | None,
    *,
    git_executable: Path | None | object = _GIT_EXECUTABLE_UNSET,
) -> BootstrapSourceSnapshot | None:
    try:
        canonical_root = project_root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not canonical_root.is_dir():
        return None

    repository_kind = _snapshot_repository_kind(canonical_root, git_head)
    git_timeline: tuple[str, ...] = ()
    git_remote = ""
    last_commit = ""
    if repository_kind in {"git-head", "git-no-head"}:
        if git_executable is _GIT_EXECUTABLE_UNSET:
            try:
                from session_start_project_state import _resolve_git_executable

                executable = _resolve_git_executable()
            except ImportError:
                return None
        else:
            executable = git_executable
        if not isinstance(executable, Path):
            return None
        canonical_cwd = str(canonical_root)
        git_timeline = tuple(
            _extract_git_timeline(
                canonical_cwd,
                git_executable=executable,
            )
        )
        git_remote = _run_git(
            canonical_cwd,
            "remote",
            "get-url",
            "origin",
            git_executable=executable,
        )
        last_commit = _run_git(
            canonical_cwd,
            "log",
            "-1",
            "--format=%ci",
            git_executable=executable,
        )

    sources: list[BootstrapSourceFile] = []
    remaining = MAX_BOOTSTRAP_SOURCE_TOTAL_BYTES

    def add(path: Path, role: str) -> BootstrapSourceFile | None:
        nonlocal remaining
        if len(sources) >= MAX_BOOTSTRAP_SOURCE_FILES:
            return None
        source = _read_snapshot_source(path, canonical_root, role, remaining)
        if source is None:
            return None
        sources.append(source)
        remaining -= source.size
        return source

    readme_source: BootstrapSourceFile | None = None
    for name in README_CANDIDATES:
        candidate = canonical_root / name
        if _regular_project_file(candidate, canonical_root) is None:
            continue
        readme_source = add(candidate, "readme")
        if readme_source is None:
            return None
        break

    marker_sources: dict[str, BootstrapSourceFile] = {}
    for marker in TECH_MARKERS:
        candidate = canonical_root / marker
        if _regular_project_file(candidate, canonical_root) is None:
            continue
        source = add(candidate, f"tech-marker:{marker}")
        if source is None:
            return None
        marker_sources[marker] = source

    docs_state = "absent"
    docs_structure: tuple[str, ...] | None = ()
    docs = canonical_root / "docs"
    try:
        docs_metadata = docs.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        docs_state = "unavailable"
        docs_structure = None
    else:
        if (
            not stat.S_ISDIR(docs_metadata.st_mode)
            or stat.S_ISLNK(docs_metadata.st_mode)
            or _is_reparse_point(docs_metadata)
        ):
            docs_state = "unavailable"
            docs_structure = None
        else:
            inventory = bounded_path_inventory(
                docs,
                "*.md",
                MAX_DOC_ENTRIES_SCANNED,
                recursive=True,
                kind="file",
            )
            if inventory.incomplete:
                docs_state = "unavailable"
                docs_structure = None
            else:
                selected_docs = inventory.paths[:MAX_DOC_FILES]
                rendered_docs: list[str] = []
                for path in selected_docs:
                    source = add(path, "documentation")
                    if source is None:
                        return None
                    rendered_docs.append(f"- `{source.relative_path}`")
                docs_state = "available"
                docs_structure = tuple(rendered_docs)

    if readme_source is None:
        readme_summary = "(no README found)"
    else:
        try:
            readme_text = readme_source.content[:MAX_README_BYTES].decode(
                "utf-8",
                errors="strict",
            )
        except UnicodeDecodeError:
            return None
        readme_summary = _readme_summary_from_content(readme_text)

    tech_stack = [
        f"- {technology} (`{marker}`)"
        for marker, technology in TECH_MARKERS.items()
        if marker in marker_sources
    ]
    package = marker_sources.get("package.json")
    if package is not None and package.size <= MAX_PACKAGE_JSON_BYTES:
        try:
            package_data = json.loads(package.content.decode("utf-8", errors="strict"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            package_data = None
        if isinstance(package_data, dict):
            dependencies: dict[str, object] = {}
            for field in ("dependencies", "devDependencies"):
                candidate = package_data.get(field)
                if isinstance(candidate, dict):
                    dependencies.update(candidate)
            for dependency, label in (
                ("next", "- Next.js"),
                ("react", "- React"),
                ("vue", "- Vue.js"),
                ("express", "- Express"),
                ("typescript", "- TypeScript"),
            ):
                if dependency in dependencies:
                    tech_stack.append(label)

    fingerprint_payload = {
        "schema": BOOTSTRAP_SCHEMA_VERSION,
        "project_root": str(canonical_root),
        "repository": {
            "kind": repository_kind,
            "head": git_head,
        },
        "git_values": {
            "timeline": list(git_timeline),
            "remote": git_remote,
            "last_commit": last_commit,
        },
        "docs_state": docs_state,
        "files": [
            {
                "role": source.role,
                "path": source.relative_path,
                "mode": source.mode,
                "size": source.size,
                "mtime_ns": source.mtime_ns,
                "sha256": source.sha256,
            }
            for source in sources
        ],
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return BootstrapSourceSnapshot(
        project_root=str(canonical_root),
        repository_kind=repository_kind,
        git_head=git_head,
        git_timeline=git_timeline,
        git_remote=git_remote,
        last_commit=last_commit,
        source_files=tuple(sources),
        fingerprint=fingerprint,
        readme_summary=readme_summary,
        tech_stack=tuple(tech_stack),
        docs_structure=docs_structure,
    )


def _bootstrap_source_fingerprint(
    project_root: Path,
    git_head: str | None,
    *,
    git_executable: Path | None | object = _GIT_EXECUTABLE_UNSET,
) -> str | None:
    snapshot = _collect_bootstrap_source_snapshot(
        project_root,
        git_head,
        git_executable=git_executable,
    )
    return snapshot.fingerprint if snapshot is not None else None


def _run_git(
    cwd: str,
    *args: str,
    git_executable: Path | None | object = _GIT_EXECUTABLE_UNSET,
) -> str:
    """Run a git command and return stdout."""
    try:
        from session_start_project_state import (
            MAX_GIT_IDENTITY_OUTPUT_CHARS,
            MAX_GIT_STDERR_BYTES,
            _git_subprocess_environment,
            _resolve_git_executable,
            _run_bounded_process,
        )

        executable = (
            _resolve_git_executable()
            if git_executable is _GIT_EXECUTABLE_UNSET
            else git_executable
        )
        if not isinstance(executable, Path):
            return ""
        result = _run_bounded_process(
            [executable, *args],
            cwd=Path(cwd),
            env=_git_subprocess_environment(),
            timeout=10,
            max_stdout_bytes=MAX_GIT_IDENTITY_OUTPUT_CHARS,
            max_stderr_bytes=MAX_GIT_STDERR_BYTES,
        )
        if result is None or result.returncode != 0:
            return ""
        return result.stdout.decode("utf-8", errors="strict").strip()
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        return ""


def _extract_git_timeline(
    cwd: str,
    max_commits: int = 30,
    *,
    git_executable: Path | None | object = _GIT_EXECUTABLE_UNSET,
) -> list[str]:
    """Extract key commits as a timeline."""
    log = _run_git(
        cwd,
        "log",
        "--oneline",
        f"-{max_commits}",
        "--no-merges",
        git_executable=git_executable,
    )
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
            _current_project_git_head,
            _read_bootstrap_context,
            _resolve_git_executable,
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
            and _read_bootstrap_context(state_path, slug, project_root)
        ):
            return f"Already exists: {bootstrap_path.relative_to(ROOT)}"

        git_executable = _resolve_git_executable()
        git_head: str | None = None
        if apply:
            git_status, git_head = _current_project_git_head(
                project_root,
                git_executable=git_executable,
            )
            if not git_status:
                return "Skipped: current project Git provenance unavailable"

        source_snapshot = _collect_bootstrap_source_snapshot(
            project_root,
            git_head,
            git_executable=git_executable,
        )
        if source_snapshot is None:
            return "Skipped: project source snapshot unavailable"

        # Redact every field before it lands in a vault file that may later
        # be exported or shared, matching the capture-hook contract.
        timeline = [redact_secrets(t) for t in source_snapshot.git_timeline]
        readme_summary = redact_secrets(source_snapshot.readme_summary)
        tech_stack = [redact_secrets(t) for t in source_snapshot.tech_stack]
        raw_docs_structure = source_snapshot.docs_structure
        docs_structure = (
            None
            if raw_docs_structure is None
            else [redact_secrets(item) for item in raw_docs_structure]
        )
        git_remote = redact_secrets(source_snapshot.git_remote)
        last_commit = redact_secrets(source_snapshot.last_commit)

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
            current_git_status, current_git_head = _current_project_git_head(
                project_root,
                git_executable=git_executable,
            )
            if not current_git_status or current_git_head != git_head:
                return "Skipped: project Git provenance changed before publication"
            current_source_snapshot = _collect_bootstrap_source_snapshot(
                project_root,
                current_git_head,
                git_executable=git_executable,
            )
            if (
                current_source_snapshot is None
                or current_source_snapshot.fingerprint != source_snapshot.fingerprint
            ):
                return "Skipped: project source snapshot changed before publication"
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
                f"git_head_json: {json.dumps(git_head)}\n"
                f"bootstrap_schema_json: {BOOTSTRAP_SCHEMA_VERSION}\n"
                "source_fingerprint_json: "
                f"{json.dumps(source_snapshot.fingerprint)}\n"
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
