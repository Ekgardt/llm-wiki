"""Tests for code_graph.py — tree-sitter code intelligence."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from code_graph import detect_language, find_callers, index_directory, parse_file  # noqa: E402


class TestDetectLanguage:
    """Test language detection from file extension."""

    def test_python(self):
        assert detect_language(Path("test.py")) == "python"

    def test_javascript(self):
        assert detect_language(Path("app.js")) == "javascript"

    def test_typescript(self):
        assert detect_language(Path("app.ts")) == "typescript"

    def test_tsx(self):
        assert detect_language(Path("component.tsx")) == "typescript"

    def test_unknown(self):
        assert detect_language(Path("readme.md")) is None

    def test_case_insensitive(self):
        assert detect_language(Path("Test.PY")) == "python"


class TestParseFile:
    """Test source file parsing."""

    def test_parse_python_file(self, tmp_path):
        """Parse a simple Python file and extract functions."""
        f = tmp_path / "example.py"
        f.write_text(
            "def hello():\n"
            "    print('world')\n\n"
            "def goodbye():\n"
            "    hello()\n",
            encoding="utf-8",
        )
        result = parse_file(f)
        assert result["language"] == "python"
        assert len(result["functions"]) >= 2
        names = [f["name"] for f in result["functions"]]
        assert "hello" in names
        assert "goodbye" in names

    def test_parse_javascript_file(self, tmp_path):
        f = tmp_path / "app.js"
        f.write_text(
            "function greet() { return 'hi'; }\n"
            "const farewell = () => { return 'bye'; }\n",
            encoding="utf-8",
        )
        result = parse_file(f)
        assert result["language"] == "javascript"
        assert len(result["functions"]) >= 1

    def test_parse_typescript_file(self, tmp_path):
        f = tmp_path / "svc.ts"
        f.write_text(
            "class Service {\n"
            "  fetch(): void {}\n"
            "}\n",
            encoding="utf-8",
        )
        result = parse_file(f)
        assert result["language"] == "typescript"

    def test_parse_unknown_file(self, tmp_path):
        f = tmp_path / "readme.md"
        f.write_text("# Hello\n", encoding="utf-8")
        result = parse_file(f)
        assert result["language"] is None
        assert result["functions"] == []

    def test_parse_extracts_line_numbers(self, tmp_path):
        f = tmp_path / "lined.py"
        f.write_text("\n\ndef func():\n    pass\n", encoding="utf-8")
        result = parse_file(f)
        assert len(result["functions"]) >= 1
        assert result["functions"][0]["line"] >= 3


class TestFindCallers:
    """Test caller search."""

    def test_find_callers_finds_function(self, tmp_path):
        """find_callers should locate calls to a function."""
        f1 = tmp_path / "a.py"
        f1.write_text("def target():\n    pass\n", encoding="utf-8")
        f2 = tmp_path / "b.py"
        f2.write_text("target()\n", encoding="utf-8")

        callers = find_callers("target", tmp_path)
        # Should find at least one call in b.py
        assert any("b.py" in c["file"] for c in callers)

    def test_find_callers_empty_result(self, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("def other():\n    pass\n", encoding="utf-8")
        callers = find_callers("nonexistent_func", tmp_path)
        assert callers == []


class TestIndexDirectory:
    """Test directory indexing."""

    def test_index_empty_dir(self, tmp_path):
        stats = index_directory(tmp_path, verbose=False)
        assert stats["files"] == 0

    def test_index_counts_correctly(self, tmp_path):
        (tmp_path / "a.py").write_text("def f(): pass\n", encoding="utf-8")
        (tmp_path / "b.js").write_text("function g() {}\n", encoding="utf-8")
        (tmp_path / "c.md").write_text("# Not code\n", encoding="utf-8")

        stats = index_directory(tmp_path, verbose=False)
        assert stats["files"] == 2  # Only .py and .js
        assert stats["functions"] >= 2

    def test_index_skips_git_and_venv(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "hook.py").write_text("def git_func(): pass\n", encoding="utf-8")
        (tmp_path / "real.py").write_text("def real_func(): pass\n", encoding="utf-8")

        stats = index_directory(tmp_path, verbose=False)
        assert stats["files"] == 1  # Only real.py, not .git/hook.py

    def test_index_returns_all_stats(self, tmp_path):
        (tmp_path / "test.py").write_text("def f(): pass\nclass C: pass\n", encoding="utf-8")
        stats = index_directory(tmp_path, verbose=False)
        assert "files" in stats
        assert "functions" in stats
        assert "classes" in stats
        assert "calls" in stats
        assert "imports" in stats
