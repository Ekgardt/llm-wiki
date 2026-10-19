#!/usr/bin/env python3
"""Select one subscription model for all memory work in llm-wiki.toml.

Catalog entries are candidates, not entitlement evidence. A selected model must
complete one protected short request before its choice is atomically saved.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import llm_client  # noqa: E402

# The variable each provider reads its model from (llm_client's configurations).
MODEL_VARIABLES = {
    "claude": "MEMORY_CLAUDE_MODEL",
    "codex": "MEMORY_CODEX_MODEL",
    "openai": "MEMORY_LLM_MODEL",
    "ollama": "MEMORY_LLM_MODEL",
}
# Claude Code's documented aliases (https://code.claude.com/docs/en/model-config);
# there is no command that lists them for an account.
CLAUDE_ALIASES = ("sonnet", "opus", "haiku", "fable")
PROBE_PROMPT = "Reply with the single word OK."


def _say(message: str) -> None:
    print(message, file=sys.stderr)


# --- The provider ------------------------------------------------------------------


def detected_provider() -> llm_client.ProviderDescriptor | None:
    """The first candidate `llm_client` would call, by its own order and probes."""
    candidates = llm_client.provider_candidates(llm_client.forced_provider())
    return next((c for c in candidates if llm_client.probe_candidate(c)), None)


def refusal(descriptor: llm_client.ProviderDescriptor, model: str) -> str | None:
    """None when the model answers one short question, else the provider's reason."""
    result = llm_client.call_candidate(replace(descriptor, model=model), PROBE_PROMPT, "", available=True)
    if result.text is not None:
        return None
    return result.failure_detail or result.failure_class or "no answer"


# --- What each provider offers ---------------------------------------------------------


def _claude_models(descriptor: llm_client.ProviderDescriptor) -> list[str]:
    """Each alias asked once, in parallel: one call's deadline bounds them all."""
    with ThreadPoolExecutor(max_workers=len(CLAUDE_ALIASES)) as pool:
        refusals = list(pool.map(lambda alias: refusal(descriptor, alias), CLAUDE_ALIASES))
    return [alias for alias, refused in zip(CLAUDE_ALIASES, refusals) if refused is None]


# The keys a listing names a model under: Codex's catalog, OpenAI, Ollama.
_NAME_KEYS = ("slug", "id", "model", "name")


def _is_name(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _model_name(item: object) -> str | None:
    if not isinstance(item, dict):
        return None
    return next(filter(_is_name, (item.get(key) for key in _NAME_KEYS)), None)


def _named(items: object) -> list[str]:
    if not isinstance(items, list):
        return []
    return [name for name in map(_model_name, items) if name]


def _codex_catalog():
    """Ask the installed CLI; hidden entries are not menu choices."""
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        subprocess.run(
            [llm_client._find_codex_binary() or "codex", "debug", "models"],
            stdout=output, stderr=errors, timeout=llm_client._timeout_s(), check=True,
        )
        size = output.tell()
        if size > _catalog_byte_budget():
            raise ValueError('provider catalog exceeds the metadata byte budget')
        output.seek(0)
        payload = json.load(output)
    records = payload.get('models') if isinstance(payload, dict) else payload
    return _visible_models(records)


def _visible_models(records):
    if not isinstance(records, list):
        raise ValueError('provider catalog is not a model list')
    return [row for row in records if isinstance(row, dict) and row.get('visibility', 'list') == 'list']


def _codex_models(_descriptor):
    return _named(_codex_catalog())


def _catalog_byte_budget():
    from settings import setting_value

    return setting_value("provider.catalog_max_bytes")


def _fetched_json(request: urllib.request.Request) -> object:
    with llm_client.open_provider_request(request, timeout=llm_client._timeout_s()) as response:
        budget = _catalog_byte_budget()
        payload = response.read(budget + 1)
        if len(payload) > budget:
            raise ValueError('provider catalog exceeds the metadata byte budget')
        return json.loads(payload.decode("utf-8"))


def _ollama_models(descriptor: llm_client.ProviderDescriptor) -> list[str]:
    """The models on this machine, as `ollama list` shows them."""
    payload = _fetched_json(urllib.request.Request(llm_client._ollama_api_url(descriptor._endpoint, "tags")))
    local = [item for item in payload.get("models", []) if llm_client._is_local_ollama_entry(item)]
    return _named(local)


def _openai_models(descriptor: llm_client.ProviderDescriptor) -> list[str]:
    key = os.environ.get("MEMORY_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    request = urllib.request.Request(
        f"{descriptor._endpoint.rstrip('/')}/models", headers={"Authorization": f"Bearer {key}"}
    )
    payload = _fetched_json(request)
    return sorted(_named(payload.get("data") if isinstance(payload, dict) else None))


_LISTINGS: dict[str, Callable[[llm_client.ProviderDescriptor], list[str]]] = {
    "claude": _claude_models,
    "codex": _codex_models,
    "ollama": _ollama_models,
    "openai": _openai_models,
}


def offered_models(descriptor: llm_client.ProviderDescriptor) -> list[str]:
    """What the provider offers; a listing that fails is named and offers nothing."""
    try:
        return _LISTINGS[descriptor.provider](descriptor)
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError) as error:
        _say(f"choose_model: {descriptor.provider} did not list its models ({type(error).__name__}: {error})")
        return []


# --- One vault-wide selection ---------------------------------------------------------


def _discovery_provider(provider):
    import model_selection

    with model_selection.using(None):
        candidates = llm_client.provider_candidates(provider)
        return next((candidate for candidate in candidates if llm_client.probe_candidate(candidate)), None)


