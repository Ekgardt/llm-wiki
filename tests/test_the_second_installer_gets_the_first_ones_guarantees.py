"""Audit 3, B21: the language-server installer gets the Pyright one's posture.

No proxy, no redirect, one absolute deadline, streamed to disk, one install
lock, a swept scratch directory, an existing install validated rather than
refused, and components an operator can supply from disk. Research:
`docs/research/2026-09-17-inst-the-second-installer-gets-the-first-ones-guarantees.md`.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import platform
import random
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import install_language_server as installer  # noqa: E402
import pinned_download  # noqa: E402
from lsp_server_profile import (  # noqa: E402
    READINESS_INITIALIZED,
    LanguageServerProfile,
    PlatformArtifact,
    ServerComponent,
    freeze_profile_value,
    normalized_platform,
)
from reliable_memory import canonical_json_bytes  # noqa: E402

from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402

SERVER = b"console.log('managed');\n"
PACKAGE = b'{"name":"x","version":"1"}\n'
# Incompressible and larger than one transfer chunk, so the *compressed*
# archive is bigger than one read and a streamed download needs several.
FILLER = random.Random(20260917).randbytes(pinned_download.CHUNK_BYTES * 3)
SERVER_URL = "https://example.invalid/x-1.tgz"
COMPONENT_URL = "https://example.invalid/x-extra-1.tar.gz"


def _tarball(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in sorted(members.items()):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _integrity(content: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(content).digest()).decode("ascii")


SERVER_ARCHIVE = _tarball(
    {
        "package/main.js": SERVER,
        "package/package.json": PACKAGE,
        "package/filler": FILLER,
    }
)
COMPONENT_ARCHIVE = _tarball({"extra/prefix/tool": b"tool\n"})


def _here() -> tuple[str, str]:
    return normalized_platform(platform.system(), platform.machine())


def _component() -> ServerComponent:
    system, machine = _here()
    return ServerComponent(
        name="extra",
        prefix=Path("toolchain"),
        artifacts=(
            PlatformArtifact(
                system=system,
                machine=machine,
                url=COMPONENT_URL,
                integrity=_integrity(COMPONENT_ARCHIVE),
                size=len(COMPONENT_ARCHIVE),
            ),
        ),
        strip_components=2,
    )


def _profile(**overrides) -> LanguageServerProfile:
    fields = {
        "name": "x",
        "language_ids": ("x",),
        "file_suffixes": (".x",),
        "version": "1",
        "package_url": SERVER_URL,
        "package_integrity": _integrity(SERVER_ARCHIVE),
        "server_relative": Path("package/main.js"),
        "managed_relative_root": Path("cache/code-tools/x/1"),
        "install_manifest_schema": "x/v1",
        "node_major": 22,
        "launch_flags": ("--stdio",),
        "server_notifications": frozenset(),
        "configuration": freeze_profile_value({}),
        "initialization_options": freeze_profile_value({}),
        "readiness": READINESS_INITIALIZED,
        "degradation_prefix": "x",
    }
    fields.update(overrides)
    return LanguageServerProfile(**fields)


class _Socket:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)


class _Response:
    """Enough of `http.client.HTTPResponse` for the pinned transfer to run."""

    def __init__(self, content: bytes, url: str) -> None:
        self.content = content
        self.url = url
        self.status = 200
        self.headers = {"Content-Length": str(len(content))}
        self.socket = _Socket()
        self.fp = type("_Fp", (), {"raw": type("_Raw", (), {})()})()
        self.fp.raw._sock = self.socket
        self.offset = 0
        self.reads = 0

    def geturl(self) -> str:
        return self.url

    def read1(self, size: int) -> bytes:
        self.reads += 1
        chunk = self.content[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exception: object) -> bool:
        return False


def _serving(monkeypatch: pytest.MonkeyPatch, bodies: dict[str, bytes]) -> list:
    served: list[_Response] = []

    def open_url(request: urllib.request.Request, *, timeout: float) -> _Response:
        response = _Response(bodies[request.full_url], request.full_url)
        served.append(response)
        return response

    monkeypatch.setattr(pinned_download, "open_pinned_url", open_url)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("the default opener was used"),
    )
    return served


def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pinned_download,
        "open_pinned_url",
        lambda *args, **kwargs: pytest.fail("the network was used for a local archive"),
    )


def _install(tmp_path: Path, profile: LanguageServerProfile, **kwargs) -> Path:
    return installer.install_language_server(
        profile, state_root=tmp_path / "state", **kwargs
    )


def _lock_file(tmp_path: Path) -> Path:
    return tmp_path / ".install-x-lock"


# ---------------------------------------------------------------------------
# How the bytes are fetched
# ---------------------------------------------------------------------------


class _StubOpener:
    def open(self, request: object, timeout: float | None = None) -> object:
        return request


def test_the_pinned_opener_carries_no_proxy_and_refuses_a_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []

    def build_opener(*handlers: object) -> _StubOpener:
        captured.extend(handlers)
        return _StubOpener()

    monkeypatch.setattr(pinned_download.urllib.request, "build_opener", build_opener)

    pinned_download.open_pinned_url(
        urllib.request.Request(SERVER_URL), timeout=SHORT_TIMEOUT
    )

    kinds = [type(handler) for handler in captured]
    assert (
        urllib.request.ProxyHandler in kinds,
        pinned_download.RejectRedirect in kinds,
    ) == (True, True)
    assert captured[0].proxies == {}


def test_the_archive_is_streamed_in_chunks_instead_of_read_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = _serving(monkeypatch, {SERVER_URL: SERVER_ARCHIVE})

    root = _install(tmp_path, _profile())

    assert (root / "package/main.js").read_bytes() == SERVER
    assert served[0].reads > 2
    assert len(served[0].socket.timeouts) == served[0].reads


def test_a_server_that_drips_runs_out_of_the_install_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serving(monkeypatch, {SERVER_URL: SERVER_ARCHIVE})
    clock = iter([100.0 + step * 30.0 for step in range(64)])
    monkeypatch.setattr(installer.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(pinned_download.time, "monotonic", lambda: next(clock))

    with pytest.raises(TimeoutError):
        _install(tmp_path, _profile(), deadline=150.0)

    assert not (tmp_path / "state/cache/code-tools/x/1").exists()


# ---------------------------------------------------------------------------
# One installer at a time, and what an abrupt death leaves
# ---------------------------------------------------------------------------


def test_a_second_installer_is_refused_by_the_lock_its_sibling_holds(
    tmp_path: Path,
) -> None:
    lock = _lock_file(tmp_path)
    held = installer._acquire_lock(lock, time.monotonic() + SHORT_TIMEOUT)

    with pytest.raises(installer.InstallError):
        installer._acquire_lock(lock, time.monotonic())

    installer._release_lock(held)
    assert not lock.exists()


def test_a_lock_whose_owner_is_gone_is_reclaimed(tmp_path: Path) -> None:
    lock = _lock_file(tmp_path)
    lock.write_bytes(
        canonical_json_bytes(
            {
                "acquired_at_unix_ns": 0,
                "nonce": "a" * 32,
                "pid": os.getpid(),
                "process_start": "a process that started at another time",
            }
        )
    )

    claimed = installer._acquire_lock(lock, time.monotonic() + SHORT_TIMEOUT)

    assert claimed.nonce != "a" * 32
    installer._release_lock(claimed)
    assert not lock.exists()


def test_scratch_an_abrupt_death_left_behind_is_swept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serving(monkeypatch, {SERVER_URL: SERVER_ARCHIVE})
    parent = tmp_path / "state/cache/code-tools/x"
    abandoned = parent / ".install-x-killed"
    abandoned.mkdir(parents=True)
    (abandoned / "half-a-toolchain").write_bytes(b"z" * 4096)

    root = _install(tmp_path, _profile())

    assert not abandoned.exists()
    assert (root / "package/main.js").is_file()


# ---------------------------------------------------------------------------
# An install that is already there
# ---------------------------------------------------------------------------


def test_an_install_already_there_is_validated_and_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serving(monkeypatch, {SERVER_URL: SERVER_ARCHIVE})
    first = _install(tmp_path, _profile())
    _offline(monkeypatch)

    second = _install(tmp_path, _profile())

    assert second == first
    assert (second / "package/main.js").read_bytes() == SERVER


def test_an_install_that_no_longer_matches_its_receipt_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serving(monkeypatch, {SERVER_URL: SERVER_ARCHIVE})
    root = _install(tmp_path, _profile())
    (root / "package/main.js").write_bytes(b"something else\n")
    _offline(monkeypatch)

    with pytest.raises(installer.InstallError) as error:
        _install(tmp_path, _profile())

    assert "receipt" in str(error.value)


# ---------------------------------------------------------------------------
# Offline: every archive the operator already has
# ---------------------------------------------------------------------------


def test_every_component_may_come_from_disk_so_rust_can_install_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = tmp_path / "server.tgz"
    server.write_bytes(SERVER_ARCHIVE)
    component = tmp_path / "component.tar.gz"
    component.write_bytes(COMPONENT_ARCHIVE)
    _offline(monkeypatch)

    root = _install(
        tmp_path,
        _profile(components=(_component(),)),
        server_artifact=server,
        component_artifacts={"extra": component},
    )

    assert (root / "toolchain/tool").read_bytes() == b"tool\n"
    assert (root / "package/main.js").read_bytes() == SERVER
