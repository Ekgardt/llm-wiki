"""A re-export made by `import *`, inside a `try`, or by a later import is followed (audit 2026-09-27 C-7).

Only straight-line `from x import y` statements were recorded, and the first one
won: a package that star-imports its core, or picks an implementation in
`try/except ImportError`, left the call `missing_dependency`, and a name imported
twice resolved to the file the module no longer uses.
docs/research/2026-09-27-every-re-export-form-is-followed.md
"""
from __future__ import annotations

from code_extractor import extract_code

from tests.a_decorator_is_a_call_until_it_is_a_route_helpers import _call_target_names, _source

APP = b"from lib import compute\n\n\ndef run():\n    return compute()\n"
DEFINITION = b"def compute():\n    return 1\n"


def _extract(files: dict[str, bytes]):
    return extract_code(tuple(_source(path, content) for path, content in files.items()), repository_id="repo")


def _call_target_paths(result) -> list[str]:
    paths = {node["node_id"]: node["metadata"].get("path") for node in result.nodes}
    return [paths[item["target_node_id"]] for item in result.assertions if item["edge_type"] == "CALLS"]


def _call_reasons(result) -> list[str]:
    return [item["reason"] for item in result.observations if item["edge_type"] == "CALLS"]


def test_a_star_import_hands_on_the_public_names() -> None:
    result = _extract({"lib/__init__.py": b"from .core import *\n", "lib/core.py": DEFINITION, "app.py": APP})

    assert _call_target_names(result) == ["compute"]


def test_a_star_import_hands_on_only_what_all_names() -> None:
    core = b"__all__ = ['other']\n\n\ndef compute():\n    return 1\n\n\ndef other():\n    return 2\n"
    result = _extract({"lib/__init__.py": b"from .core import *\n", "lib/core.py": core, "app.py": APP})

    assert _call_target_names(result) == []


def test_a_star_import_does_not_hand_on_a_private_name() -> None:
    app = b"from lib import _compute\n\n\ndef run():\n    return _compute()\n"
    result = _extract({"lib/__init__.py": b"from .core import *\n", "lib/core.py": b"def _compute():\n    return 1\n", "app.py": app})

    assert _call_target_names(result) == []


def test_a_fallback_import_names_both_implementations() -> None:
    package = b"try:\n    from .fast import compute\nexcept ImportError:\n    from .slow import compute\n"
    result = _extract({"lib/__init__.py": package, "lib/fast.py": DEFINITION, "lib/slow.py": DEFINITION, "app.py": APP})

    assert (_call_target_names(result), _call_reasons(result)) == ([], ["ambiguous_target"])


def test_a_later_import_replaces_the_earlier_one() -> None:
    package = b"from .old import compute\nfrom .new import compute\n"
    result = _extract({"lib/__init__.py": package, "lib/old.py": DEFINITION, "lib/new.py": DEFINITION, "app.py": APP})

    assert _call_target_paths(result) == ["lib/new.py"]
