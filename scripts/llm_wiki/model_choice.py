"""The value of a model choice and the settings root it is read from; no file access."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

# These existing adapters can carry an explicit model. OpenCode currently cannot;
# it remains discoverable in legacy auto mode, not a misleading selectable target.
SELECTABLE_PROVIDERS = ('codex', 'claude', 'openai', 'ollama')
_SETTINGS_ROOT = ContextVar("memory_model_settings_root", default=None)


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
    if not isinstance(table, Mapping) or set(table) - {'model', 'reasoning'}:
        raise ValueError(f'{source}: invalid [llm] selection fields')
    chosen = selection(table.get('model'), table.get('reasoning'))
    return {f'llm.{key}': value for key, value in selection_table(chosen).items()}


def selection_table(chosen):
    result = {'model': chosen.name}
    if chosen.reasoning is not None:
        result['reasoning'] = chosen.reasoning
    return result


def settings_root(default):
    return _SETTINGS_ROOT.get() or default


@contextmanager
def using_root(root):
    token = _SETTINGS_ROOT.set(root)
    try:
        yield
    finally:
        _SETTINGS_ROOT.reset(token)
