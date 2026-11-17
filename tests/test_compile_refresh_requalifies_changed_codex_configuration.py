"""Configuration-only changes must refresh planning before another dispatch."""
from dataclasses import replace

import llm_client as client
import pytest
from llm_wiki import compile_pipeline_batches

from tests.codex_planning_basis_owns_the_selected_model_helpers import _basis
from tests.compile_packing_counts_original_entry_metadata_helpers import _inputs
from tests.compile_paths import point_compile_path
from tests.compile_refresh_requalifies_changed_provider_environment_helpers import _empty_context


def _write_configuration(path, content):
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.write_bytes(content)


@pytest.mark.parametrize('before,after', [
    (b'[hooks.state]\n', b'[hooks.state]\ntrusted = true\n'),
    (None, b'[hooks.state]\n'),
    (b'[hooks.state]\n', None),
])
def test_configuration_change_refreshes_basis_without_environment_change(monkeypatch, tmp_path, before, after):
    path = tmp_path / 'config.toml'
    _write_configuration(path, before)
    files = ((str(path), client.codex_basis_file_digest(str(path))),)
    selected = _basis(tmp_path, files=files)
    batch = compile_pipeline_batches.pack_compile_batches(_inputs(), model=selected.model,
                                         planning_candidates=(selected.descriptor,))[0]
    point_compile_path(monkeypatch, 'snapshot_compile_inputs', _empty_context)
    calls = []

    def resolve(candidate, *, deadline):
        calls.append(candidate)
        return replace(selected, config_files=((str(path), client.codex_basis_file_digest(str(path))),))

    point_compile_path(monkeypatch, 'resolve_codex_planning_basis', resolve)
    _write_configuration(path, after)
    assert client.codex_basis_digest(client.provider_environment()) == selected.environment_sha256
    with pytest.raises(RuntimeError, match='configuration changed'):
        client.prepare_codex(selected.descriptor, 'source', 'system')
    refreshed = compile_pipeline_batches.refresh_compile_batch(batch)
    client.prepare_codex(refreshed.planning_candidates[0], 'source', 'system')
    assert calls == [selected.original_descriptor]
    assert refreshed.planning_candidates[0]._codex_basis is not selected
    assert refreshed.inputs.dailies == batch.inputs.dailies
    assert refreshed.manifest == batch.manifest
    with pytest.raises(RuntimeError, match='configuration changed'):
        client.prepare_codex(selected.descriptor, 'source', 'system')
