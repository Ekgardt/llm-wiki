from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _transaction_db(state_root: Path, now: datetime) -> Path:
    database = state_root / "run/markdown-transactions.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE "transaction" (
                id TEXT PRIMARY KEY, operation_id TEXT, request_hash TEXT,
                state TEXT, preconditions_json TEXT, plan_hash TEXT,
                created_at TEXT, updated_at TEXT,
                artifacts_pruned_at TEXT
            );
            CREATE TABLE "operation" (
                transaction_id TEXT, position INTEGER, kind TEXT, path TEXT,
                before_hash TEXT, after_hash TEXT, parent_device INTEGER,
                parent_inode INTEGER, applied INTEGER
            );
            CREATE TABLE project_leases (
                project TEXT PRIMARY KEY, expires_at TEXT
            );
            CREATE TABLE writer_owners (
                gate_name TEXT PRIMARY KEY, process_id INTEGER, expires_at TEXT
            );
            CREATE TABLE maintenance_owners (
                owner_name TEXT PRIMARY KEY, process_id INTEGER, expires_at TEXT
            );
            """
        )
        connection.executemany(
            'INSERT INTO "transaction" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            [
                (name, f"operation-{name}", "a" * 64, state, "{}", "b" * 64,
                 now.isoformat(), now.isoformat(), None)
                for name, state in (
                    ("tx-active", "applying"),
                    ("tx-conflict", "conflicted"),
                    ("tx-quarantine", "quarantined"),
                    ("tx-undo", "committed"),
                )
            ],
        )
        connection.executemany(
            'INSERT INTO "operation" VALUES (?, 0, "create", ?, "absent", ?, 1, 2, 1)',
            [
                (name, f"knowledge/notes/{name}.md", "c" * 64)
                for name in ("tx-active", "tx-conflict", "tx-quarantine", "tx-undo")
            ],
        )
        future = (now + timedelta(minutes=5)).isoformat()
        connection.execute("INSERT INTO project_leases VALUES ('p', ?)", (future,))
        connection.execute("INSERT INTO writer_owners VALUES ('global', 1, ?)", (future,))
        connection.execute("INSERT INTO maintenance_owners VALUES ('doctor', 1, ?)", (future,))
    for name in ("tx-active", "tx-conflict", "tx-quarantine", "tx-undo"):
        (state_root / "run/transactions" / name).mkdir(parents=True, exist_ok=True)
    return database


def _queue_db(state_root: Path, now: datetime) -> Path:
    database = state_root / "run/queue.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, state TEXT, error_code TEXT,
                blocked_capability TEXT, result_reference TEXT
            );
            CREATE TABLE queue_ownership (
                role TEXT PRIMARY KEY, token TEXT, pid INTEGER, expires_at TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO tasks VALUES "
            "('task', 'dead', 'attempts_exhausted', NULL, NULL)"
        )
        connection.execute(
            "INSERT INTO queue_ownership VALUES ('worker', 'token', 1, ?)",
            ((now + timedelta(minutes=5)).isoformat(),),
        )
    results = state_root / "run/queue-results"
    results.mkdir()
    (results / "orphan.result").write_text("retained", encoding="utf-8")
    return database


def test_run_deletion_reports_every_contract_blocker(tmp_path, monkeypatch):
    import doctor

    now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
    state_root = tmp_path / "state"
    _transaction_db(state_root, now)
    _queue_db(state_root, now)
    monkeypatch.setattr(doctor, "_pid_alive", lambda pid: True)

    result = doctor._run_deletion_check(state_root, now)

    assert {item["code"] for item in result["blockers"]} == {
        "transaction_nonterminal",
        "transaction_conflicted",
        "transaction_quarantined",
        "transaction_undo_retained",
        "queue_task_retained",
        "queue_result_retained",
        "project_lease_live",
        "writer_live",
        "queue_worker_live",
        "maintenance_owner_live",
    }
    assert result["allowed"] is False
    assert "tx-active" not in json.dumps(result)


def test_run_deletion_is_allowed_only_when_no_blocker_exists(tmp_path):
    import doctor

    state_root = tmp_path / "state"
    (state_root / "run").mkdir(parents=True)

    assert doctor._run_deletion_check(
        state_root, datetime.now(timezone.utc)
    ) == {"allowed": True, "blockers": []}


def _python_direct_runtime_root_deletions(source: str) -> list[int]:
    """Find direct runtime-root deletes in explicitly supported AST forms.

    Paths resolve through string literals, Path constructors, ``/``, ``joinpath``,
    ``os.path.join``, ``.parent``, and straight-line name aliases. This guard does
    not infer function returns, containers, attributes, or arbitrary control flow.
    """

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.modules: list[dict[str, str]] = [{}]
            self.functions: list[dict[str, str]] = [{}]
            self.paths: list[dict[str, tuple[str, ...]]] = [{}]
            self.path_constructors: list[set[str]] = [{"Path", "PurePath"}]
            self.lines: list[int] = []

        def _push_scope(self) -> None:
            self.modules.append(dict(self.modules[-1]))
            self.functions.append(dict(self.functions[-1]))
            self.paths.append(dict(self.paths[-1]))
            self.path_constructors.append(set(self.path_constructors[-1]))

        def _pop_scope(self) -> None:
            self.modules.pop()
            self.functions.pop()
            self.paths.pop()
            self.path_constructors.pop()

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self._push_scope()
            for statement in node.body:
                self.visit(statement)
            self._pop_scope()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function(node)

        def visit_Import(self, node: ast.Import) -> None:
            for name in node.names:
                if name.name in {"os", "pathlib", "shutil"}:
                    self.modules[-1][name.asname or name.name] = name.name

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.module in {"os", "shutil"}:
                for name in node.names:
                    if name.name in {"remove", "rmdir", "rmtree", "unlink"}:
                        alias = name.asname or name.name
                        self.functions[-1][alias] = f"{node.module}.{name.name}"
            if node.module == "pathlib":
                for name in node.names:
                    if name.name in {"Path", "PurePath"}:
                        self.path_constructors[-1].add(name.asname or name.name)

        def _is_path_type(self, node: ast.AST) -> bool:
            return (
                isinstance(node, ast.Name)
                and node.id in self.path_constructors[-1]
            ) or (
                isinstance(node, ast.Attribute)
                and node.attr in {"Path", "PurePath"}
                and isinstance(node.value, ast.Name)
                and self.modules[-1].get(node.value.id) == "pathlib"
            )

        def _delete_api(self, node: ast.AST) -> str | None:
            if isinstance(node, ast.Name):
                return self.functions[-1].get(node.id)
            if not isinstance(node, ast.Attribute):
                return None
            if self._is_path_type(node.value) and node.attr in {"rmdir", "unlink"}:
                return f"pathlib.{node.attr}"
            if isinstance(node.value, ast.Name):
                module = self.modules[-1].get(node.value.id)
                if module == "os" and node.attr in {"remove", "rmdir", "unlink"}:
                    return f"os.{node.attr}"
                if module == "shutil" and node.attr == "rmtree":
                    return "shutil.rmtree"
            return None

        def _path_parts(self, node: ast.AST) -> tuple[str, ...] | None:
            if isinstance(node, ast.Name):
                return self.paths[-1].get(node.id, (node.id.casefold(),))
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return tuple(
                    part.casefold()
                    for part in node.value.replace("\\", "/").split("/")
                    if part and part != "."
                )
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                left = self._path_parts(node.left)
                right = self._path_parts(node.right)
                return None if left is None or right is None else left + right
            if isinstance(node, ast.Attribute) and node.attr == "parent":
                value = self._path_parts(node.value)
                return value[:-1] if value else None
            if not isinstance(node, ast.Call):
                return None
            if isinstance(node.func, ast.Attribute) and node.func.attr == "joinpath":
                result = self._path_parts(node.func.value)
                for argument in node.args:
                    addition = self._path_parts(argument)
                    if result is None or addition is None:
                        return None
                    result += addition
                return result
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "join"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "path"
                and isinstance(node.func.value.value, ast.Name)
                and self.modules[-1].get(node.func.value.value.id) == "os"
            ):
                result: tuple[str, ...] = ()
                for argument in node.args:
                    addition = self._path_parts(argument)
                    if addition is None:
                        return None
                    result += addition
                return result
            if self._is_path_type(node.func) and node.args:
                return self._path_parts(node.args[0])
            return None

        def _bind(self, target: ast.AST, value: ast.AST) -> None:
            if not isinstance(target, ast.Name):
                return
            parts = self._path_parts(value)
            if parts is None:
                self.paths[-1].pop(target.id, None)
            else:
                self.paths[-1][target.id] = parts
            api = self._delete_api(value)
            if api is None:
                self.functions[-1].pop(target.id, None)
            else:
                self.functions[-1][target.id] = api

        def visit_Assign(self, node: ast.Assign) -> None:
            self.visit(node.value)
            for target in node.targets:
                self._bind(target, node.value)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if node.value is not None:
                self.visit(node.value)
                self._bind(node.target, node.value)

        @staticmethod
        def _is_runtime_root(parts: tuple[str, ...] | None) -> bool:
            if not parts:
                return False
            return parts[-1:] == ("run",) or parts[-2:] == ("run", "lsp")

        def visit_Call(self, node: ast.Call) -> None:
            target: ast.AST | None = None
            api = self._delete_api(node.func)
            if api is not None:
                if node.args:
                    target = node.args[0]
                else:
                    keyword_names = {"path", "self"} if api.startswith("pathlib.") else {"path"}
                    target = next(
                        (
                            keyword.value
                            for keyword in node.keywords
                            if keyword.arg in keyword_names
                        ),
                        None,
                    )
            elif isinstance(node.func, ast.Attribute) and node.func.attr in {
                "rmdir",
                "unlink",
            }:
                target = node.func.value
            if target is not None and self._is_runtime_root(self._path_parts(target)):
                self.lines.append(node.lineno)
            self.generic_visit(node)

    visitor = Visitor()
    visitor.visit(ast.parse(source))
    return sorted(set(visitor.lines))


def _installer_direct_runtime_root_deletions(source: str) -> list[int]:
    """Find direct installer deletes in a deliberately small source subset.

    The guard joins ``\\``/backtick continuations and resolves simple assignments
    in source order. It does not evaluate scopes, branches, command substitutions,
    quoting semantics, or arbitrary shell/PowerShell execution.
    """
    variable_reference = re.compile(
        r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|"
        r"(?P<plain>[A-Za-z_][A-Za-z0-9_]*))"
    )
    shell_assignment = re.compile(
        r"^\s*(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*?)\s*;?\s*$"
    )
    powershell_assignment = re.compile(
        r"^\s*\$(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*?)\s*;?\s*$"
    )
    delete_command = re.compile(
        r"^\s*(?:&\s*)?(?:(?:sudo|command)\s+)?"
        r"(?P<command>remove-item|rm|rmdir|del)\b(?P<arguments>.*)$",
        re.IGNORECASE,
    )
    variables: dict[str, str] = {}

    def expand_variables(value: str) -> str:
        def replace(match: re.Match[str]) -> str:
            name = match.group("braced") or match.group("plain")
            return variables.get(name.casefold(), match.group(0))

        return variable_reference.sub(replace, value)

    def normalize_expression(value: str) -> str:
        expanded = expand_variables(value)
        expanded = re.sub(r"\bjoin-path\b", " ", expanded, flags=re.IGNORECASE)
        expanded = expanded.translate(
            str.maketrans(
                {"\\": "/", '"': " ", "'": " ", "(": " ", ")": " ", ",": " "}
            )
        )
        pieces = [
            piece.strip("/;")
            for piece in expanded.split()
            if piece and not piece.startswith("-")
        ]
        return re.sub(r"/+", "/", "/".join(piece for piece in pieces if piece))

    def is_runtime_root(value: str) -> bool:
        normalized = value.strip(" \t\"'();,").replace("\\", "/").rstrip("/")
        parts = tuple(part.casefold() for part in normalized.split("/") if part)
        return parts[-1:] == ("run",) or parts[-2:] == ("run", "lsp")

    logical_lines: list[tuple[int, str]] = []
    fragments: list[str] = []
    start_line = 1
    for line_number, physical_line in enumerate(source.splitlines(), start=1):
        line = physical_line.rstrip()
        if not fragments:
            start_line = line_number
        continued = line.endswith(("\\", "`"))
        fragments.append((line[:-1] if continued else line).strip())
        if continued:
            continue
        logical_lines.append((start_line, " ".join(fragments)))
        fragments = []
    if fragments:
        logical_lines.append((start_line, " ".join(fragments)))

    findings: list[int] = []
    for line_number, line in logical_lines:
        if not line or line.lstrip().startswith("#"):
            continue
        assignment = powershell_assignment.match(line) or shell_assignment.match(line)
        if assignment is not None:
            variables[assignment.group("name").casefold()] = normalize_expression(
                assignment.group("value")
            )
            continue
        command = delete_command.match(line)
        if command is None:
            continue
        arguments = expand_variables(command.group("arguments"))
        candidates = [arguments, normalize_expression(arguments), *arguments.split()]
        if any(is_runtime_root(candidate) for candidate in candidates):
            findings.append(line_number)
    return findings


@pytest.mark.parametrize(
    "source",
    [
        """
