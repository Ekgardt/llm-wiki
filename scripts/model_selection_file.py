"""Read and publish the saved model choice in the private settings file."""
from __future__ import annotations

import settings
from bounded_io import read_stable_bytes
from llm_wiki import runtime_roots
from llm_wiki.model_choice import selection_table
from llm_wiki.sqlite_transactions import begin_immediate
from memory_state import atomic_write


def file_bytes(root=None):
    try:
        return read_stable_bytes(settings.settings_path(root), settings.MAX_SETTINGS_FILE_BYTES,
                                 label='settings file')
    except FileNotFoundError:
        return b''


def _render(raw, chosen):
    import tomlkit

    settings.parsed_bytes(raw, settings.SETTINGS_FILE_NAME)
    document = tomlkit.parse(raw.decode('utf-8'))
    table = document.setdefault('llm', tomlkit.table())
    table.update(selection_table(chosen))
    _remove_unused_reasoning(table, chosen)
    rendered = tomlkit.dumps(document).encode('utf-8')
    if len(rendered) > settings.MAX_SETTINGS_FILE_BYTES:
        raise ValueError('model selection exceeds the shared settings-file byte bound')
    settings.parsed_bytes(rendered, settings.SETTINGS_FILE_NAME)
    return rendered


def save(chosen, *, expected, root=None):
    """Save after verification, refusing concurrent edits; no provider call under lock."""
    from markdown_transaction import active_or_legacy_coordinator

    path = settings.settings_path(root)
    vault = path.parent.resolve(strict=True)
    state = runtime_roots.state_root_for(vault)
    coordinator = active_or_legacy_coordinator(vault, state)
    rendered = _render(expected, chosen)
    with coordinator.writer_gate():
        _publish(coordinator, path, expected, rendered)
    return chosen


def _publish(coordinator, path, expected, rendered):
    with coordinator._connect() as database, begin_immediate(database):
        coordinator._assert_writer_ownership(database)
        if file_bytes(path.parent) != expected:
            raise ValueError('settings changed during model verification; choose again')
        atomic_write(path, rendered.decode('utf-8'))


def _remove_unused_reasoning(table, chosen):
    if chosen.reasoning is None:
        table.pop('reasoning', None)
