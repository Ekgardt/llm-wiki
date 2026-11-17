"""The vault's authoritative model choice, shared by every memory entrypoint."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

import settings
from llm_wiki.model_choice import selection

_UNSET = object()
_CURRENT = ContextVar("memory_model_selection", default=_UNSET)


MODEL_ENV_KEYS = frozenset((
    'MEMORY_LLM_PROVIDER', 'MEMORY_CODEX_MODEL', 'MEMORY_CODEX_REASONING',
    'MEMORY_CLAUDE_MODEL', 'MEMORY_LLM_MODEL',
))


def current(root=None):
    scoped = _CURRENT.get()
    if scoped is not _UNSET:
        return scoped
    return _current_file(root)


def _current_file(root):
    values = settings.file_values(settings.settings_path(root))
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
    return selected_configuration(provider, configuration, chosen)


def selected_configuration(provider, configuration, chosen):
    if chosen is None or chosen.provider != provider:
        return configuration
    _, capabilities, inference, endpoint = configuration
    inference = dict(inference)
    if chosen.reasoning is not None:
        inference['reasoning'] = chosen.reasoning
    return chosen.model, capabilities, inference, endpoint
