"""No new site may decide repository identity by comparing whole scope records.

The same mistake has now been made four times: `NEW-65` (generation
eligibility), `NEW-90` (publication root), `NEW-111` (the open loop) and
`NEW-138` (the reuse gates in `evidence_graph_builder` and `doctor`). Each time
a recorded `RepositoryScope` -- or its `as_dict()` record -- was compared whole
against a live-resolved one. A scope carries `git_commit`, which is provenance,
and this vault commits its own runtime, so "is this the same repository" was
answered "almost never".

Fixing occurrences one at a time has not worked, and no typing change can:
`NEW-65`, `NEW-90` and `NEW-111` compared objects, where a narrower `__eq__`
might have helped, but `NEW-138` compared plain dicts, where the class is not
involved at all. What both shapes have in common is syntax, so this guard is
syntactic -- the same property-not-function approach as
`tests/test_security_invariants.py`.

What it does: for every function in `scripts/`, it seeds the names known to
hold a repository scope (parameters annotated `RepositoryScope`, the parameter
names the codebase uses, and anything assigned from
`resolve_repository_scope`, `RepositoryScope.from_dict`, or a manifest's
`repository_scope` key), grows that set monotonically through assignments, and
then reports every `==`/`!=` whose operand is a *whole* scope.

What it does not do, stated so the next reader does not over-trust it: it reads
one function at a time, so a scope handed across a call boundary under a name
it does not seed is invisible, and it says nothing about `<`/`in`/`hash`. It is
a tripwire on the shape that has actually been written five times, not a proof.

Answering rather than removing: two comparisons are legitimate and are listed
below with the question each one asks. A new entry is allowed, but it must name
the question -- "is this the same repository" is never the right one for `==`;
`repository_scope.same_repository` or `same_repository_record` answers that.
"""

from __future__ import annotations

import ast
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# Parameter names this codebase uses for a whole scope record.
SEED_PARAMETERS = frozenset(
    {"repository_scope", "repository_scope_object", "expected_repository_scope"}
)

# Expressions that hand back a whole scope, in either form.
PRODUCERS = (
    'get("repository_scope")',
    "get('repository_scope')",
    '["repository_scope"]',
    "resolve_repository_scope(",
    "RepositoryScope.from_dict(",
)

# Comparisons that are deliberate, each with the question it asks.
ALLOWED = {
    # "Is the active generation byte-for-byte what this checkout is at now?" --
    # the identity question one line below goes through `same_repository_record`,
    # and the difference between the two answers is what `superseded` reports.
    ("doctor.py", "_scope_state"),
    # "Did the manifest we are publishing record the scope we were handed?" --
    # publication binds provenance too, commit included; both sides come from
    # the same build, so this is self-consistency, not repository identity.
    ("generation_catalog.py", "_require_publication_scope"),
}


def _is_producer(node: ast.AST) -> bool:
    """Unparsing is the expensive step, so only shapes that can match reach it."""
    if not isinstance(node, (ast.Call, ast.Subscript)):
        return False
    text = ast.unparse(node)
    return any(marker in text for marker in PRODUCERS)


def _carries_scope(node: ast.AST, names: frozenset[str]) -> bool:
    """A whole scope record -- not a field read off one."""
    if isinstance(node, ast.Name):
        return node.id in names
    if _is_producer(node):
        return True
    return _is_scope_serialization(node, names)


def _is_scope_serialization(node: ast.AST, names: frozenset[str]) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    return node.func.attr == "as_dict" and _carries_scope(node.func.value, names)


def _seeded_parameters(function: ast.AST) -> set[str]:
    arguments = (
        list(function.args.posonlyargs)
        + list(function.args.args)
        + list(function.args.kwonlyargs)
    )
    return {argument.arg for argument in arguments if _seeds_a_scope(argument)}


def _seeds_a_scope(argument: ast.arg) -> bool:
    if argument.arg in SEED_PARAMETERS:
        return True
    annotation = argument.annotation
    return annotation is not None and "RepositoryScope" in ast.unparse(annotation)


