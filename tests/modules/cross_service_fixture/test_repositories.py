"""Exercise real local commits and the builder's existing refusal boundary."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from benchmark import build_cross_service_fixture as builder
from tests.module_context import context_reader
from tests.slow_machine import LONG_TIMEOUT

ROOT = Path(__file__).resolve().parents[3]
read_source = context_reader(ROOT, ROOT / 'docs/modules/cross-service-fixture-context.json')


@pytest.fixture(autouse=True)
def isolated_git_configuration(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith('GIT_'):
            monkeypatch.delenv(name)
    config = tmp_path / 'empty-git-config'
    config.write_text('', encoding='utf-8')
    template = tmp_path / 'empty-git-template'
    template.mkdir()
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(config))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_TEMPLATE_DIR', str(template))


def git_output(repository, *arguments):
    return subprocess.run(
        ['git', '-C', str(repository), *arguments], check=True,
        capture_output=True, timeout=LONG_TIMEOUT,
    ).stdout


def assert_committed_sources(repository, expected):
    names = git_output(repository, 'ls-tree', '-r', '--name-only', 'HEAD').decode().splitlines()
    assert names == sorted(expected)
    assert git_output(repository, 'status', '--porcelain') == b''
    assert git_output(repository, 'rev-list', '--count', 'HEAD').strip() == b'1'
    for relative, content in expected.items():
        assert (repository / relative).read_text(encoding='utf-8') == content
        assert git_output(repository, 'show', f'HEAD:{relative}') == (repository / relative).read_bytes()


def test_build_creates_two_committed_repositories(tmp_path):
    output = tmp_path / 'стенд с пробелами'
    service, client = builder.build(output)
    assert (service, client) == (output / 'service-repo', output / 'client-repo')
    assert_committed_sources(service, builder.SERVICE_FILES)
    assert_committed_sources(client, builder.CLIENT_FILES)
    assert git_output(service, 'remote') == git_output(client, 'remote') == b''


def test_cli_reports_the_actual_created_paths(tmp_path):
    output = tmp_path / 'cli-fixture'
    result = subprocess.run(
        [sys.executable, str(ROOT / 'benchmark/build_cross_service_fixture.py'), '--out', str(output)],
        check=True, capture_output=True, text=True, encoding='utf-8', timeout=LONG_TIMEOUT,
    )
    assert result.stdout.splitlines() == [
        f'service: {output.resolve() / "service-repo"}',
        f'client:  {output.resolve() / "client-repo"}',
    ]
    assert_committed_sources(output / 'client-repo', builder.CLIENT_FILES)


def test_existing_first_directory_is_unchanged(tmp_path):
    output = tmp_path / 'existing-service'
    service = output / 'service-repo'
    service.mkdir(parents=True)
    marker = service / 'keep.txt'
    marker.write_bytes(b'existing user content')
    with pytest.raises(FileExistsError):
        builder.build(output)
    assert marker.read_bytes() == b'existing user content'
    assert set(service.iterdir()) == {marker}
    assert not (output / 'client-repo').exists()


def test_second_directory_refusal_preserves_completed_first_repository(tmp_path):
    output = tmp_path / 'existing-client'
    client = output / 'client-repo'
    client.mkdir(parents=True)
    marker = client / 'keep.txt'
    marker.write_bytes(b'existing client')
    with pytest.raises(FileExistsError):
        builder.build(output)
    assert marker.read_bytes() == b'existing client'
    assert set(client.iterdir()) == {marker}
    assert_committed_sources(output / 'service-repo', builder.SERVICE_FILES)


def test_task_gold_names_real_fixture_routes_and_the_explicit_boundary():
    tasks = json.loads(read_source(ROOT / 'benchmark/code-parity-cross-service-v1.json'))
    assert [task['id'] for task in tasks['tasks']] == ['X01', 'X02']
    assert tasks['tasks'][0]['gold']['must'] == ['create_order', ['POST /orders', '/orders']]
    assert tasks['tasks'][1]['gold']['must'] == ['place_order']
    assert '@router.post("/orders")\ndef create_order(payload):' in builder.SERVICE_FILES['orders/api.py']
    assert 'return place_order(payload, 1)' in builder.SERVICE_FILES['orders/api.py']
    assert 'requests.post("https://orders.internal/orders", json=basket)' in builder.CLIENT_FILES['shop/checkout.py']
    assert 'expected to stop at the boundary' in '\n'.join(tasks['notes'])
