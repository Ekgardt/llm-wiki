"""Helpers shared by test_codex_installed_provider_bundle_reaches_launch.py and the test files importing them."""

import os

import integration_hook_config as hooks

BUNDLE = {'MEMORY_LLM_PROVIDER': 'codex', 'MEMORY_CODEX_MODEL': 'gpt-6-luna',
          'MEMORY_CODEX_REASONING': 'max'}


def _script(root, name):
    path = root / 'scripts' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('import json,os\n' + f'KEYS={hooks.PROVIDER_ENV_KEYS!r}\n' +
                    'print(json.dumps({k:v for k,v in os.environ.items() if k in KEYS}))\n',
                    encoding='utf-8')
    return path


def _host_environment():
    env = dict(os.environ)
    for key in hooks.PROVIDER_ENV_KEYS:
        env.pop(key, None)
    return env
