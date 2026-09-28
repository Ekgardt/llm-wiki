"""The install brings the runtime of the model the read path embeds with.

The models step fetches a pinned model only when its runtime modules are
installed, and the dependency step synced the baseline alone: `onnxruntime` never
arrived, the e5 weights were never fetched or used, and doctor reported the
missing runtime after every install. This holds the sync plan to the models:
what a default install syncs must carry the embedding model's runtime, and every
other pinned model's runtime must be one declared extra away.
See docs/research/2026-09-28-a-check-names-its-cause.md.
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from install_models import EMBEDDING_MODEL, pinned_models
from installer_config import uv_sync_arguments

ROOT = Path(__file__).resolve().parent.parent
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
SELF_REFERENCE = re.compile(r"^llm-wiki\[(?P<extras>[^\]]+)\]")


def _distribution(requirement: str) -> str:
    return re.split(r"[\s<>=!~;\[]", requirement, maxsplit=1)[0].lower().replace("-", "_")


def _extra_distributions(extra: str) -> set[str]:
    """Every distribution one extra brings, following `llm-wiki[...]` references."""
    found: set[str] = set()
    for requirement in PROJECT["optional-dependencies"][extra]:
        nested = SELF_REFERENCE.match(requirement)
        found |= _nested(nested) if nested else {_distribution(requirement)}
    return found


def _nested(match: re.Match[str]) -> set[str]:
    return set().union(*(_extra_distributions(name.strip()) for name in match["extras"].split(",")))


def _planned_distributions(tmp_path: Path) -> set[str]:
    """The baseline plus the extras the installer's sync plan names."""
    _environment, arguments = uv_sync_arguments(tmp_path, None)
    extras = [arguments[index + 1] for index, value in enumerate(arguments) if value == "--extra"]
    planned = {_distribution(requirement) for requirement in PROJECT["dependencies"]}
    return planned.union(*(_extra_distributions(extra) for extra in extras))


def test_a_default_install_syncs_the_runtime_of_the_embedding_model(tmp_path: Path) -> None:
    planned = _planned_distributions(tmp_path)
    fetched = [model.repo_id for model in pinned_models() if set(model.runtime) <= planned]
    assert EMBEDDING_MODEL in fetched


def _one_extra_brings(runtime: tuple[str, ...]) -> bool:
    declared = (_extra_distributions(extra) for extra in PROJECT["optional-dependencies"])
    return any(set(runtime) <= brought for brought in declared)


def test_every_pinned_model_has_its_runtime_in_one_declared_extra() -> None:
    orphaned = [model.repo_id for model in pinned_models() if not _one_extra_brings(model.runtime)]
    assert orphaned == []
