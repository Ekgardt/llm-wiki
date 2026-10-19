"""The vault's authoritative model choice, shared by every memory entrypoint."""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

# These existing adapters can carry an explicit model. OpenCode currently cannot;
# it remains discoverable in legacy auto mode, not a misleading selectable target.
SELECTABLE_PROVIDERS = ('codex', 'claude', 'openai', 'ollama')
_UNSET = object()
_CURRENT = ContextVar("memory_model_selection", default=_UNSET)
_SETTINGS_ROOT = ContextVar("memory_model_settings_root", default=None)


MODEL_ENV_KEYS = frozenset((
    'MEMORY_LLM_PROVIDER', 'MEMORY_CODEX_MODEL', 'MEMORY_CODEX_REASONING',
    'MEMORY_CLAUDE_MODEL', 'MEMORY_LLM_MODEL',
))


@dataclass(frozen=True)
class Selection:
    provider: str
    model: str
    reasoning: str | None = None

    @property
    def name(self) -> str:
        return f'{self.provider}/{self.model}'


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'llm.{field} requires nonempty text')
    if value != value.strip() or not value.isprintable():
        raise ValueError(f'llm.{field} contains whitespace or control characters')
    return value


def selection(name, reasoning=None):
    provider, separator, model = _text(name, 'model').partition('/')
    if not separator or provider not in SELECTABLE_PROVIDERS:
        raise ValueError('llm.model requires a supported provider/model identifier')
    _text(model, 'model')
    _reasoning(provider, reasoning)
    return Selection(provider, model, reasoning)


def _reasoning(provider, value):
    if provider == 'codex':
        _text(value, 'reasoning')
        return
    if value is not None:
        raise ValueError('this adapter does not expose a reasoning selector')


def table_values(table, source):
    from collections.abc import Mapping

    if not isinstance(table, Mapping) or set(table) - {'model', 'reasoning'}:
        raise ValueError(f'{source}: invalid [llm] selection fields')
    chosen = selection(table.get('model'), table.get('reasoning'))
    return {f'llm.{key}': value for key, value in selection_table(chosen).items()}


def selection_table(chosen):
    result = {'model': chosen.name}
    if chosen.reasoning is not None:
        result['reasoning'] = chosen.reasoning
    return result


def current(root=None):
    scoped = _CURRENT.get()
    if scoped is not _UNSET:
        return scoped
    return _current_file(root)


def _current_file(root):
    import settings

    values = settings._file_values(settings.settings_path(root))
    if 'llm.model' not in values:
        return None
    return selection(values['llm.model'], values.get('llm.reasoning'))


@contextmanager
def using(chosen):
    """Freeze one resolved selection; explicit discovery may inspect another provider."""
    token = _CURRENT.set(chosen)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def selected_provider(legacy):
    if legacy == 'fake':
        return legacy
    chosen = current()
    return chosen.provider if chosen is not None else legacy


def configure(provider, configuration):
    if provider == 'fake':
        return configuration
    chosen = current()
    return _selected_configuration(provider, configuration, chosen)


def _selected_configuration(provider, configuration, chosen):
    if chosen is None or chosen.provider != provider:
        return configuration
    _, capabilities, inference, endpoint = configuration
    inference = dict(inference)
    if chosen.reasoning is not None:
        inference['reasoning'] = chosen.reasoning
    return chosen.model, capabilities, inference, endpoint


def file_bytes(root=None):
    import settings
    from bounded_io import read_stable_bytes

    try:
        return read_stable_bytes(settings.settings_path(root), settings.MAX_SETTINGS_FILE_BYTES,
                                 label='settings file')
    except FileNotFoundError:
        return b''


def _render(raw, chosen):
    import settings
    import tomlkit

    settings._parsed_bytes(raw, settings.SETTINGS_FILE_NAME)
    document = tomlkit.parse(raw.decode('utf-8'))
    table = document.setdefault('llm', tomlkit.table())
    table.update(selection_table(chosen))
    _remove_unused_reasoning(table, chosen)
    rendered = tomlkit.dumps(document).encode('utf-8')
    if len(rendered) > settings.MAX_SETTINGS_FILE_BYTES:
        raise ValueError('model selection exceeds the shared settings-file byte bound')
    settings._parsed_bytes(rendered, settings.SETTINGS_FILE_NAME)
    return rendered


def save(chosen, *, expected, root=None):
    """Save after verification, refusing concurrent edits; no provider call under lock."""
    import settings
    from markdown_transaction import active_or_legacy_coordinator

    path = settings.settings_path(root)
    vault = path.parent.resolve(strict=True)
    state = Path(os.environ.get('LLM_WIKI_STATE_ROOT', str(vault)))
    coordinator = active_or_legacy_coordinator(vault, state)
    rendered = _render(expected, chosen)
    with coordinator.writer_gate():
        _publish(coordinator, path, expected, rendered)
    return chosen


def _publish(coordinator, path, expected, rendered):
    from memory_state import atomic_write
    from reliable_memory import begin_immediate

    with coordinator._connect() as database, begin_immediate(database):
        coordinator._assert_writer_ownership(database)
        if file_bytes(path.parent) != expected:
            raise ValueError('settings changed during model verification; choose again')
        atomic_write(path, rendered.decode('utf-8'))


def _remove_unused_reasoning(table, chosen):
    if chosen.reasoning is None:
        table.pop('reasoning', None)


def settings_root(default):
    return _SETTINGS_ROOT.get() or default


@contextmanager
def using_root(root):
    token = _SETTINGS_ROOT.set(root)
    try:
        yield
    finally:
        _SETTINGS_ROOT.reset(token)
