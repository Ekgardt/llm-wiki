"""A name the function binds itself hides an import of it (audit 2026-09-26 C-9).

docs/research/2026-09-26-a-local-name-hides-an-import.md
"""
from __future__ import annotations

import pytest
from code_extractor import extract_code

from tests.test_a_decorator_is_a_call_until_it_is_a_route import _source

LIB = b"def compute():\n    return 1\n"

# Every way a block binds a name (Python reference, "Binding of names").
SHADOWING_BODIES = {
    "assignment": "    compute = len\n    return compute()\n",
    "annotated": "    compute: object = len\n    return compute()\n",
    "augmented": "    compute += 1\n    return compute()\n",
    "walrus": "    if (compute := len):\n        return compute()\n",
    "for target": "    for compute in (len,):\n        return compute()\n",
    "with target": "    with open('x') as compute:\n        return compute()\n",
    "except name": "    try:\n        pass\n    except Exception as compute:\n        return compute()\n",
    "deletion": "    del compute\n    return compute()\n",
    "class": "    class compute:\n        pass\n    return compute()\n",
    "unpacking": "    (compute, other) = (len, 1)\n    return compute()\n",
    "capture pattern": "    match len:\n        case compute:\n            return compute()\n",
    "closure": "    compute = len\n\n    def inner():\n        return compute()\n    return inner\n",
}


def _called_paths(result) -> list[str]:
    """Where each called definition lives: `lib.py` is the import, `app.py` a local one."""
    by_id = {node["node_id"]: node["metadata"].get("path") for node in result.nodes}
    return sorted(by_id[item["target_node_id"]] for item in result.assertions if item["edge_type"] == "CALLS")


def _targets(body: str, signature: str = "run()") -> list[str]:
    app = f"from lib import compute\n\n\ndef {signature}:\n{body}".encode()
    files = {"lib.py": LIB, "app.py": app}
    result = extract_code(tuple(_source(path, content) for path, content in files.items()), repository_id="repo")
    return _called_paths(result)


@pytest.mark.parametrize("form", sorted(SHADOWING_BODIES))
def test_a_local_binding_is_not_a_call_to_the_import(form) -> None:
    assert "lib.py" not in _targets(SHADOWING_BODIES[form])


def test_a_parameter_hides_the_import() -> None:
    assert _targets("    return compute()\n", "run(compute)") == []


def test_a_nested_definition_is_the_target_not_the_import() -> None:
    targets = _targets("    def compute():\n        return 2\n    return compute()\n")

    assert targets == ["app.py"]


def test_a_global_declaration_keeps_the_import() -> None:
    assert _targets("    global compute\n    compute = compute\n    return compute()\n") == ["lib.py"]


def test_an_untouched_name_still_reaches_the_import() -> None:
    assert _targets("    return compute()\n") == ["lib.py"]


def test_an_inner_global_declaration_reaches_the_import_past_an_outer_binding() -> None:
    body = "    compute = len\n\n    def inner():\n        global compute\n        return compute()\n    return inner\n"

    assert _targets(body) == ["lib.py"]
