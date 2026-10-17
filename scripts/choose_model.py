#!/usr/bin/env python3
"""Choose the model the memory pipeline calls, from those the user's provider offers.

The installers run this before the install transaction and export the answer, so
`integration_hook_config.provider_environment` persists it into the hooks' env and
the scheduler units. The provider is the one `llm_client` would pick. Claude has no
model listing, so its documented aliases are each asked one short question and only
those that answer are offered; Codex, Ollama and OpenAI list theirs. OpenCode's
model is set in OpenCode's own configuration and is not chosen here.
See docs/research/2026-09-29-the-installer-asks-which-model.md.

Prints one JSON object on stdout: provider, variable, model ("" is the provider's
own default) and source (chosen, flag, previous, default, none). Messages go to
stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
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
# A local CLI answering a local question, as llm_client's `claude --help` probe.
LISTING_TIMEOUT_SECONDS = 30
# Every listing reply is read to this size at most, as llm_client reads Ollama's tags.
MAX_LISTING_BYTES = 1024 * 1024


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


def _codex_models(_descriptor: llm_client.ProviderDescriptor) -> list[str]:
    """`codex debug models` prints the catalog as JSON: a list, or an object holding one."""
    completed = subprocess.run(
        [llm_client._find_codex_binary() or "codex", "debug", "models"],
        capture_output=True,
        text=True,
        timeout=LISTING_TIMEOUT_SECONDS,
        check=True,
    )
    payload = json.loads(completed.stdout[:MAX_LISTING_BYTES])
    return _named(payload.get("models") if isinstance(payload, dict) else payload)


def _fetched_json(request: urllib.request.Request) -> object:
    with llm_client.open_provider_request(request, timeout=LISTING_TIMEOUT_SECONDS) as response:
        return json.loads(response.read(MAX_LISTING_BYTES).decode("utf-8"))


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


# --- The current choice ---------------------------------------------------------------


def _settings_env(home: Path) -> dict[str, object]:
    """The env the installer wrote into Claude's settings, where the last choice lives."""
    try:
        settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    env = settings.get("env") if isinstance(settings, dict) else None
    return env if isinstance(env, dict) else {}


def previous_choice(variable: str, home: Path) -> str:
    """This shell's value, else the one the last install persisted, else ""."""
    value = os.environ.get(variable) or _settings_env(home).get(variable) or ""
    return str(value).strip()


# --- Asking ---------------------------------------------------------------------------


def _menu(models: Sequence[str], current: str) -> str:
    lines = [f"  {number}. {model}" for number, model in enumerate(models, start=1)]
    shown = current or "the provider's own default"
    return "\n".join([*lines, f"Enter keeps {shown}; a number or any model name chooses."])


def resolved_answer(answer: str, models: Sequence[str], current: str) -> str:
    """Enter keeps the current choice, a listed number picks it, anything else is a name."""
    answer = answer.strip()
    if not answer:
        return current
    if answer.isdigit() and 1 <= int(answer) <= len(models):
        return models[int(answer) - 1]
    return answer


def _verified(descriptor: llm_client.ProviderDescriptor, model: str) -> str | None:
    """The provider's default needs no check; a named model gets one short call."""
    if not model:
        return None
    return refusal(descriptor, model)


def chosen_interactively(
    descriptor: llm_client.ProviderDescriptor,
    models: Sequence[str],
    current: str,
    read: Callable[[str], str] = input,
) -> str:
    """Ask until the person names a model the provider answers with, or keeps the current one."""
    _say(f"Models {descriptor.provider} offers you:\n{_menu(models, current)}")
    while True:
        model = resolved_answer(read("Model: "), models, current)
        refused = _verified(descriptor, model)
        if refused is None:
            return model
        _say(f"{model} is not available to you: {refused}")


# --- Deciding -------------------------------------------------------------------------


def _without_terminal(descriptor, flag: str | None, current: str) -> tuple[str, str]:
    """No one to ask: the flag (verified), else the last choice, else the default."""
    if flag:
        return _flagged(descriptor, flag)
    return (current, "previous") if current else ("", "default")


def _flagged(descriptor: llm_client.ProviderDescriptor, flag: str) -> tuple[str, str]:
    refused = refusal(descriptor, flag)
    if refused is not None:
        raise SystemExit(f"choose_model: --model {flag} is not available: {refused}")
    return flag, "flag"


def decide(
    descriptor: llm_client.ProviderDescriptor,
    home: Path,
    flag: str | None,
    interactive: bool,
    read: Callable[[str], str] = input,
) -> dict[str, str]:
    variable = MODEL_VARIABLES[descriptor.provider]
    current = previous_choice(variable, home)
    if interactive and not flag:
        model = chosen_interactively(descriptor, offered_models(descriptor), current, read)
        return _answer(descriptor.provider, variable, model, "chosen")
    model, source = _without_terminal(descriptor, flag, current)
    return _answer(descriptor.provider, variable, model, source)


def _answer(provider: str, variable: str, model: str, source: str) -> dict[str, str]:
    return {"provider": provider, "variable": variable, "model": model, "source": source}


def _nothing_to_choose(descriptor: llm_client.ProviderDescriptor | None) -> dict[str, str] | None:
    """No provider, or one whose model is not ours to set (OpenCode, the test double)."""
    if descriptor is None:
        _say("choose_model: no provider answered; the model is chosen once one is installed")
        return _answer("", "", "", "none")
    if descriptor.provider not in MODEL_VARIABLES:
        _say(f"choose_model: {descriptor.provider} sets its model in its own configuration")
        return _answer(descriptor.provider, "", "", "none")
    return None


def _report(answer: dict[str, str]) -> None:
    shown = answer["model"] or "the provider's own default"
    _say(f"choose_model: {answer['provider']} will use {shown} ({answer['source']})")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Use this model; it is checked with one short call.")
    parser.add_argument("--home", type=Path, default=Path.home())
    args = parser.parse_args(argv)
    descriptor = detected_provider()
    answer = _nothing_to_choose(descriptor) or decide(descriptor, args.home, args.model, sys.stdin.isatty())
    _report(answer)
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
