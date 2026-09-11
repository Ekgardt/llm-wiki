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
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from markdown_transaction import mutate_knowledge, stable_operation_id  # noqa: E402
from memory_state import ROOT  # noqa: E402
from secret_redact import redact_secrets  # noqa: E402

PROJECTS_DIR = ROOT / "knowledge" / "projects"
TEMPLATE = PROJECTS_DIR / "_template" / "state.md"


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


def _compute_slug(cwd: str) -> str:
    """Compute the project slug using the existing 5-step algorithm."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from session_start_project_state import _compute_slug as _slug
        return _slug(Path(cwd).resolve(), PROJECTS_DIR)
    except Exception:
        return Path(cwd).resolve().name.lower().replace(" ", "-")


_TRIVIAL_COMMITS = ("formatting", "merge branch", "bump version", "update .gitignore")


def _extract_git_timeline(cwd: str, max_commits: int = 30) -> list[str]:
    """Extract key commits as a timeline."""
    log = _run_git(cwd, "log", "--oneline", f"-{max_commits}", "--no-merges")
    if not log:
        return []
    # Filter to meaningful commits (skip pure formatting/merge)
    return [f"- `{line.strip()}`" for line in log.splitlines() if not _trivial_commit(line)][:20]


def _trivial_commit(line: str) -> bool:
    msg = line.split(":", 1)[-1].strip() if " " in line else line
    lower = msg.lower()
    return any(skip in lower for skip in _TRIVIAL_COMMITS)


_README_NAMES = ("README.md", "README.rst", "README.txt", "README", "readme.md")


def _extract_readme_summary(cwd: str) -> str:
    """Extract project description from README."""
    for name in _README_NAMES:
        content = _readable_text(Path(cwd) / name)
        if content is not None:
            return _readme_paragraph(content)
    return "(no README found)"


def _readable_text(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _readme_paragraph(content: str) -> str:
    reader = _ParagraphReader()
    for line in content.splitlines():
        if reader.done_after(line.strip()):
            break
    return "\n".join(reader.lines) if reader.lines else content[:500]


class _ParagraphReader:
    """The first meaningful paragraph after the title, at most five lines."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.in_content = False

    def done_after(self, stripped: str) -> bool:
        """Take one stripped line; True once the paragraph is complete."""
        if not stripped:
            return self.in_content and bool(self.lines)  # a blank line ends the first paragraph
        if stripped.startswith("#"):
            self.in_content = True
            return False
        return self._take(stripped)

    def _take(self, stripped: str) -> bool:
        if self.in_content or not self.lines:
            self.lines.append(stripped)
        return len(self.lines) >= 5


_STACK_MARKERS = {
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
_PACKAGE_FRAMEWORKS = (
    ("next", "Next.js"),
    ("react", "React"),
    ("vue", "Vue.js"),
    ("express", "Express"),
    ("typescript", "TypeScript"),
)


def _extract_tech_stack(cwd: str) -> list[str]:
    """Detect tech stack from marker files."""
    stack = [
        f"- {tech} (`{marker}`)"
        for marker, tech in _STACK_MARKERS.items()
        if (Path(cwd) / marker).exists()
    ]
    stack.extend(_package_frameworks(Path(cwd) / "package.json"))
    return stack


def _package_frameworks(pkg: Path) -> list[str]:
    """Detect frameworks from package.json."""
    if not pkg.exists():
        return []
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    except (json.JSONDecodeError, OSError):
        return []
    return [f"- {name}" for dependency, name in _PACKAGE_FRAMEWORKS if dependency in deps]


def _extract_docs_structure(cwd: str) -> list[str]:
    """List docs/ directory structure if it exists."""
    docs = Path(cwd) / "docs"
    if not docs.exists():
        return []
    files = []
    for p in sorted(docs.rglob("*.md")):
        if p.is_file():
            rel = p.relative_to(Path(cwd)).as_posix()
            files.append(f"- `{rel}`")
    return files[:15]


def bootstrap(cwd: str, apply: bool = False) -> str:
    """Generate a bootstrap context for a new project."""
    slug = _compute_slug(cwd)
    project_dir = PROJECTS_DIR / slug
    content = _bootstrap_content(cwd, slug)
    if not apply:
        return content
    bootstrap_path = project_dir / "bootstrap.md"
    encoded = _bootstrap_page(slug, content).encode("utf-8")
    mutate_knowledge(
        stable_operation_id("bootstrap", slug, encoded), {bootstrap_path: encoded}
    )
    return f"Written: {bootstrap_path.relative_to(ROOT)}"


def _redacted(items: list[str]) -> list[str]:
    return [redact_secrets(item) for item in items]


def _section(heading: str, lines: list[str]) -> list[str]:
    if not lines:
        return []
    return [heading, *lines, ""]


def _remote_lines(git_remote: str) -> list[str]:
    if not git_remote:
        return []
    return [f"- `{git_remote}`"]


def _last_commit_line(last_commit: str) -> list[str]:
    if not last_commit:
        return []
    return [f"## Last commit: {last_commit}"]


def _bootstrap_content(cwd: str, slug: str) -> str:
    # Collect information — redact every field before it lands in a vault
    # file that may later be exported or shared (mirrors the secret_redact
    # pass that all capture hooks run).
    timeline = _redacted(_extract_git_timeline(cwd))
    readme_summary = redact_secrets(_extract_readme_summary(cwd))
    tech_stack = _redacted(_extract_tech_stack(cwd))
    docs_structure = _redacted(_extract_docs_structure(cwd))
    git_remote = redact_secrets(_run_git(cwd, "remote", "get-url", "origin"))
    last_commit = redact_secrets(_run_git(cwd, "log", "-1", "--format=%ci"))
    parts = [
        f"# {slug} — Bootstrap Context",
        "",
        f"One-sentence summary: Auto-generated project context for {slug}.",
        "",
        "## Project description",
        readme_summary,
        "",
        *_section("## Tech stack", tech_stack),
        *_section(f"## Recent git history ({len(timeline)} commits)", timeline),
        *_section("## Existing documentation", docs_structure),
        *_section("## Git remote", _remote_lines(git_remote)),
        *_last_commit_line(last_commit),
    ]
    return "\n".join(parts)


def _bootstrap_page(slug: str, content: str) -> str:
    return (
        "---\n"
        f"type: bootstrap-context\ntitle: \"{slug} bootstrap\"\n"
        f"description: \"Auto-generated from git history + README\"\n"
        f"timestamp: {datetime.now().isoformat(timespec='seconds')}\n"
        f"project: {slug}\n"
        "---\n\n"
        f"{content}\n"
    )


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
