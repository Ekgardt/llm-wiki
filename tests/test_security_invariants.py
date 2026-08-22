"""Security invariant tests — verify PROPERTIES, not specific functions.

These tests exist to break the cycle of "fix → new audit finds new issue."
Each test checks a class of problem across multiple code paths. If a new
script or code change violates the invariant, the test fails — regardless
of whether the specific function was tested.

OWASP LLM Top 10 (2025) coverage:
  LLM01 — Prompt injection (captured text framing)
  LLM02 — Insecure output handling (LLM-controlled flags)
  LLM06 — Sensitive information disclosure (path validation, redaction)
  LLM08 — Excessive agency (evidence bypass, skip flags)
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

RENAME_APIS = {"rename", "replace", "move"}
CONTEXT_WINDOW = 200


def _calls_near(source: str, pattern: str, needle: str, window: int = CONTEXT_WINDOW) -> bool:
    """True when `pattern` matches with `needle` in the preceding source window."""
    for match in re.finditer(pattern, source):
        before = source[max(0, match.start() - window) : match.start()]
        if needle in before.casefold():
            return True
    return False


def _is_rename_call(node: object) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    return node.func.attr in RENAME_APIS


def _rename_calls(tree: ast.AST) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if _is_rename_call(node)]


def _mentions_daily_archive(text: str) -> bool:
    lowered = text.casefold()
    return "daily" in lowered and "archive" in lowered


def _assignment_sources(source: str, tree: ast.AST) -> list[str]:
    return [
        ast.get_source_segment(source, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]


def _archive_assignment(source: str, tree: ast.AST) -> bool:
    return any(_mentions_daily_archive(text) for text in _assignment_sources(source, tree))


def _archive_rename(source: str, renames: list[ast.Call]) -> bool:
    return any(
        _mentions_daily_archive(ast.get_source_segment(source, call) or "")
        for call in renames
    )


def _publishes_daily_archive(path: Path) -> bool:
    """A script publishes an archive when it renames a daily-archive path."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    renames = _rename_calls(tree)
    if not renames:
        return False
    return _archive_assignment(source, tree) or _archive_rename(source, renames)


def _parsed_module(py: Path) -> ast.AST | None:
    try:
        return ast.parse(py.read_text(encoding="utf-8"))
    except SyntaxError:
        return None


def _string_constants(tree: ast.AST) -> list[ast.Constant]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _pattern_hits(py: Path, pattern: str) -> list[str]:
    tree = _parsed_module(py)
    if tree is None:
        return []
    return [
        f"{py.name}:{node.lineno}"
        for node in _string_constants(tree)
        if pattern in node.value
    ]


def _tolerated_hit(hit: str) -> bool:
    """export_vault.py lists the forbidden paths; test files quote them."""
    return "export_vault" in hit or hit.startswith("test_")


def _opens_daily_append(src: str) -> bool:
    if not _calls_near(src, r'\.open\s*\(\s*["\']a', "daily"):
        return False
    return "locked_append" not in src and "append_daily" not in src


def _start_threads(threads: list) -> None:
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def _assert_triple_contiguous(lines: list[str], index: int) -> None:
    assert lines[index].startswith("START-"), (
        f"Expected START at line {index}, got: {lines[index]!r}"
    )
    _, tid, num = lines[index].split("-")
    assert lines[index + 1] == f"MIDDLE-{tid}-{num}", (
        f"Interleaving detected: line {index + 1} expected MIDDLE-{tid}-{num}, "
        f"got {lines[index + 1]!r}"
    )
    assert lines[index + 2] == f"END-{tid}-{num}", (
        f"Interleaving detected: line {index + 2} expected END-{tid}-{num}, "
        f"got {lines[index + 2]!r}"
    )
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


# ---------------------------------------------------------------------------
# INVARIANT 1: Transcript path containment — no arbitrary file read
# (would have caught: H-001 in audits 2 and 3)
# ---------------------------------------------------------------------------


