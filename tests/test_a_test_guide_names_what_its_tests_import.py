"""A test packet's guide names the code its tests import and how the packet carries it.

Every guide titled "Tests of `X`" said the packet carries the exact derived
description of `X`, and some said its tests use only the public names of `X`.
Measured 2026-11-17 over 809 guides: 227 named the wrong carriage, 42 were titled
after a module their tests never import, and 14 claimed public-only use of a
source the packet carries whole. The statement is now rendered from the manifest
and the imports of the packet's own files, and a guide that says anything else
fails here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tests.module_context import _source_dependencies

ROOT = Path(__file__).resolve().parents[1]
Manifest = dict[str, Any]
TITLE = re.compile(r"^# Tests (of|filed with) `([^`]+)`$", re.MULTILINE)
DESCRIBED = "its exact derived description"
WHOLE = "its whole source"
PUBLIC_USE = "through its public names"


def _manifest(guide: Path) -> Manifest:
    manifest = guide.with_name(f"{guide.stem}-context.json")
    loaded: Manifest = json.loads(manifest.read_text(encoding="utf-8"))
    return loaded


def packet_guides(root: Path) -> list[Path]:
    guides = (root / "docs" / "modules").glob("*.md")
    return sorted(g for g in guides if TITLE.match(g.read_text(encoding="utf-8")))


def _product_source(path: str, owned: set[str]) -> bool:
    return not path.startswith("tests/") and not path.endswith("__init__.py") and path not in owned


def _imported_by(root: Path, name: str) -> set[str]:
    return {path.relative_to(root).as_posix() for path in _source_dependencies(root, root / name)}


def direct_imports(root: Path, manifest: Manifest) -> set[str]:
    """Product files the packet's own Python files import, by statement or by call."""
    owned = set(manifest["ownership"]["files"])
    reached: set[str] = set()
    for name in sorted(filter(lambda path: path.endswith(".py"), owned)):
        reached |= _imported_by(root, name)
    return set(filter(lambda path: _product_source(path, owned), reached))


def _carriage(path: str, manifest: Manifest) -> str:
    described = {entry["provider"] for entry in manifest.get("qualified_dependencies") or []}
    return DESCRIBED if path in described else WHOLE


def _subject_sentence(subject: str, manifest: Manifest) -> str:
    if _carriage(subject, manifest) == DESCRIBED:
        return (
            f"`{subject}` itself belongs to another module: this packet carries {DESCRIBED}, "
            "and these tests are the evidence that the description matches real use."
        )
    return (
        f"`{subject}` itself belongs to another module: this packet carries {WHOLE}, "
        "and these tests exercise it."
    )


def _filed_paragraph(subject: str, reached: set[str], manifest: Manifest) -> str:
    opening = f"These files do not import `{subject}`, the module they are filed with"
    if not reached:
        return f"{opening}, and they import no product code directly."
    lines = "\n".join(f"- `{path}`: {_carriage(path, manifest)}." for path in sorted(reached))
    return (
        f"{opening}. The product code they import directly is carried in this packet "
        f"as follows:\n\n{lines}"
    )


def expected_statement(root: Path, subject: str, manifest: Manifest) -> tuple[str, str]:
    """The title word and the paragraph that tell the truth about `subject`."""
    reached = direct_imports(root, manifest)
    if subject in reached:
        return "of", _subject_sentence(subject, manifest)
    return "filed with", _filed_paragraph(subject, reached, manifest)


def _flat(text: str) -> str:
    return " ".join(text.split())


def guide_is_true(root: Path, guide: Path) -> bool:
    text = guide.read_text(encoding="utf-8")
    title = TITLE.match(text)
    assert title is not None, guide
    word, paragraph = expected_statement(root, title.group(2), _manifest(guide))
    claims_public_use = PUBLIC_USE in text and WHOLE in paragraph
    return title.group(1) == word and _flat(paragraph) in _flat(text) and not claims_public_use


def test_every_test_guide_names_what_its_tests_import() -> None:
    untrue = [g.name for g in packet_guides(ROOT) if not guide_is_true(ROOT, g)]

    assert untrue == []


def _packet(root: Path, guide_text: str, *, described: bool) -> Path:
    (root / "scripts").mkdir()
    (root / "tests").mkdir()
    (root / "docs" / "modules").mkdir(parents=True)
    (root / "scripts" / "used.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_used.py").write_text("import used\n", encoding="utf-8")
    manifest = {
        "files": ["scripts/used.py", "tests/test_used.py"],
        "ownership": {"files": ["tests/test_used.py"]},
        "qualified_dependencies": [{"provider": "scripts/used.py"}] if described else [],
    }
    guide = root / "docs" / "modules" / "used-tests.md"
    guide.write_text(guide_text, encoding="utf-8")
    context = root / "docs" / "modules" / "used-tests-context.json"
    context.write_text(json.dumps(manifest), encoding="utf-8")
    return guide


_DESCRIBED_CLAIM = (
    "# Tests of `scripts/used.py`\n\n`scripts/used.py` itself belongs to another module: "
    "this packet carries its exact derived\ndescription, and these tests are the evidence "
    "that the description matches real use.\n"
)


def test_a_description_claim_holds_where_the_packet_describes(tmp_path: Path) -> None:
    guide = _packet(tmp_path, _DESCRIBED_CLAIM, described=True)

    assert guide_is_true(tmp_path, guide)


def test_a_description_claim_fails_where_the_packet_carries_the_source(tmp_path: Path) -> None:
    guide = _packet(tmp_path, _DESCRIBED_CLAIM, described=False)

    assert not guide_is_true(tmp_path, guide)


def test_a_subject_the_tests_never_import_fails(tmp_path: Path) -> None:
    claim = _DESCRIBED_CLAIM.replace("scripts/used.py", "scripts/other.py")
    guide = _packet(tmp_path, claim, described=True)

    assert not guide_is_true(tmp_path, guide)


def test_a_packet_carrying_the_whole_source_claims_no_public_only_use(tmp_path: Path) -> None:
    text = (
        "# Tests of `scripts/used.py`\n\n`tests/test_used.py` exercise `scripts/used.py` "
        "through its public names.\n\n`scripts/used.py` itself belongs to another module: "
        "this packet carries its whole source, and these tests exercise it.\n"
    )
    guide = _packet(tmp_path, text, described=False)

    assert not guide_is_true(tmp_path, guide)


def test_a_filed_guide_lists_what_its_tests_import(tmp_path: Path) -> None:
    text = (
        "# Tests filed with `scripts/other.py`\n\nThese files do not import "
        "`scripts/other.py`, the module they are filed with. The product code they "
        "import directly is carried in this packet as follows:\n\n"
        "- `scripts/used.py`: its whole source.\n"
    )
    guide = _packet(tmp_path, text, described=False)

    assert guide_is_true(tmp_path, guide)
