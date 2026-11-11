"""Classification does not authorize deletion or broaden job checkouts."""
from pathlib import Path
from types import SimpleNamespace

import ephemeral_paths
import pytest


def test_the_temporary_and_job_directories_are_ephemeral(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "host"))
    temporary = tmp_path / "system-temp"
    permanent = tmp_path / "workspaces" / "vault"
    monkeypatch.setattr(ephemeral_paths, "tempfile", SimpleNamespace(gettempdir=lambda: str(temporary)))
    job = tmp_path / "host" / "jobs" / "abc" / "tmp" / "clean"

    roots = ephemeral_paths.ephemeral_roots()
    verdicts = (
        ephemeral_paths.is_under(temporary / "x", roots),
        ephemeral_paths.is_under(job, roots),
        ephemeral_paths.is_under(permanent, roots),
    )

    assert verdicts == (True, True, False)



def test_checkout_predicate_is_narrower_than_temporary_roots(tmp_path, monkeypatch):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'host'))
    assert ephemeral_paths.is_throwaway_checkout(tmp_path / 'host/jobs/a')
    assert not ephemeral_paths.is_throwaway_checkout(tmp_path / 'elsewhere')
    assert not ephemeral_paths.is_throwaway_checkout(tmp_path / 'host/jobs-extra')


def test_default_host_directory_follows_home(monkeypatch, tmp_path):
    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    assert ephemeral_paths.host_directory() == tmp_path / '.claude'


def test_spellings_keep_order_and_remove_repeated_roots(tmp_path):
    assert ephemeral_paths.spellings((tmp_path, tmp_path)) == (tmp_path,)
    assert ephemeral_paths.is_under(tmp_path, (tmp_path,))
    assert not ephemeral_paths.is_under(tmp_path, ())


def _resolution_failure(self):
    raise OSError('filesystem cannot resolve the path')


def test_resolution_oserror_preserves_absolute_fallback(monkeypatch):
    relative = Path('relative/child')
    expected = relative.absolute()
    monkeypatch.setattr(Path, 'resolve', _resolution_failure)
    assert ephemeral_paths.resolved(relative) == expected


def test_containment_follows_actual_symlink_target(tmp_path):
    target = tmp_path / 'outside'
    target.mkdir()
    root = tmp_path / 'root'
    root.mkdir()
    link = root / 'alias'
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f'symlink unavailable: {exc}')
    assert not ephemeral_paths.is_under(link / 'child', (root,))
    assert ephemeral_paths.is_under(link / 'child', (target,))