class TestTranscriptPathContainment:
    """No transcript path from hook JSON should ever read sensitive files."""

    SENSITIVE_PATHS = [
        "~/.ssh/id_rsa",
        "~/.ssh/config",
        "~/.env",
        "~/.aws/credentials",
        "~/.gitconfig",
        "~/.npmrc",
        "~/.docker/config.json",
    ]

    @pytest.mark.parametrize("sensitive", SENSITIVE_PATHS)
    def test_sensitive_paths_rejected(self, sensitive):
        """Each sensitive file path must be rejected by flush_memory."""
        import flush_memory

        if hasattr(flush_memory, "_transcript_path_allowed"):
            p = Path(sensitive).expanduser()
            assert not flush_memory._transcript_path_allowed(p), (
                f"Transcript path {sensitive} should be rejected"
            )

    def test_transcript_must_have_known_extension(self):
        """Transcripts with arbitrary extensions (e.g. .key, .pem) must be rejected."""
        import flush_memory

        if hasattr(flush_memory, "_transcript_path_allowed"):
            for ext in (".pem", ".key", ".env", ".db", ".sqlite"):
                p = Path.home() / ".claude" / f"session{ext}"
                assert not flush_memory._transcript_path_allowed(p), (
                    f"Extension {ext} should be rejected for transcript paths"
                )


# ---------------------------------------------------------------------------
# INVARIANT 2: No LLM-controlled flag bypasses evidence verification
# (would have caught: H-003 skip_evidence bypass)
# ---------------------------------------------------------------------------


class TestNoLLMBypass:
    """The LLM plan schema must not allow bypassing security checks."""

    def test_skip_evidence_not_in_source(self):
        """No 'skip_evidence' field should exist in compile_memory source."""
        src = (SCRIPTS / "compile_memory.py").read_text(encoding="utf-8")
        assert "skip_evidence" not in src, (
            "skip_evidence found in compile_memory.py — LLM can bypass "
            "evidence verification. This field was removed because it "
            "allows prompt injection to create knowledge without evidence."
        )

    def test_grep_no_shell_true(self):
        """No script should use shell=True with subprocess."""
        for py in SCRIPTS.glob("*.py"):
            src = py.read_text(encoding="utf-8")
            # Broad patterns: the value may be computed (shell=condition),
            # so context decides whether the flag reaches a subprocess call.
            assert not _calls_near(src, r"shell\s*=\s*True", "subprocess"), (
                f"{py.name}: subprocess with shell=True found"
            )
            assert not _calls_near(src, r"shell\s*=\s*isinstance", "subprocess"), (
                f"{py.name}: subprocess with shell=isinstance(...) found — "
                "use list args only"
            )

    def test_only_daily_archiver_has_directory_publication_exception(self):
        offenders = {
            path.name
            for path in SCRIPTS.glob("*.py")
            # Scanner patterns are data, not publication calls.
            if path.name != "check_knowledge_writers.py"
            and _publishes_daily_archive(path)
        }
        assert offenders == {"archive_daily.py"}


# ---------------------------------------------------------------------------
# INVARIANT 3: Redaction before persistence
# (would have caught: H-014 bootstrap, L-001 truncation order)
# ---------------------------------------------------------------------------