import shutil as cleanup
runtime = state_root.joinpath("run")
cleanup.rmtree(runtime)
""",
        """
from shutil import rmtree as wipe
runtime = state_root / "run"
lsp_runtime = runtime.joinpath("lsp")
wipe(lsp_runtime)
""",
        """
import os as platform_os
platform_os.remove(state_root.joinpath("run"))
""",
        """
from os import unlink as erase
lsp_runtime = state_path / "run" / "lsp"
erase(lsp_runtime)
""",
        """
lsp_root = (state_root / "run").joinpath("lsp")
lsp_root.unlink()
""",
    ],
)
def test_runtime_root_static_guard_recognizes_direct_python_deletion(source):
    assert _python_direct_runtime_root_deletions(source)


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            """
import os as filesystem
runtime = state_root / "run"
target = runtime.joinpath("lsp")
filesystem.rmdir(
    target,
)
""",
            id="os-rmdir-positional",
        ),
        pytest.param(
            """
from os import rmdir as remove_directory
runtime = state_root.joinpath("run")
target = runtime / "lsp"
remove_directory(
    path=target,
)
""",
            id="os-rmdir-keyword",
        ),
        pytest.param(
            """
import os
target = state_root / "run" / "lsp"
os.remove(
    target,
)
""",
            id="os-remove-positional",
        ),
        pytest.param(
            """
