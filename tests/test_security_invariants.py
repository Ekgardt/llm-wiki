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

import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
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
            # Broad regex: catches shell=True anywhere in a subprocess call
            # even if the value is computed (shell=condition)
            for match in re.finditer(r"shell\s*=\s*True", src):
                # Check context: is it inside a subprocess call?
                before = src[max(0, match.start() - 200) : match.start()]
                if "subprocess" in before:
                    pytest.fail(
                        f"{py.name}: subprocess with shell=True found"
                    )
            # Also check for shell=<variable> pattern
            for match in re.finditer(r"shell\s*=\s*isinstance", src):
                before = src[max(0, match.start() - 200) : match.start()]
                if "subprocess" in before:
                    pytest.fail(
                        f"{py.name}: subprocess with shell=isinstance(...) found — "
                        "use list args only"
                    )


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

        plan = {
            "operations": [{
                "action": "create",
                "category": evil,
                "slug": "test",
                "title": "T",
                "summary": "s",
                "body_markdown": "b",
                "evidence": [],
            }],
            "audit": {},
        }
        touched, audit = compile_memory._execute_plan(plan, [], dry_run=True)
        assert touched == [], f"Traversal input {evil!r} was not rejected"

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
        import ast

        violations = []
        for py in (ROOT / search_dir).glob("*.py"):
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if pattern in node.value:
                        # Check if it's in a comment-like context (docstring)
                        violations.append(f"{py.name}:{node.lineno}")
        # Filter: allow in export_vault.py FORBIDDEN list
        violations = [v for v in violations if "export_vault" not in v]
        # Filter: allow in test files
        violations = [v for v in violations if not v.startswith("test_")]
        if violations:
            pytest.fail(
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
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Verify: each START/MIDDLE/END triple must be contiguous
        content = daily_path.read_text(encoding="utf-8")
        lines = content.strip().splitlines()
        i = 0
        while i < len(lines):
            if not lines[i].startswith("START-"):
                pytest.fail(f"Expected START at line {i}, got: {lines[i]!r}")
            # Next line must be the matching MIDDLE
            parts = lines[i].split("-")
            tid, num = parts[1], parts[2]
            assert lines[i + 1] == f"MIDDLE-{tid}-{num}", (
                f"Interleaving detected: line {i+1} expected MIDDLE-{tid}-{num}, "
                f"got {lines[i+1]!r}"
            )
            assert lines[i + 2] == f"END-{tid}-{num}", (
                f"Interleaving detected: line {i+2} expected END-{tid}-{num}, "
                f"got {lines[i+2]!r}"
            )
            i += 3

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

    def test_create_without_evidence_is_dropped(self, tmp_path, monkeypatch):
        """A create operation with empty evidence must be dropped."""
        import compile_memory

        knowledge = tmp_path / "knowledge" / "notes"
        knowledge.mkdir(parents=True)
        monkeypatch.setattr(compile_memory, "KNOWLEDGE", knowledge)
        monkeypatch.setattr(compile_memory, "ROOT", tmp_path)

        plan = {
            "operations": [
                {
                    "action": "create",
                    "category": "patterns",
                    "slug": "no-evidence-test",
                    "title": "Test",
                    "summary": "s",
                    "body_markdown": "b",
                    "evidence": [],  # EMPTY evidence
                }
            ],
            "audit": {},
        }
        touched, audit = compile_memory._execute_plan(plan, [], dry_run=False)
        assert touched == [], "Create with empty evidence should be dropped"
        assert "no evidence" in audit.lower(), (
            f"Audit should explain WHY the op was dropped: {audit}"
        )

    def test_create_with_valid_evidence_passes(self, tmp_path, monkeypatch):
        """A create operation with verified evidence should proceed."""
        import compile_memory

        knowledge = tmp_path / "knowledge" / "notes"
        knowledge.mkdir(parents=True)
        monkeypatch.setattr(compile_memory, "KNOWLEDGE", knowledge)
        monkeypatch.setattr(compile_memory, "ROOT", tmp_path)
        # Mock evidence verification to return 1 verified, 0 failed
        monkeypatch.setattr(compile_memory, "_verify_evidence", lambda ev, dp: (1, 0))

        plan = {
            "operations": [
                {
                    "action": "create",
                    "category": "patterns",
                    "slug": "with-evidence-test",
                    "title": "Test",
                    "summary": "s",
                    "body_markdown": "b",
                    "evidence": [
                        {
                            "daily_date": "2026-07-11",
                            "timestamp": "10:00:00",
                            "claim": "test claim",
                        }
                    ],
                }
            ],
            "audit": {},
        }
        touched, audit = compile_memory._execute_plan(plan, [], dry_run=False)
        assert len(touched) == 1, (
            f"Create with valid evidence should succeed. "
            f"touched={touched}, audit={audit}"
        )


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
        for py in (SCRIPTS).glob("*.py"):
            if py.name in self.DAILY_APPEND_INFRA:
                continue
            src = py.read_text(encoding="utf-8")
            # Look for pattern: path.open("a" or "a") on a daily-log-like path
            for m in re.finditer(r'\.open\s*\(\s*["\']a', src):
                # Check context: is this a daily-log write?
                before = src[max(0, m.start() - 200):m.start()]
                if "daily" in before.lower():
                    has_delegate = "locked_append" in src or "append_daily" in src
                    if not has_delegate:
                        pytest.fail(
                            f"{py.name}: opens daily-log file directly with "
                            f"open('a') instead of delegating to locked_append()."
                        )


# ---------------------------------------------------------------------------
# INVARIANT 11: Compile snapshot excludes superseded pages
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
