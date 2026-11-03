"""Original source-level detection fixtures, with unchanged assertions."""
from pathlib import Path

import pytest


def test_scanner_keeps_runtime_writes_outside_boundary():
    from check_knowledge_writers import scan_source

    source = 'Path("cache/access.jsonl").write_text("x")\n'
    assert scan_source(Path("scripts/example.py"), source) == []


@pytest.mark.parametrize(
    ("source", "api"),
    [
        ('p = ROOT / "knowledge" / "notes" / "x.md"\np.open("w")\n', "open"),
        ('p = ROOT / "knowledge" / "notes" / "x.md"\np.write_text("x")\n', "write_text"),
        ('p = ROOT / "knowledge" / "notes" / "x.md"\np.touch()\n', "touch"),
        ('p = ROOT / "knowledge" / "notes" / "x.md"\np.unlink()\n', "unlink"),
        ('p = ROOT / "knowledge" / "notes"\np.mkdir()\n', "mkdir"),
        ('dst = ROOT / "knowledge" / "notes" / "x.md"\nos.replace("tmp", dst)\n', "replace"),
        ('dst = ROOT / "knowledge" / "notes" / "x.md"\nos.rename("tmp", dst)\n', "rename"),
        ('dst = ROOT / "knowledge" / "notes" / "x.md"\nshutil.move("tmp", dst)\n', "move"),
    ],
)
def test_python_scanner_resolves_write_destinations(source, api):
    from check_knowledge_writers import scan_source

    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved) for item in findings] == [(api, False)]


def test_python_scanner_recognizes_feedback_and_boundary_calls():
    from check_knowledge_writers import scan_source

    source = (
        "from markdown_transaction import mutate_knowledge\n"
        'p = ROOT / "knowledge" / "feedback" / "abcdef123456.json"\n'
        'mutate_knowledge("event", {p: b"x"})\n'
    )
    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved) for item in findings] == [("mutate_knowledge", True)]


def test_python_scanner_detects_direct_guardrails_atomic_write():
    from check_knowledge_writers import scan_source

    source = (
        "from memory_state import atomic_write\n"
        'target = ROOT / "knowledge" / "guardrails.md"\n'
        'atomic_write(target, "unsafe")\n'
    )

    findings = scan_source(Path("scripts/probe.py"), source)

    assert [(item.api, item.approved) for item in findings] == [("atomic_write", False)]


def test_python_scanner_recognizes_checked_markdown_publication_boundary():
    from check_knowledge_writers import scan_source

    source = """
def _apply_windows_operation(staged, destination):
    durable_publish_file(
        staged,
        destination,
        replace=True,
        expected_sha256="0" * 64,
        max_bytes=1024,
    )
"""

    findings = scan_source(Path("scripts/markdown_transaction.py"), source)

    assert [(item.api, item.approved) for item in findings] == [("durable_publish_file", True)]


def test_python_scanner_traces_parameter_sink_alias_and_path_alias():
    from check_knowledge_writers import scan_source

    source = """
from pathlib import Path as P
ROOT_DIR = P("knowledge")
def raw_write(destination):
    destination.write_text("x")
writer = raw_write
target = ROOT_DIR / "notes" / "x.md"
writer(target)
"""
    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved, item.function) for item in findings] == [
        ("raw_write", False, "<module>")
    ]


def test_python_scanner_traces_method_path_parameter_to_callsite():
    from check_knowledge_writers import scan_source

    source = """
from pathlib import Path
class Writer:
    def save(self, destination):
        destination.write_bytes(b"x")
target = Path("knowledge/notes/x.md")
Writer().save(target)
"""
    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved, item.function) for item in findings] == [
        ("save", False, "<module>")
    ]


@pytest.mark.parametrize(
    ("source", "approved"),
    [
        (
            "from scripts.markdown_transaction import mutate_knowledge as mutate\n"
            'p = Path("knowledge/notes/x.md")\nmutate("id", {p: b"x"})\n',
            True,
        ),
        (
            "def mutate_knowledge(operation_id, changes):\n    return None\n"
            'p = Path("knowledge/notes/x.md")\nmutate_knowledge("id", {p: b"x"})\n',
            False,
        ),
    ],
)
def test_python_scanner_proves_canonical_boundary_binding(source, approved):
    from check_knowledge_writers import scan_source

    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved) for item in findings] == [
        ("mutate_knowledge" if not approved else "mutate", approved)
    ]


def test_python_scanner_tracks_canonical_module_and_function_aliases():
    from check_knowledge_writers import scan_source

    source = """
import scripts.markdown_transaction as transactions
from pathlib import Path as P
safe_mutate = transactions.mutate_knowledge
target = P("knowledge") / "notes" / "x.md"
safe_mutate("id", {target: b"x"})
"""
    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved) for item in findings] == [("safe_mutate", True)]


@pytest.mark.parametrize(
    "source",
    [
        """
p = Path("cache/x")
if enabled:
    p = Path("knowledge/notes/x.md")
else:
    p = Path("cache/y")
p.write_text("x")
""",
        """
p = Path("cache/x")
for candidate in candidates:
    if candidate:
        p = Path("knowledge/notes/x.md")
p.write_text("x")
""",
        """
p = Path("cache/x")
while pending:
    p = Path("knowledge/notes/x.md")
p.write_text("x")
""",
        """
p = Path("cache/x")
try:
    p = Path("knowledge/notes/x.md")
except OSError:
    p = Path("cache/y")
finally:
    p.write_text("x")
""",
        """
p = Path("cache/x")
try:
    p = Path("knowledge/notes/x.md")
    risky()
    p = Path("cache/y")
except OSError:
    pass
finally:
    p.write_text("x")
""",
        """
p = Path("cache/x")
match kind:
    case "note":
        p = Path("knowledge/notes/x.md")
    case _:
        p = Path("cache/y")
writer = lambda: p.write_text("x")
[writer() for _ in range(1)]
""",
    ],
)
def test_python_scanner_preserves_covered_paths_across_control_flow(source):
    from check_knowledge_writers import scan_source

    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved) for item in findings] == [("write_text", False)]