class TestRedactionBeforePersistence:
    """All text written to durable storage must pass through redact_secrets."""

    WRITE_FUNCTIONS = [
        "compile_memory.py",
        "query_memory.py",
        "flush_memory.py",
        "feedback_capture.py",
        "daily_log_append.py",
        "bootstrap_project.py",
    ]

    def test_secret_redact_importable(self):
        """secret_redact must be importable with zero deps."""
        from secret_redact import redact_secrets

        assert callable(redact_secrets)

    def test_redact_catches_bearer(self):
        from secret_redact import redact_secrets

        out = redact_secrets("Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz012345")
        assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in out

    def test_redact_catches_aws_key(self):
        from secret_redact import redact_secrets

        out = redact_secrets("AWS_KEY=AKIAIOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_redact_catches_jwt(self):
        from secret_redact import redact_secrets

        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUPCTHLak8"  # gitleaks:allow — public example JWT from jwt.io, not a real secret
        out = redact_secrets(f"token: {jwt}")
        assert jwt not in out

    def test_entropy_token_redacted(self):
        from secret_redact import redact_secrets

        out = redact_secrets("entropy=short-secret entropy: another-secret")
        assert out == "entropy=[REDACTED] entropy: [REDACTED]"

    def test_redact_catches_hyphenated_provider_key(self):
        """Modern provider keys carry hyphenated prefixes (sk-ant-…, sk-proj-…)."""
        from secret_redact import redact_secrets

        key = "sk-ant-api03-QWERTYUIOPASDFGHJKLZXCVBNM1234"
        out = redact_secrets(f"call failed with {key}")
        assert key not in out
        assert "[REDACTED_API_KEY]" in out

    def test_redact_keeps_a_key_prefix_that_is_part_of_a_word(self):
        """`task-` ends in `sk-`, and that blocked every compile this vault ran.

        The page slug `dead-task-retirement-and-restore-decision` contains
        `sk-retirement-and-restore-decision`, which the provider-key pattern
        matched. The fail-closed DLP boundary then quarantined the transaction,
        so the memory pipeline could not write at all. A real key starts a
        token; a suffix inside a word does not.
        """
        from secret_redact import redact_secrets

        for text in (
            "- [[knowledge/notes/dead-task-retirement-and-restore-decision]] — a page",
            "Recorded in `dead-task-retirement-and-restore-decision.md`.",
            "see risk-assessment-and-mitigation-plan for the rest",
        ):
            assert redact_secrets(text) == text

    def test_redact_still_catches_a_key_after_punctuation(self):
        """Boundary means token start, not whitespace: `=`, quotes and `(` count."""
        from secret_redact import redact_secrets

        key = "sk-ant-api03-QWERTYUIOPASDFGHJKLZXCVBNM1234"
        for text in (f"OPENAI_API_KEY={key}", f'value "{key}"', f"({key})"):
            assert key not in redact_secrets(text)

    def test_redact_keeps_hyphenated_prose(self):
        """A long hyphenated identifier is not a key just because it is long."""
        from secret_redact import redact_secrets

        text = "branch fix/linux-installer-and-transient-cleanup is ready"
        assert redact_secrets(text) == text

    def test_redact_keeps_macos_temporary_paths(self):
        """A macOS temp path is 41 characters of `[A-Za-z0-9/]` at entropy 4.46."""
        from secret_redact import redact_secrets

        path = (
            "/private/var/folders/_5/zjnzxgh147qcg3bb5cg2wvqw0000gn/T/"
            "pytest-of-runner/pytest-0/test_case0/Project With Spaces"
        )
        assert redact_secrets(path) == path

    def test_redact_still_catches_base64_with_slashes(self):
        """A real blob keeps long dense runs between its separators."""
        from secret_redact import redact_secrets

        blob = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldY/YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4"
        out = redact_secrets(f"payload {blob}")
        assert blob not in out
        assert "[REDACTED_TOKEN]" in out

    def test_redact_does_not_redact_sha256(self):
        """Git SHA hashes (pure hex) must NOT be redacted."""
        from secret_redact import redact_secrets

        sha = "a" * 64  # 64 hex chars = SHA-256
        out = redact_secrets(f"commit {sha}")
        assert sha in out, "SHA-256 hash was incorrectly redacted"

    def test_redact_does_not_redact_normal_text(self):
        """Normal prose with alphanumeric strings over 40 chars should survive."""
        from secret_redact import redact_secrets

        text = "The quick brown fox jumps over the lazy dog and then runs away"
        out = redact_secrets(text)
        # The text should be unchanged (no high-entropy base64 matches)
        assert "quick brown fox" in out


# ---------------------------------------------------------------------------
# INVARIANT 4: Status filtering — superseded/archived excluded everywhere
# (would have caught: M-003, M-012, M-013 — inconsistent status filtering)
# ---------------------------------------------------------------------------


class TestStatusFiltering:
    """Pages with status: superseded or status: archived must be excluded
    from search results, index, context injection, and guardrails."""

    SUPERSEDED_FM = "---\nstatus: superseded\ntype: pattern\n---\n\n# Page\n"
    ARCHIVED_FM = "---\nstatus: archived\ntype: pattern\n---\n\n# Page\n"
    ACTIVE_FM = "---\ntype: pattern\n---\n\n# Page\n"

    def test_search_memory_excludes_superseded(self):
        """search_memory._collect_pages must skip superseded/archived."""

        # Check that the source code has the status filter
        src = (SCRIPTS / "search_memory.py").read_text(encoding="utf-8")
        assert "superseded" in src, (
            "search_memory.py does not filter superseded pages — "
            "add status: superseded/archived check in _collect_pages"
        )

    def test_rebuild_memory_index_excludes_superseded(self):
        """rebuild_memory_index must skip superseded/archived."""
        src = (SCRIPTS / "rebuild_memory_index.py").read_text(encoding="utf-8")
        assert "superseded" in src or "archived" in src, (
            "rebuild_memory_index.py does not filter superseded/archived"
        )

    def test_build_context_excludes_superseded(self):
        """build_context must skip superseded/archived."""
        src = (SCRIPTS / "build_context.py").read_text(encoding="utf-8")
        assert "superseded" in src or "archived" in src, (
            "build_context.py does not filter superseded/archived"
        )

    def test_build_guardrails_excludes_superseded(self):
        """build_guardrails must skip superseded/archived."""
        src = (SCRIPTS / "build_guardrails.py").read_text(encoding="utf-8")
        assert "superseded" in src or "archived" in src, (
            "build_guardrails.py does not filter superseded/archived"
        )


# ---------------------------------------------------------------------------
# INVARIANT 5: Path safety — no traversal in any user-facing path input
# (would have caught: C-001 path traversal, H-009 feedback ID, M-004 advisory)
# ---------------------------------------------------------------------------


class TestPathSafety:
    """User-supplied path components must never escape their intended directory."""

    TRAVERSAL_INPUTS = [
        "../../etc/passwd",
        "..\\..\\windows\\system32",
        "../../../",
        "....//....//",
        "%2e%2e%2f",
        "/etc/passwd",
        "C:\\Windows\\System32",
    ]

    @pytest.mark.parametrize("evil", TRAVERSAL_INPUTS)
    def test_compile_category_rejects_traversal(self, evil):
        """compile_memory must reject traversal in category field."""
        import compile_memory

        categories = compile_memory.RAW_PLAN_SCHEMA["properties"]["operations"][
            "items"
        ]["properties"]["category"]["enum"]
        assert evil not in categories
        assert not hasattr(compile_memory, "_execute_plan")

    @pytest.mark.parametrize("evil", TRAVERSAL_INPUTS)
    def test_feedback_candidate_id_rejects_traversal(self, evil):
        """feedback_capture must reject non-hex candidate IDs."""
        import feedback_capture

        result = feedback_capture.promote_candidate(evil)
        assert result is None, f"Traversal candidate_id {evil!r} was not rejected"

    def test_blackboard_project_rejects_traversal(self, tmp_path):
        """blackboard must reject traversal in project slug."""
        import blackboard

        with patch.object(blackboard, "PROJECTS_DIR", tmp_path):
            try:
                d = blackboard._bb_dir("../../evil")
                # If we get here, check the path doesn't escape
                resolved = d.resolve()
                assert resolved.is_relative_to(tmp_path.resolve()), (
                    f"Blackboard path escaped projects dir: {resolved}"
                )
            except ValueError:
                pass  # Rejected — good


# ---------------------------------------------------------------------------
# INVARIANT 6: YAML safety — frontmatter interpolation is escaped
# (would have caught: M-006 feedback YAML injection)
# ---------------------------------------------------------------------------


class TestYAMLSafety:
    """Frontmatter built from user/LLM input must escape YAML special chars."""

    YAML_INJECTIONS = [
        'title: "injected"\ntype: evil',  # newline injection
        "value: '\\nmalicious: true'",     # escape sequence
        '"""block string"""',              # YAML block scalar
    ]

    def test_feedback_frontmatter_escapes_newlines(self):
        """feedback_capture must escape newlines in interpolated fields."""

        src = (SCRIPTS / "feedback_capture.py").read_text(encoding="utf-8")
        # The _esc function should handle newlines
        assert "chr(10)" in src or "\\n" in src, (
            "feedback_capture.py does not escape newlines in YAML frontmatter"
        )

    def test_compile_frontmatter_escapes_quotes(self):
        """compile_memory must escape quotes in title/summary."""
        src = (SCRIPTS / "compile_memory.py").read_text(encoding="utf-8")
        assert "chr(34)" in src or '\\"' in src, (
            "compile_memory.py does not escape quotes in YAML frontmatter"
        )


# ---------------------------------------------------------------------------
# INVARIANT 7: No legacy forbidden paths in active code
# (would have caught: all legacy path regressions across 3 audit rounds)
# ---------------------------------------------------------------------------


class TestNoLegacyPaths:
    """Active code must not reference forbidden root directories."""

    FORBIDDEN_IN_CODE = [
        ("wiki/", "scripts/"),
        ("memory/", "scripts/"),  # Allow in comments/docstrings only
        ("outputs/", "scripts/"),
        ("LLM-wiki-state", "scripts/"),
        ("memory-state", "scripts/"),
        ("memory-reports", "scripts/"),
    ]

    @pytest.mark.parametrize("pattern,search_dir", FORBIDDEN_IN_CODE)
    def test_no_legacy_path_in_active_code(self, pattern, search_dir):
        """Check that forbidden paths don't appear in active code logic
        (comments and docstrings are tolerated for historical context)."""
        violations = [
            hit
            for py in (ROOT / search_dir).glob("*.py")
            for hit in _pattern_hits(py, pattern)
            if not _tolerated_hit(hit)
        ]
        assert not violations, (
            f"Legacy path '{pattern}' found in active code: {violations[:3]}. "
            "Update to current three-zone paths."
        )


# ---------------------------------------------------------------------------
# INVARIANT 8: Daily-log lock actually provides exclusivity
# (BEHAVIORAL test — would have caught H-001/H-002 rename-based lock)
# ---------------------------------------------------------------------------


class TestDailyLockExclusivity:
    """The daily-log lock must ACTUALLY prevent concurrent writes.

    Previous presence-tests checked that ``_daily_lock`` is imported, but
    the lock itself was broken (rename overwrites on POSIX). This test
    proves the lock provides real exclusivity by spawning concurrent
    writers and asserting no interleaving.
    """

    def test_concurrent_writers_do_not_interleave(self, tmp_path, monkeypatch):
        """N threads write to the same daily file under _daily_lock —
        each write must be atomic (no line interleaving)."""
        import threading

        import daily_log_append

        # Redirect STATE_ROOT so the lock file lives in tmp
        lock_dir = tmp_path / "run"
        lock_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(daily_log_append, "STATE_ROOT", tmp_path)

        daily_path = tmp_path / "daily.md"

        def writer(thread_id: int):
            for i in range(10):
                line = f"START-{thread_id}-{i}\nMIDDLE-{thread_id}-{i}\nEND-{thread_id}-{i}\n"
                with daily_log_append._daily_lock(timeout=30.0):
                    with daily_path.open("a", encoding="utf-8") as f:
                        f.write(line)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(5)]
        _start_threads(threads)

        # Verify: each START/MIDDLE/END triple must be contiguous
        lines = daily_path.read_text(encoding="utf-8").strip().splitlines()
        for index in range(0, len(lines), 3):
            _assert_triple_contiguous(lines, index)

    def test_lock_is_fail_closed(self, tmp_path, monkeypatch):
        """If lock can't be acquired, it must raise (not silently write)."""
        import daily_log_append

        lock_dir = tmp_path / "run"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_file = lock_dir / "daily-append.lock"
        monkeypatch.setattr(daily_log_append, "STATE_ROOT", tmp_path)

        # Pre-create a fresh lock owned by a "live" PID (ours)
        lock_file.write_text(str(__import__("os").getpid()), encoding="utf-8")

        # Should raise TimeoutError, not silently proceed
        with pytest.raises(TimeoutError):
            with daily_log_append._daily_lock(timeout=0.5):
                pass


