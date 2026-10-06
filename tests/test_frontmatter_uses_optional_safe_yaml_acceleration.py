"""The common metadata parser accelerates safely without changing old outcomes."""
import corpus_snapshot as cs
import pytest
import yaml


def test_missing_c_extension_preserves_python_loader(monkeypatch):
    monkeypatch.delattr(yaml, 'CSafeLoader', raising=False)
    assert cs._frontmatter_mapping(b'type: concept\n') == ({'type': 'concept'}, None)


def test_c_yaml_error_uses_original_python_classification(monkeypatch):
    accelerator = getattr(yaml, 'CSafeLoader', object())
    original = yaml.load
    calls = []

    def load(text, Loader):
        calls.append(Loader)
        if Loader is accelerator:
            raise yaml.parser.ParserError('controlled accelerator error')
        return original(text, Loader=Loader)

    monkeypatch.setattr(yaml, 'CSafeLoader', accelerator, raising=False)
    monkeypatch.setattr(yaml, 'load', load)
    assert cs._frontmatter_mapping(b'title: Fix: x\n') == ({}, 'frontmatter is not valid YAML (ScannerError)')
    assert calls == [accelerator, yaml.SafeLoader]


def test_unexpected_accelerator_failure_is_not_hidden(monkeypatch):
    accelerator = getattr(yaml, 'CSafeLoader', object())
    original = yaml.load

    def load(text, Loader):
        if Loader is accelerator:
            raise RuntimeError('controlled unexpected failure')
        return original(text, Loader=Loader)

    monkeypatch.setattr(yaml, 'CSafeLoader', accelerator, raising=False)
    monkeypatch.setattr(yaml, 'load', load)
    with pytest.raises(RuntimeError, match='controlled unexpected failure'):
        cs._frontmatter_mapping(b'type: concept\n')


@pytest.mark.parametrize('raw', [b'', b'- x\n', b'title: Fix: x\n', b'title: bell \x07\n', b'!!python/object/apply:os.system ["forbidden"]', b'!unknown x', b'a: 1\na: 2\n', b'date: 2026-10-06\nbool: on\nint: 012\n', b'a: &x [1]\nb: *x\n', b'a: {x: 1}\n<<: {b: 2}\n', b'a: "\\q"\n', b'a: [1\n', b'\xff', b'---\na: 1\n---\nb: 2\n'])
def test_legacy_safe_outcomes_are_preserved(raw, monkeypatch):
    actual = cs._frontmatter_mapping(raw)
    monkeypatch.delattr(yaml, 'CSafeLoader', raising=False)
    assert actual == cs._frontmatter_mapping(raw)


def test_frontmatter_boundaries_and_deadline_remain_original():
    raw = b'---\ntype: concept\n---\nbody\n'
    assert cs.read_frontmatter(raw).body_start == raw.index(b'body')
    with pytest.raises(TimeoutError):
        cs.read_frontmatter(raw, deadline=0.0)
    assert cs.read_frontmatter(b'---\ntype: concept\n').problem == 'unterminated YAML frontmatter'
