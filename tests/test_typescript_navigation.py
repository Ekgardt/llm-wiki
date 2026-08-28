"""Precise TypeScript navigation against a live pinned server.

Honest framing, because it matters for how these results should be read:

* The project navigated is a **fixture** (`tests/fixtures/typescript_navigation/`).
  There is no TypeScript repository on this machine -- measured 2026-08-28,
  `find / -name tsconfig.json -not -path '*/node_modules/*'` returns nothing and
  no `node_modules/typescript` exists anywhere.
* These tests need the managed artifact, which is installed by a separate,
  explicit operator action and is not present by default. Without it they skip.
  A skip here is not a pass; it means the evidence was not produced on this run.

What they do prove, when the artifact is present, is that
`lsp_profiles.TYPESCRIPT_PROFILE` is not merely well-formed data: the argv it
builds starts a real server, the initialization options it produces make that
server load the engine we pinned, its readiness policy is the difference between
a right and a wrong answer, and the locations that come back parse through the
same normalizers the Python path already uses.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import lsp_profiles
import pytest
from pyright_session import LspLocation, _location_key, _lsp_range

FIXTURE = Path(__file__).parent / "fixtures" / "typescript_navigation"

# The state root holding the managed TypeScript artifact. Deliberately its own
# variable: these tests must never be pointed at the live vault by accident.
STATE_ROOT_ENV = "LLM_WIKI_LSP_TEST_STATE_ROOT"

STARTUP_SECONDS = 60.0
READY_SECONDS = 30.0
REQUEST_SECONDS = 30.0

PROFILE = lsp_profiles.TYPESCRIPT_PROFILE


def _state_root() -> Path | None:
    raw = os.environ.get(STATE_ROOT_ENV)
    if not raw:
        return None
    return Path(raw).resolve()


def _installed_paths() -> tuple[Path, Path] | None:
    """(server entry, pinned tsserver) if the managed artifact is installed."""
    state_root = _state_root()
    if state_root is None:
        return None
    server = PROFILE.server_path(state_root)
    engine = PROFILE.managed_root(state_root) / lsp_profiles.TSSERVER_RELATIVE
    if not server.is_file() or not engine.is_file():
        return None
    return server, engine


def _node() -> Path | None:
    from shutil import which

    found = which("node")
    if found is None:
        return None
    return Path(found)


requires_artifact = pytest.mark.skipif(
    _installed_paths() is None or _node() is None,
    reason=(
        f"managed TypeScript artifact not installed; set {STATE_ROOT_ENV} to a "
        "state root containing it (separate explicit operator action)"
    ),
)


class _Client:
    """A bounded LSP client, driven entirely by the profile under test.

    It is deliberately small and deliberately not a second session
    implementation: it exists to prove the profile's data starts and steers a
    real server. Lifecycle ownership, containment, leases and evidence stay in
    `lsp_process`, which this does not duplicate and does not replace.
    """

    def __init__(self, node: Path, server: Path, state_root: Path) -> None:
        self.state_root = state_root
        self.command = PROFILE.launch_command(node, server, FIXTURE.resolve())
        self.proc = subprocess.Popen(
            list(self.command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=str(FIXTURE),
        )
        self._next_id = 0
        self._replies: dict[int, dict] = {}
        self._lock = threading.Lock()
        self.ready = threading.Event()
        self.identity: tuple[str | None, bool] | None = None
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # -- transport ---------------------------------------------------------

    def _write(self, message: dict) -> None:
        body = json.dumps(message).encode()
        assert self.proc.stdin is not None
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.proc.stdin.flush()

    def notify(self, method: str, params: object) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: object) -> int:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
        self._write(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        return request_id

    def _read_loop(self) -> None:
        stream = self.proc.stdout
        assert stream is not None
        while True:
            header = stream.readline()
            if not header:
                return
            if not header.startswith(b"Content-Length:"):
                continue
            length = int(header.split(b":")[1])
            self._drain_headers(stream)
            self._dispatch(json.loads(stream.read(length)))

    @staticmethod
    def _drain_headers(stream) -> None:
        while stream.readline().strip():
            pass

    def _dispatch(self, payload: dict) -> None:
        if "id" in payload and "method" not in payload:
            self._replies[payload["id"]] = payload
            return
        self._observe(payload)
        self._answer_server_request(payload)

    def _observe(self, payload: dict) -> None:
        method = payload.get("method")
        if method == PROFILE.identity_notification.method:
            self.identity = PROFILE.identity_notification.confirmed(
                payload.get("params")
            )
        if self._is_progress_end(payload):
            self.ready.set()

    @staticmethod
    def _is_progress_end(payload: dict) -> bool:
        if payload.get("method") != "$/progress":
            return False
        value = (payload.get("params") or {}).get("value") or {}
        return value.get("kind") == "end"

    def _answer_server_request(self, payload: dict) -> None:
        """The server withholds `$/progress` until its create request is answered."""
        if "id" not in payload or "method" not in payload:
            return
        self._write({"jsonrpc": "2.0", "id": payload["id"], "result": None})

    def wait(self, request_id: int, seconds: float = REQUEST_SECONDS) -> dict:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            reply = self._replies.pop(request_id, None)
            if reply is not None:
                return reply
            time.sleep(0.002)
        raise TimeoutError(f"no reply to request {request_id}")

    # -- lifecycle ---------------------------------------------------------

    def initialize(self) -> dict:
        request_id = self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": _uri(FIXTURE),
                "capabilities": _CLIENT_CAPABILITIES,
                "initializationOptions": PROFILE.wire_initialization_options(
                    self.state_root
                ),
                "workspaceFolders": [{"uri": _uri(FIXTURE), "name": "fixture"}],
            },
        )
        result = self.wait(request_id, STARTUP_SECONDS)["result"]
        self.notify("initialized", {})
        return result

    def open(self, path: Path) -> None:
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": _uri(path),
                    "languageId": PROFILE.language_ids[0],
                    "version": 1,
                    "text": path.read_text(encoding="utf-8"),
                }
            },
        )

    def await_ready(self) -> bool:
        if not PROFILE.gates_on_progress():
            return True
        return self.ready.wait(READY_SECONDS)

    def close(self) -> None:
        try:
            self.wait(self.request("shutdown", {}), 10.0)
            self.notify("exit", {})
            self.proc.wait(timeout=10.0)
        except Exception:
            self.proc.kill()


# The subset of `pyright_session._CLIENT_CAPABILITIES` this probe needs. The
# `window.workDoneProgress` entry is the one that matters: without it the server
# never opens a progress token and the readiness gate cannot exist.
_CLIENT_CAPABILITIES = {
    "general": {"positionEncodings": ["utf-16", "utf-8"]},
    "textDocument": {
        "definition": {"dynamicRegistration": False, "linkSupport": False},
        "references": {"dynamicRegistration": False},
        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
        "synchronization": {},
    },
    "window": {"workDoneProgress": True},
    "workspace": {"configuration": True, "workspaceFolders": True},
}


def _uri(path: Path) -> str:
    return "file://" + str(path.resolve())


def _anchor() -> tuple[Path, int, int]:
    """The second use of `renewLease` in main.ts -- a use site, not a declaration."""
    main = FIXTURE / "src" / "main.ts"
    lines = main.read_text(encoding="utf-8").splitlines()
    line = next(i for i, text in enumerate(lines) if text.startswith("const next"))
    return main, line, lines[line].index("renewLease")


def _locations(result: object) -> tuple[LspLocation, ...]:
    """Parse wire locations with the normalizers the Python path already uses."""
    if not isinstance(result, list):
        return ()
    return tuple(_one_location(entry) for entry in result if _readable(entry))


def _readable(entry: object) -> bool:
    return isinstance(entry, dict) and isinstance(entry.get("uri"), str)


def _one_location(entry: dict) -> LspLocation:
    return LspLocation(uri=entry["uri"], range=_lsp_range(entry.get("range")))


@pytest.fixture()
def client():
    paths = _installed_paths()
    node = _node()
    session = _Client(node, paths[0], _state_root())
    try:
        yield session
    finally:
        session.close()


@requires_artifact
def test_the_profile_argv_starts_the_pinned_server(client):
    result = client.initialize()
    assert client.command[2] == "--stdio"
    assert result["capabilities"]["definitionProvider"] is True
    assert result["capabilities"]["referencesProvider"] is True


def _awaited_identity(session, seconds: float = 10.0) -> tuple[str | None, bool]:
    deadline = time.monotonic() + seconds
    while session.identity is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert session.identity is not None, "server never reported its TypeScript"
    return session.identity


def _definition_at(session, main: Path, line: int, character: int):
    reply = session.wait(
        session.request(
            "textDocument/definition",
            {
                "textDocument": {"uri": _uri(main)},
                "position": {"line": line, "character": character},
            },
        )
    )
    return _locations(reply.get("result"))


def _references_at(session, main: Path, line: int, character: int):
    reply = session.wait(
        session.request(
            "textDocument/references",
            {
                "textDocument": {"uri": _uri(main)},
                "position": {"line": line, "character": character},
                "context": {"includeDeclaration": True},
            },
        )
    )
    return _locations(reply.get("result"))


def _opened_and_ready(session):
    session.initialize()
    main, line, character = _anchor()
    session.open(main)
    assert session.await_ready() is True, "no readiness signal within the deadline"
    return main, line, character


def _key_is_well_formed(key: tuple) -> bool:
    if len(key) != 5 or not isinstance(key[0], str):
        return False
    return all(isinstance(part, int) for part in key[1:])


def _check_location_shape(location: LspLocation) -> None:
    assert isinstance(location, LspLocation)
    assert location.range is not None, "range failed pyright_session._lsp_range"
    assert _key_is_well_formed(_location_key(location))


@requires_artifact
def test_the_server_loads_the_typescript_we_pinned_not_one_it_found(client):
    """`source != user-setting` would mean answers from an unpinned engine."""
    client.initialize()
    version, confirmed = _awaited_identity(client)
    assert confirmed is True
    assert version == lsp_profiles.TSSERVER_VERSION


@requires_artifact
def test_definition_crosses_the_file_edge_once_the_project_is_ready(client):
    """The whole point of the readiness policy, stated as an assertion."""
    main, line, character = _opened_and_ready(client)
    found = _definition_at(client, main, line, character)
    assert len(found) == 1
    assert found[0].uri.endswith("src/lease.ts"), "definition must cross the file edge"
    assert found[0].range.start.line == 5


@requires_artifact
def test_references_include_the_declaration_in_the_other_file(client):
    main, line, character = _opened_and_ready(client)
    refs = _references_at(client, main, line, character)
    assert len(refs) == 4
    assert any(location.uri.endswith("src/lease.ts") for location in refs)


@requires_artifact
def test_the_returned_locations_have_the_shape_the_python_path_returns(client):
    """Same envelope: these parse through pyright_session's own normalizers."""
    main, line, character = _opened_and_ready(client)
    found = _definition_at(client, main, line, character)
    assert found, "no locations to check the shape of"
    for location in found:
        _check_location_shape(location)


@requires_artifact
def test_querying_before_readiness_is_what_the_gate_exists_to_prevent(client):
    """Documents the failure, so nobody later removes the gate as ceremony.

    Measured 2026-08-28: ungated, this query returned the import binding in
    `main.ts` rather than the declaration in `lease.ts`, 12 runs out of 12. The
    assertion is deliberately weak -- it accepts either outcome -- because the
    race is a race. What it pins is that the readiness signal is the thing that
    separates them, and that an ungated answer may be wrong without being empty.
    """
    client.initialize()
    main, line, character = _anchor()
    client.open(main)
    reply = client.wait(
        client.request(
            "textDocument/definition",
            {
                "textDocument": {"uri": _uri(main)},
                "position": {"line": line, "character": character},
            },
        )
    )
    early = _locations(reply.get("result"))
    assert early, "an unready server still answers -- that is the hazard"

    assert client.await_ready() is True
    late = client.wait(
        client.request(
            "textDocument/definition",
            {
                "textDocument": {"uri": _uri(main)},
                "position": {"line": line, "character": character},
            },
        )
    )
    settled = _locations(late.get("result"))
    assert len(settled) == 1
    assert settled[0].uri.endswith("src/lease.ts")
