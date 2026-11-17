"""Helpers shared by test_compile_execution_inventory.py and the test files importing them."""

import compile_controller as controller
import pytest
from llm_wiki import compile_controller_dependencies as controller_dependencies


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.delenv('LLM_WIKI_DLP_POLICY', raising=False)
    monkeypatch.delenv('LLM_WIKI_COMPILE_VALIDATION_RETRIES', raising=False)
    return tmp_path


def _components(monkeypatch, root):
    product = root / 'product'
    product.write_bytes(b'original')
    monkeypatch.setattr(controller_dependencies, 'compile_product_dependency_manifest',
        lambda root, parameters, **kw: controller.compile_dependency_manifest({'product': product}, parameters))
    monkeypatch.setattr(controller_dependencies, 'compile_package_dependency_manifest', lambda **kw: {'package': 'fixture'})
    monkeypatch.setattr(controller_dependencies, 'compile_runtime_dependency_manifest', lambda **kw: {'python': 'fixture'})
    return product