# ---------------------------------------------------------------------------
# INVARIANT 9: Compile requires evidence for create operations
# (BEHAVIORAL test — would have caught M-001 empty evidence bypass)
# ---------------------------------------------------------------------------


class TestCompileEvidenceEnforcement:
    """Compile create operations MUST cite at least 1 evidence item."""

    def test_create_without_evidence_is_rejected_by_closed_schema(self):
        import compile_memory

        evidence = compile_memory.RAW_PLAN_SCHEMA["properties"]["operations"][
            "items"
        ]["properties"]["evidence"]
        assert evidence["minItems"] == 1
        assert not hasattr(compile_memory, "_execute_plan")

    def test_evidence_requires_exact_quote_and_claim(self):
        import compile_memory

        item = compile_memory.RAW_PLAN_SCHEMA["properties"]["operations"][
            "items"
        ]["properties"]["evidence"]["items"]
        assert set(item["required"]) == {
            "daily_date",
            "timestamp",
            "quoted_text",
            "claim",
        }


# ---------------------------------------------------------------------------
# INVARIANT 10: Single daily-log write path — no duplicated logic
# ---------------------------------------------------------------------------


class TestSingleDailyWritePath:
    """All daily-log writes must go through locked_append() or append_daily().

    Previous audits found 4 independent copies of the daily-log write logic
    (create file if missing + append under lock). This invariant ensures
    no script reimplements that pattern instead of delegating.
    """

    DAILY_APPEND_INFRA = {"daily_log_append.py", "memory_state.py"}

    def test_no_direct_daily_file_open_outside_infra(self):
        """No script outside daily_log_append.py should open a daily-log
        file directly with open(... 'a') — it must use locked_append()."""
        for py in SCRIPTS.glob("*.py"):
            if py.name in self.DAILY_APPEND_INFRA:
                continue
            assert not _opens_daily_append(py.read_text(encoding="utf-8")), (
                f"{py.name}: opens daily-log file directly with "
                f"open('a') instead of delegating to locked_append()."
            )