def test_python_scanner_analyzes_nested_helper_definitions():
    from check_knowledge_writers import scan_source

    source = """
def outer(destination):
    def helper(path):
        with path.open("w") as stream:
            stream.write("x")
    helper(destination)
outer(Path("knowledge/notes/x.md"))
"""
    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved, item.function) for item in findings] == [
        ("outer", False, "<module>")
    ]


def test_python_scanner_merges_function_aliases_across_branches():
    from check_knowledge_writers import scan_source

    source = """
def raw(path):
    path.write_text("x")
def harmless(path):
    return path
if enabled:
    writer = raw
else:
    writer = harmless
writer(Path("knowledge/notes/x.md"))
"""
    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved) for item in findings] == [("writer", False)]


def test_python_scanner_drops_path_taint_after_exhaustive_safe_reassignment():
    from check_knowledge_writers import scan_source

    source = """
p = Path("knowledge/notes/x.md")
if enabled:
    p = Path("cache/x")
else:
    p = Path("logs/x")
p.write_text("x")
"""
    assert scan_source(Path("scripts/probe.py"), source) == []


def test_python_scanner_drops_alias_taint_after_exhaustive_safe_reassignment():
    from check_knowledge_writers import scan_source

    source = """
def raw(path):
    path.write_text("x")
def harmless(path):
    return path
writer = raw
if enabled:
    writer = harmless
else:
    writer = harmless
writer(Path("knowledge/notes/x.md"))
"""
    assert scan_source(Path("scripts/probe.py"), source) == []


def test_python_scanner_analyzes_definition_time_expressions():
    from check_knowledge_writers import scan_source

    source = """
def sink(path):
    path.write_text("x")
target = Path("knowledge/notes/x.md")
@sink(target)
def decorated(
    annotated: sink(target),
    defaulted=sink(target),
    *, keyword=sink(target),
) -> sink(target):
    pass
@sink(target)
class Defined(
    sink(target),
    metaclass=sink(target),
):
    pass
"""
    findings = scan_source(Path("scripts/probe.py"), source)
    assert [(item.api, item.approved) for item in findings] == [
        ("sink", False),
    ] * 8


@pytest.mark.parametrize(
    ("suffix", "source", "expected_api"),
    [
        (
            ".js",
            'const p = root + "/knowledge/notes/x.md";\nfs.writeFileSync(p, data);\n',
            "writeFileSync",
        ),
        (
            ".ps1",
            '$p = Join-Path $root "knowledge/notes/x.md"\nSet-Content -Path $p -Value $x\n',
            "Set-Content",
        ),
        (".sh", 'p="$root/knowledge/notes/x.md"\nprintf "%s" "$x" > "$p"\n', ">"),
    ],
)
def test_non_python_scanner_resolves_variable_write_destinations(suffix, source, expected_api):
    from check_knowledge_writers import scan_source

    findings = scan_source(Path(f"scripts/probe{suffix}"), source)
    assert [(item.api, item.approved) for item in findings] == [(expected_api, False)]


@pytest.mark.parametrize(
    ("suffix", "source"),
    [
        (
            ".js",
            '// fs.writeFileSync("knowledge/notes/x.md", x)\nconst s = "writeFile knowledge/notes/x.md";\n',
        ),
        (".ps1", '# Set-Content knowledge/notes/x.md\n$s = "Set-Content knowledge/notes/x.md"\n'),
        (".sh", '# echo x > knowledge/notes/x.md\ns="echo x > knowledge/notes/x.md"\n'),
    ],
)
def test_non_python_scanner_ignores_comments_and_strings(suffix, source):
    from check_knowledge_writers import scan_source

    assert scan_source(Path(f"scripts/probe{suffix}"), source) == []


@pytest.mark.parametrize(
    ("suffix", "source", "api"),
    [
        (
            ".js",
            'const target = root +\n  "/knowledge/notes/x.md";\n'
            "fs.writeFileSync(\n  target,\n  data\n);\n",
            "writeFileSync",
        ),
        (
            ".ps1",
            '$target = Join-Path `\n  $root `\n  "knowledge/notes/x.md"\n'
            "Set-Content `\n  -Path $target `\n  -Value $data\n",
            "Set-Content",
        ),
        (
            ".sh",
            'target="$root/knowledge/notes/x.md"\nprintf "%s" \\\n  "$data" \\\n  > "$target"\n',
            ">",
        ),
    ],
)
def test_non_python_scanner_parses_multiline_constructs(suffix, source, api):
    from check_knowledge_writers import scan_source

    findings = scan_source(Path(f"scripts/probe{suffix}"), source)
    assert [(item.api, item.approved) for item in findings] == [(api, False)]


def test_archive_exception_is_function_and_operation_scoped():
    from check_knowledge_writers import scan_source

    source = """
def _build_bag_contents():
    hidden_build = archive_root / ".bag-building"
    (hidden_build / "bagit.txt").write_bytes(data)
def _publish_build():
    hidden_build.replace(final_bag)
def _bad_flat_write():
    flat = ROOT / "knowledge" / "daily" / "2026-01-01.md"
    flat.write_bytes(data)
"""
    findings = scan_source(Path("scripts/archive_daily.py"), source)
    assert [(item.api, item.approved) for item in findings] == [
        ("write_bytes", True),
        ("replace", True),
        ("write_bytes", False),
    ]