import os as filesystem
runtime = state_root.joinpath("run")
target = runtime / "lsp"
filesystem.remove(
    path=target,
)
""",
            id="os-remove-keyword",
        ),
        pytest.param(
            """
from os import unlink as erase
runtime = state_root / "run"
target = runtime.joinpath("lsp")
erase(
    target,
)
""",
            id="os-unlink-positional",
        ),
        pytest.param(
            """
from os import unlink as erase
runtime = state_root.joinpath("run")
target = runtime / "lsp"
erase(
    path=target,
)
""",
            id="os-unlink-keyword",
        ),
        pytest.param(
            """
from pathlib import Path as RuntimePath
runtime = state_root / "run"
target = runtime.joinpath("lsp")
RuntimePath.unlink(
    target,
)
""",
            id="path-unlink-positional",
        ),
        pytest.param(
            """
from pathlib import Path as RuntimePath
runtime = state_root.joinpath("run")
target = runtime / "lsp"
RuntimePath.unlink(
    self=target,
)
""",
            id="path-unlink-keyword",
        ),
        pytest.param(
            """
import pathlib as paths
runtime = state_root / "run"
target = runtime.joinpath("lsp")
paths.Path.rmdir(
    target,
)
""",
            id="path-rmdir-positional",
        ),
        pytest.param(
            """