def _provider(args):
    import model_selection

    chosen = model_selection.current(args.root)
    requested = args.provider or (chosen.provider if chosen else llm_client.forced_provider())
    descriptor = _discovery_provider(requested)
    if descriptor is None:
        raise ValueError('no provider is available; sign in to the subscription CLI first')
    if descriptor.provider not in MODEL_VARIABLES:
        raise ValueError('this provider adapter cannot select an explicit model')
    return descriptor


def _show(args):
    import model_selection

    chosen = model_selection.current(args.root)
    if chosen is None:
        return {'status': 'unconfigured', 'source': str(args.root / 'llm-wiki.toml')}
    return {**model_selection.selection_table(chosen), 'status': 'configured',
            'source': str(args.root / 'llm-wiki.toml')}


def _list(args):
    descriptor = _provider(args)
    models = offered_models(descriptor)
    if not models:
        raise ValueError('provider returned no selectable models')
    return {'provider': descriptor.provider, 'models': models,
            'access': 'catalog candidates; selection is verified with one short request'}


def _codex_reasoning(model, requested):
    record = _catalog_model(model)
    if record is None:
        raise ValueError('selected model is absent from the visible provider catalog')
    effort = requested or record.get('default_reasoning_level')
    levels = [item.get('effort') for item in record.get('supported_reasoning_levels', [])]
    if effort not in levels:
        raise ValueError(f'unsupported reasoning setting; provider offers {levels}')
    return effort


def _catalog_model(model):
    return next((row for row in _codex_catalog() if _model_name(row) == model), None)


def _chosen(descriptor, model, reasoning):
    import model_selection

    if descriptor.provider == 'codex':
        reasoning = _codex_reasoning(model, reasoning)
    return model_selection.selection(f'{descriptor.provider}/{model}', reasoning)


def _verified_selection(chosen):
    import model_selection

    with model_selection.using(chosen):
        descriptor, = llm_client.provider_candidates(chosen.provider)
        refused = refusal(descriptor, chosen.model)
    if refused is not None:
        raise ValueError(f'model verification failed; selection unchanged: {refused}')


def _save_choice(args, descriptor, model, expected):
    import model_selection

    chosen = _chosen(descriptor, model, args.reasoning)
    _verified_selection(chosen)
    model_selection.save(chosen, expected=expected, root=args.root)
    return {**model_selection.selection_table(chosen), 'provider': chosen.provider,
            'status': 'saved', 'source': str(args.root / 'llm-wiki.toml')}


def _set(args):
    import model_selection

    expected = model_selection.file_bytes(args.root)
    provider, separator, model = (args.choice or '').partition('/')
    if not separator:
        raise ValueError('set requires provider/model, for example codex/gpt-6.1-sol')
    args.provider = provider
    _keep_reasoning(args, _current_for(provider, args.root), provider)
    return _save_choice(args, _provider(args), model, expected)


def _menu_choice(models, current):
    _say('Catalog candidates (access is checked after selection):')
    for number, model in enumerate(models, start=1):
        _say(f'  {number}. {model}')
    answer = input(f'Model number or name [{current or "no selection"}]: ').strip()
    return _menu_answer(answer, models, current)


def _menu_answer(answer, models, current):
    if not answer:
        return current
    if answer.isdigit() and 1 <= int(answer) <= len(models):
        return models[int(answer) - 1]
    return answer


def _select(args):
    import model_selection

    expected = model_selection.file_bytes(args.root)
    descriptor = _provider(args)
    chosen = _current_for(descriptor.provider, args.root)
    model = _requested_model(args, descriptor, chosen)
    if sys.stdin.isatty() and not args.model:
        model = _menu_choice(offered_models(descriptor), model)
    if not model:
        return {'status': 'unconfigured', 'model': '', 'provider': descriptor.provider}
    _keep_reasoning(args, chosen, descriptor.provider)
    return _save_choice(args, descriptor, model, expected)


def _requested_model(args, descriptor, chosen):
    inherited = os.environ.get(MODEL_VARIABLES[descriptor.provider], '')
    return args.model or getattr(chosen, 'model', inherited)


def _keep_reasoning(args, chosen, provider):
    inherited = _initial_reasoning(provider)
    args.reasoning = args.reasoning or getattr(chosen, 'reasoning', inherited)


def _initial_reasoning(provider):
    if provider == 'codex':
        return os.environ.get('MEMORY_CODEX_REASONING')
    return None


def _current_for(provider, root):
    import model_selection

    chosen = model_selection.current(root)
    if chosen is not None and chosen.provider == provider:
        return chosen
    return None


_COMMANDS = {'show': _show, 'list': _list, 'set': _set, 'select': _select}


def _arguments(argv):
    import settings

    parser = argparse.ArgumentParser(description='Choose one model for every LLM Wiki call.')
    parser.add_argument('command', nargs='?', default='select', choices=tuple(_COMMANDS))
    parser.add_argument('choice', nargs='?', help='Provider/model identifier for set')
    parser.add_argument('--provider', choices=tuple(MODEL_VARIABLES))
    parser.add_argument('--reasoning', help='Reasoning level offered by the selected model')
    parser.add_argument('--model', help='Installer-compatible explicit model selection')
    parser.add_argument('--home', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--root', type=Path, default=settings.settings_path().parent)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    from model_selection import using_root

    try:
        args = _arguments(argv)
        with using_root(args.root):
            print(json.dumps(_COMMANDS[args.command](args)))
    except (OSError, ValueError, RuntimeError) as error:
        _say(f'choose_model: {error}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