def _grow_once(assignments: list[ast.Assign], names: set[str]) -> bool:
    """One monotonic pass. Never rebinds, so the fixed point always exists.

    The alias pass in `test_context_compiler` did rebind, and a module with one
    name assigned twice made it loop forever; see the log for 2026-08-26.
    """
    grew = False
    for assignment in assignments:
        grew = _grow_from(assignment, names) or grew
    return grew


def _grow_from(assignment: ast.Assign, names: set[str]) -> bool:
    if not _carries_scope(assignment.value, frozenset(names)):
        return False
    targets = {
        target.id
        for target in assignment.targets
        if isinstance(target, ast.Name) and target.id not in names
    }
    names.update(targets)
    return bool(targets)


def _scope_names(function: ast.AST) -> frozenset[str]:
    names = _seeded_parameters(function)
    assignments = [node for node in ast.walk(function) if isinstance(node, ast.Assign)]
    for _ in range(len(assignments) + 1):
        if not _grow_once(assignments, names):
            break
    return frozenset(names)


def _equality_operands(node: ast.AST) -> list[ast.AST]:
    if not isinstance(node, ast.Compare):
        return []
    if not any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
        return []
    return [node.left] + list(node.comparators)


def _findings_in(function: ast.AST, names: frozenset[str], path: Path) -> list[str]:
    found = []
    for node in ast.walk(function):
        operands = _equality_operands(node)
        if any(_carries_scope(operand, names) for operand in operands):
            found.append(f"{path.name}:{node.lineno} in {function.name}: {ast.unparse(node)}")
    return found


def _whole_scope_comparisons() -> list[str]:
    findings: list[str] = []
    for path in sorted(SCRIPTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        findings.extend(_findings_in_file(tree, path))
    return findings


def _findings_in_file(tree: ast.AST, path: Path) -> list[str]:
    findings: list[str] = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if (path.name, function.name) in ALLOWED:
            continue
        findings.extend(_findings_in(function, _scope_names(function), path))
    return findings


def test_no_script_decides_repository_identity_by_whole_scope_equality():
    """Five sites wrote this shape. A sixth must fail here rather than in a build."""
    findings = _whole_scope_comparisons()
    assert findings == [], (
        "A repository scope is being compared whole. It carries `git_commit`, "
        "which is provenance, so this asks 'same repository AND same commit'. "
        "Use `repository_scope.same_repository` (objects) or "
        "`same_repository_record` (serialized), or add the site to ALLOWED with "
        "the question it actually asks:\n  " + "\n  ".join(findings)
    )


def test_the_guard_still_sees_the_shape_it_was_written_for():
    """A guard that cannot fail is not a guard. This is NEW-138's own code."""
    source = (
        "def f(parent, repository_scope_object):\n"
        "    return parent.get('repository_scope') == repository_scope_object\n"
    )
    tree = ast.parse(source)
    findings = _findings_in_file(tree, Path("evidence_graph_builder.py"))
    assert len(findings) == 1, findings


def test_the_guard_still_sees_the_object_shape():
    """NEW-65, NEW-90 and NEW-111 compared `RepositoryScope` objects, not records."""
    source = (
        "def f(registered: RepositoryScope, live: RepositoryScope) -> bool:\n"
        "    return registered != live\n"
    )
    findings = _findings_in_file(ast.parse(source), Path("generation_catalog.py"))
    assert len(findings) == 1, findings


def test_the_guard_does_not_fire_on_a_field_comparison():
    """`(x.repository_id, x.checkout_id)` carries no commit and is not the shape."""
    source = (
        "def f(repository_scope, observed):\n"
        "    return observed != (repository_scope.repository_id, "
        "repository_scope.checkout_id)\n"
    )
    assert _findings_in_file(ast.parse(source), Path("evidence_graph.py")) == []