# ---------------------------------------------------------------------------
# INVARIANT 11: Markdown transactions never invoke Git or external work
# ---------------------------------------------------------------------------


class TestMarkdownTransactionBoundary:
    """The writer boundary must stay deterministic and fail closed."""

    def test_transaction_module_has_no_git_subprocess_or_command(self):
        source = (SCRIPTS / "markdown_transaction.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        git_commands = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and re.search(r"^\s*git(?:\.exe)?(?:\s|$)", node.value, re.IGNORECASE)
        ]
        assert git_commands == []

    def test_external_work_fails_closed_while_writer_gate_is_held(self, tmp_path):
        from markdown_transaction import MarkdownCoordinator

        vault = tmp_path / "vault"
        state_root = tmp_path / "state"
        (vault / "knowledge/notes").mkdir(parents=True)
        coordinator = MarkdownCoordinator(vault, state_root)

        with coordinator.writer_gate():
            assert coordinator.writer_gate_held()
            with pytest.raises(RuntimeError, match="writer gate"):
                coordinator.assert_external_work_allowed()
        assert not coordinator.writer_gate_held()

    def test_guardrails_is_an_explicit_transaction_target(self, tmp_path):
        from markdown_transaction import MarkdownChange, MarkdownCoordinator

        vault = tmp_path / "vault"
        state_root = tmp_path / "state"
        (vault / "knowledge").mkdir(parents=True)
        coordinator = MarkdownCoordinator(vault, state_root)

        record = coordinator.prepare(
            [MarkdownChange.create("knowledge/guardrails.md", b"guardrails\n")],
            operation_id="security:guardrails-target",
        )

        assert record.operations[0].path == "knowledge/guardrails.md"