from pathlib import Path
runtime = state_root.joinpath("run")
target = runtime / "lsp"
Path.rmdir(
    self=target,
)
""",
            id="path-rmdir-keyword",
        ),
        pytest.param(
            """
import shutil as cleanup
runtime = state_root / "run"
target = runtime.joinpath("lsp")
cleanup.rmtree(
    target,
)
""",
            id="shutil-rmtree-positional",
        ),
        pytest.param(
            """
import shutil as cleanup
runtime = state_root.joinpath("run")
target = runtime / "lsp"
cleanup.rmtree(
    path=target,
)
""",
            id="shutil-rmtree-keyword",
        ),
    ],
)
def test_runtime_root_static_guard_rejects_supported_bypass_forms(source):
    assert _python_direct_runtime_root_deletions(source)


def test_runtime_root_static_guard_allows_targeted_artifact_deletion():
    source = """
import shutil
queue = state_root / "run" / "queue"
lease = queue / "lease.json"
lease.unlink()
shutil.rmtree(state_root / "cache" / "staging")
"""

    assert _python_direct_runtime_root_deletions(source) == []


@pytest.mark.parametrize(
    "source",
    [
        'rm -rf "$STATE_ROOT/run/lsp"',
        'Remove-Item -Recurse (Join-Path $STATE_ROOT "run")',
    ],
)
def test_runtime_root_static_guard_recognizes_direct_installer_deletion(source):
    assert _installer_direct_runtime_root_deletions(source)


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            """
RUN_ROOT="$STATE_ROOT/run"
LSP_ROOT="${RUN_ROOT}/lsp"
rm \\
    -rf \\
    "$LSP_ROOT"
