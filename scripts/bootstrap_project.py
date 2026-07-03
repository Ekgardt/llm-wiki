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
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import ROOT  # noqa: E402

PROJECTS_DIR = ROOT / "wiki" / "projects"
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
    for name in ("README.md", "README.rst", "README.txt", "README", "readme.md"):
        p = Path(cwd) / name
        if p.exists():
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
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
        if (Path(cwd) / marker).exists():
            stack.append(f"- {tech} (`{marker}`)")
    # Detect frameworks from package.json
    pkg = Path(cwd) / "package.json"
    if pkg.exists():
        try:
            import json
            data = json.loads(pkg.read_text(encoding="utf-8"))
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
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

    # Collect information
    timeline = _extract_git_timeline(cwd)
    readme_summary = _extract_readme_summary(cwd)
    tech_stack = _extract_tech_stack(cwd)
    docs_structure = _extract_docs_structure(cwd)
    git_remote = _run_git(cwd, "remote", "get-url", "origin")
    last_commit = _run_git(cwd, "log", "-1", "--format=%ci")

    # Build the bootstrap page
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

    if docs_structure:
        parts.append("## Existing documentation")
        parts.extend(docs_structure)
        parts.append("")

    if git_remote:
        parts.append(f"## Git remote")
        parts.append(f"- `{git_remote}`")
        parts.append("")

    if last_commit:
        parts.append(f"## Last commit: {last_commit}")

    content = "\n".join(parts)

    if apply:
        project_dir.mkdir(parents=True, exist_ok=True)
        bootstrap_path = project_dir / "bootstrap.md"
        bootstrap_path.write_text(
            "---\n"
            f"type: bootstrap-context\ntitle: \"{slug} bootstrap\"\n"
            f"description: \"Auto-generated from git history + README\"\n"
            f"timestamp: {datetime.now().isoformat(timespec='seconds')}\n"
            f"project: {slug}\n"
            "---\n\n"
            f"{content}\n",
            encoding="utf-8",
        )
        return f"Written: {bootstrap_path.relative_to(ROOT)}"
    else:
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