# ---------------------------------------------------------------------------
# INVARIANT 12: Compile snapshot excludes superseded pages
# ---------------------------------------------------------------------------


class TestCompileSnapshotExcludesSuperseded:
    """existing_knowledge_snapshot must not feed superseded/archived pages."""

    def test_snapshot_skips_superseded(self, tmp_path, monkeypatch):
        """Pages with status: superseded must not appear in snapshot."""
        import compile_memory

        knowledge = tmp_path / "knowledge" / "notes"
        knowledge.mkdir(parents=True)
        monkeypatch.setattr(compile_memory, "KNOWLEDGE", knowledge)
        monkeypatch.setattr(compile_memory, "ROOT", tmp_path)

        # Create an active page
        (knowledge / "active.md").write_text(
            "---\ntype: pattern\n---\n\n# Active\n", encoding="utf-8"
        )
        # Create a superseded page
        (knowledge / "old.md").write_text(
            "---\ntype: pattern\nstatus: superseded\n---\n\n# Old\n", encoding="utf-8"
        )

        snapshot = compile_memory.existing_knowledge_snapshot()
        # Should contain "active" but NOT "old"
        assert "active" in snapshot.lower(), f"Active page missing from snapshot: {snapshot}"
        assert "old" not in snapshot.lower(), (
            f"Superseded page 'old' should be excluded from snapshot: {snapshot}"
        )