""",
            id="shell-rm-variable-multiline",
        ),
        pytest.param(
            """
RUN_ROOT="$STATE_ROOT/run"
rmdir "$RUN_ROOT"
""",
            id="shell-rmdir-variable",
        ),
        pytest.param(
            """
$RunRoot = Join-Path `
    $STATE_ROOT `
    "run"
$LspRoot = Join-Path $RunRoot "lsp"
Remove-Item `
    -Recurse `
    -LiteralPath `
    $LspRoot
""",
            id="powershell-remove-item-variable-multiline",
        ),
        pytest.param(
            """
$LspRoot = "$STATE_ROOT\\run\\lsp"
del $LspRoot
""",
            id="powershell-del-variable",
        ),
    ],
)
def test_installer_guard_resolves_variables_and_line_continuations(source):
    assert _installer_direct_runtime_root_deletions(source)


def test_installers_and_doctor_have_no_direct_runtime_root_deletion_calls():
    root = Path(__file__).resolve().parent.parent
    doctor_source = (root / "scripts/doctor.py").read_text(encoding="utf-8")
    installer_sources = [
        (root / "install.sh").read_text(encoding="utf-8"),
        (root / "install.ps1").read_text(encoding="utf-8"),
    ]

    assert _python_direct_runtime_root_deletions(doctor_source) == []
    assert all(
        _installer_direct_runtime_root_deletions(source) == []
        for source in installer_sources
    )


def test_deletion_blocks_every_legacy_and_retained_queue_artifact(tmp_path):
    import doctor

    state_root = tmp_path / "state"
    run = state_root / "run"
    legacy = run / "queue"
    results = run / "queue-results"
    quarantine = run / "queue-quarantine"
    for directory in (legacy, results, quarantine):
        directory.mkdir(parents=True, exist_ok=True)
    (legacy / "pending.json").write_text("{}", encoding="utf-8")
    (legacy / "leased.processing").write_text("broken", encoding="utf-8")
    (results / "retained.tmp").write_text("result", encoding="utf-8")
    (quarantine / "bad.json").write_text("{}", encoding="utf-8")

    result = doctor._run_deletion_check(
        state_root, datetime.now(timezone.utc), deadline=float("inf")
    )

    assert {item["code"] for item in result["blockers"]} >= {
        "legacy_queue_retained",
        "legacy_queue_malformed",
        "queue_result_retained",
        "queue_quarantine_retained",
    }


def test_deletion_blocks_source_state_and_any_partial_database_error(
    tmp_path, monkeypatch
):
    import doctor

    state_root = tmp_path / "state"
    path = state_root / "run/queue.sqlite3"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE tasks(
                id TEXT PRIMARY KEY, state TEXT, error_code TEXT,
                blocked_capability TEXT
            );
            INSERT INTO tasks VALUES('task','ready',NULL,NULL);
            CREATE TABLE source_fences(daily_id TEXT);
            INSERT INTO source_fences VALUES('2026-01-01');
            CREATE TABLE source_failures(logical_path TEXT);
            INSERT INTO source_failures VALUES('knowledge/daily/2026-01-01.md');
            """
        )
    real = doctor._readonly_database

    class BrokenAfterRows:
        def __enter__(self):
            self.database = real(path, state_root, max_bytes=doctor.MAX_OPERATIONAL_DB_BYTES)
            return self

        def __exit__(self, *args):
            self.database.close()

        def execute(self, sql, parameters=()):
            if "source_failures" in sql:
                raise sqlite3.DatabaseError("corrupt tail")
            return self.database.execute(sql, parameters)

    monkeypatch.setattr(doctor, "_readonly_database", lambda *args, **kwargs: BrokenAfterRows())

    queue = doctor._queue_v2_check(
        state_root, datetime.now(timezone.utc), float("inf")
    )
    deletion = doctor._run_deletion_check(
        state_root,
        datetime.now(timezone.utc),
        deadline=float("inf"),
        collected={"queue": queue},
    )

    assert queue["details"]["read_error"] is True
    assert "queue_state_unreadable" in {
        item["code"] for item in deletion["blockers"]
    }


def test_deletion_reuses_collected_checks_without_rescanning(tmp_path, monkeypatch):
    import doctor

    transaction = {
        "id": "transactions",
        "status": "ok",
        "details": {"deletion_codes": []},
    }
    queue = {"id": "queue", "status": "ok", "details": {"deletion_codes": []}}
    archive = {"id": "archives", "status": "ok", "details": {"deletion_codes": []}}
    monkeypatch.setattr(
        doctor,
        "_transaction_check",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("rescanned")),
    )
    monkeypatch.setattr(
        doctor,
        "_queue_check",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("rescanned")),
    )

    result = doctor._run_deletion_check(
        tmp_path,
        datetime.now(timezone.utc),
        deadline=float("inf"),
        collected={"transactions": transaction, "queue": queue, "archives": archive},
    )

    assert result == {"allowed": True, "blockers": []}


def _malformed_transaction_db(
    state_root: Path,
    now: datetime,
    *,
    state: object = "committed",
    updated_at: object | None = None,
    created_at: object = "valid",
    request_hash: object = "a" * 64,
    plan_hash: object = "b" * 64,
    operation_transaction_id: str = "tx-health",
    operation_kind: str = "create",
    before_hash: object = "absent",
    after_hash: object = "c" * 64,
    create_artifact: bool = True,
) -> None:
    database = state_root / "run/markdown-transactions.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    timestamp = now.isoformat()
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE "transaction" (
                id, operation_id, request_hash, state, preconditions_json,
                plan_hash, created_at, updated_at, artifacts_pruned_at
            );
            CREATE TABLE "operation" (
                transaction_id, position, kind, path, before_hash, after_hash,
                parent_device, parent_inode, applied
            );
            """
        )
        connection.execute(
            'INSERT INTO "transaction" VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)',
            (
                "tx-health",
                "operation-health",
                request_hash,
                state,
                "{}",
                plan_hash,
                timestamp if created_at == "valid" else created_at,
                timestamp if updated_at is None else updated_at,
            ),
        )
        connection.execute(
            'INSERT INTO "operation" VALUES (?, 0, ?, ?, ?, ?, 1, 2, 1)',
            (
                operation_transaction_id,
                operation_kind,
                "knowledge/notes/health.md",
                before_hash,
                after_hash,
            ),
        )
    if create_artifact:
        (state_root / "run/transactions/tx-health").mkdir(parents=True)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ({"state": "invented"}, "transaction_state_unknown"),
        ({"updated_at": "not-a-date"}, "transaction_state_corrupt"),
        ({"created_at": None}, "transaction_state_corrupt"),
        ({"request_hash": None}, "transaction_state_corrupt"),
        ({"request_hash": "short"}, "transaction_state_corrupt"),
        ({"plan_hash": "short"}, "transaction_state_corrupt"),
        (
            {"before_hash": "c" * 64, "after_hash": "absent"},
            "transaction_state_corrupt",
        ),
        ({"create_artifact": False}, "transaction_state_corrupt"),
        (
            {"operation_transaction_id": "missing-transaction"},
            "transaction_state_corrupt",
        ),
    ],
)
def test_transaction_health_blocks_unknown_or_corrupt_rows(
    tmp_path, mutation, expected_code
):
    import doctor

    now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
    state_root = tmp_path / "state"
    _malformed_transaction_db(state_root, now, **mutation)

    check = doctor._transaction_check(state_root, now)
    deletion = doctor._run_deletion_check(
        state_root, now, collected={"transactions": check}
    )

    assert check["status"] == "error"
    assert expected_code in check["details"]["deletion_codes"]
    assert deletion["allowed"] is False


def test_transaction_health_blocks_missing_required_schema(tmp_path):
    import doctor

    state_root = tmp_path / "state"
    database = state_root / "run/markdown-transactions.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            'CREATE TABLE "transaction" (id, state, updated_at)'
        )

    check = doctor._transaction_check(
        state_root, datetime.now(timezone.utc)
    )

    assert check["status"] == "error"
    assert "transaction_state_corrupt" in check["details"]["deletion_codes"]


def test_recent_committed_transaction_with_malformed_date_cannot_allow_deletion(
    tmp_path,
):
    import doctor

    now = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
    state_root = tmp_path / "state"
    _malformed_transaction_db(state_root, now, updated_at="recent-but-malformed")

    result = doctor._run_deletion_check(state_root, now)

    assert result["allowed"] is False
    assert "transaction_state_corrupt" in {
        item["code"] for item in result["blockers"]
    }


def _retained_queue_db(state_root: Path, now: datetime) -> None:
    database = state_root / "run/queue.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    results = state_root / "run/queue-results"
    results.mkdir(parents=True)
    result = results / "done.json"
    result.write_bytes(b'{"ok":true}')
    result.chmod(0o600)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE tasks(id, state, error_code, blocked_capability, "
            "lease_expires_at, result_reference, result_sha256)"
        )
        connection.executemany(
            "INSERT INTO tasks VALUES (?, ?, ?, NULL, NULL, ?, ?)",
            [
                (
                    "done",
                    "succeeded",
                    None,
                    None,
                    None,
                ),
                ("cancelled", "cancelled", "cancelled", None, None),
                ("dead", "dead", "attempts_exhausted", None, None),
            ],
        )
    (state_root / "run/queue-migrated-v2").write_text("complete", encoding="utf-8")


def test_policy_retention_blocks_deletion_without_degrading_health(tmp_path, monkeypatch):
    import doctor
    import session_start_context

    from tests.test_doctor import (
        _build_root,
        _create_claim_index,
        _create_index,
        _qualified_pyright_check,
    )

    root, state_root, home = _build_root(tmp_path)
    now = datetime.now(timezone.utc)
    _malformed_transaction_db(state_root, now)
    _retained_queue_db(state_root, now)
    (state_root / "run/state.json").write_text(
        json.dumps(
            {
                "last_nightly_date": now.date().isoformat(),
                "last_nightly_status": "success",
            }
        ),
        encoding="utf-8",
    )
    index = state_root / "cache/index.sqlite"
    _create_index(index)
    _create_claim_index(root, state_root)
    os.utime(index, (now.timestamp(), now.timestamp()))
    monkeypatch.setattr(doctor, "_pyright_check", _qualified_pyright_check)

    report = doctor.run_doctor(root=root, state_root=state_root, home=home, now=now)
    checks = {check["id"]: check for check in report["checks"]}

    assert report["run_deletion"]["allowed"] is False
    assert {
        "transaction_undo_retained",
        "queue_task_retained",
        "queue_result_retained",
    }.issubset(
        {item["code"] for item in report["run_deletion"]["blockers"]}
    )
    assert checks["transactions"]["status"] == "ok"
    assert checks["queue"]["status"] == "ok", checks["queue"]
    assert checks["run_deletion"]["status"] == "ok"
    assert checks["generation"]["status"] == "ok"
    assert checks["generation"]["details"]["recommended_action"] == "rebuild_generation"
    assert report["overall_status"] == "ok"
    assert doctor.degraded_summary(report) == ""
    monkeypatch.setattr(doctor, "run_doctor", lambda **kwargs: report)
    assert session_start_context.health_block() == ""


@pytest.mark.parametrize(
    ("state", "error_code"),
    [
        ("invented", None),
        ("succeeded", "unexpected_failure"),
        ("dead", None),
        ("cancelled", "wrong_code"),
    ],
)
def test_queue_health_fails_closed_on_unknown_state_or_error_metadata(
    tmp_path, state, error_code
):
    import doctor

    state_root = tmp_path / "state"
    database = state_root / "run/queue.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE tasks(id, state, error_code, blocked_capability, "
            "lease_expires_at, result_reference, result_sha256)"
        )
        connection.execute(
            "INSERT INTO tasks VALUES ('task', ?, ?, NULL, NULL, NULL, NULL)",
            (state, error_code),
        )
    (state_root / "run/queue-migrated-v2").write_text("complete", encoding="utf-8")

    check = doctor._queue_v2_check(state_root, datetime.now(timezone.utc), float("inf"))
    deletion = doctor._run_deletion_check(
        state_root,
        datetime.now(timezone.utc),
        collected={"queue": check},
    )

    assert check["status"] == "error"
    assert {
        "queue_state_unknown",
        "queue_state_corrupt",
    } & set(check["details"]["deletion_codes"])
    assert deletion["allowed"] is False


def test_queue_health_fails_closed_when_required_state_metadata_is_missing(tmp_path):
    import doctor

    state_root = tmp_path / "state"
    database = state_root / "run/queue.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE tasks(id, error_code, blocked_capability)"
        )
        connection.execute("INSERT INTO tasks VALUES ('task', NULL, NULL)")

    check = doctor._queue_v2_check(
        state_root, datetime.now(timezone.utc), float("inf")
    )

    assert check["status"] == "error"
    assert "queue_state_corrupt" in check["details"]["deletion_codes"]
