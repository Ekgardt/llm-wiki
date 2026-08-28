"""One shared local MCP server over HTTP, for many agents at once (OPS-01).

Why this exists. Every agent on this machine starts its own stdio MCP
subprocess, and each one pays the same cold start: the semantic encoder alone
costs 7.99 s and ~1.1 GiB resident per process (`knowledge/log.md`,
2026-08-26). One resident server pays that once and every agent shares it.

What this is not. It is a *transport*, not a capability. The tool surface is
the same twelve tools, reached through `mcp_server.build_server()` - the same
`_validate_tool_arguments`, the same operation deadline, the same envelope.
Nothing here dispatches a tool itself.

Protocol: Streamable HTTP as specified in MCP revision **2025-11-25**
(`https://modelcontextprotocol.io/specification/2025-11-25/basic/transports`),
which is the revision the installed SDK reports as
`mcp.types.LATEST_PROTOCOL_VERSION`. The framing is the SDK's
`StreamableHTTPSessionManager`; this module contributes the security boundary
around it.

The security boundary, and why each half is here (spec Security Warning,
verbatim): "Servers MUST validate the `Origin` header on all incoming
connections to prevent DNS rebinding attacks"; "When running locally, servers
SHOULD bind only to localhost (127.0.0.1) rather than all network interfaces
(0.0.0.0)"; "Servers SHOULD implement proper authentication for all
connections."

  1. Bind. Literal loopback only - `127.0.0.1` or `::1`. A hostname is
     refused, because a hostname is resolved and a resolver can be moved.
  2. Origin. **No** Origin header is ever accepted. A legitimate MCP client
     sends none; a browser always sends one on a cross-origin request. So
     "present" is sufficient to refuse, and the refusal is 403 as the spec
     requires. This is the hole that cost the Rust SDK CVE-2026-42559 (CVSS
     8.8) and the Ruby SDK CVE-2026-63118.
  3. Host. Must name the address we bound - 421 otherwise, matching the SDK's
     own `TransportSecurityMiddleware`, which also runs, inside the transport.
  4. Authentication. Loopback is not authentication: every process of every
     user on this machine can reach a loopback port. A bearer token, generated
     once and kept in a 0600 file, is what identifies a legitimate client. The
     token is never an argument, because on Linux `/proc/<pid>/cmdline` is
     world-readable - the mistake Jupyter's own security guidance names.

Run (explicit, opt-in; stdio stays the default and is unchanged):

    uv run --locked --no-sync python scripts/mcp_http.py --port 8765

Agent config:

    {"mcpServers": {"llm-wiki": {"type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": {"Authorization": "Bearer <contents of the token file>"}}}}
"""
from __future__ import annotations

import argparse
import contextlib
import hmac
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mcp_server  # noqa: E402

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MCP_ENDPOINT_PATH = "/mcp"
# Literal addresses only. "localhost" is a name, and a name is resolved.
LITERAL_LOOPBACK_HOSTS = ("127.0.0.1", "::1")
LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
TOKEN_ENTROPY_BYTES = 32
TOKEN_FILE_MODE = 0o600
TOKEN_DIR_MODE = 0o700
BEARER_PREFIX = "bearer "
MAX_AUTHORIZATION_HEADER_BYTES = 4096


class HttpSurfaceError(RuntimeError):
    """The HTTP surface refuses to start or to serve a request."""


# ---------------------------------------------------------------------------
# Bind address
# ---------------------------------------------------------------------------


def require_loopback_host(host: str) -> str:
    """Return `host`, or refuse anything that is not literal loopback.

    `0.0.0.0` publishes a private vault to the network, so this fails closed
    rather than warning. A name (`localhost`) is refused too: it is resolved,
    and what resolves it is not ours to trust.
    """
    if host in LITERAL_LOOPBACK_HOSTS:
        return host
    allowed = " or ".join(LITERAL_LOOPBACK_HOSTS)
    raise HttpSurfaceError(
        f"refusing to bind {host!r}: the local MCP surface binds {allowed} only"
    )


def allowed_host_headers(host: str, port: int) -> tuple[str, ...]:
    """Host header values that name the address we actually bound."""
    return (
        f"127.0.0.1:{port}",
        f"localhost:{port}",
        f"[::1]:{port}",
        f"{host}:{port}",
    )


# ---------------------------------------------------------------------------
# Bearer token
# ---------------------------------------------------------------------------


def default_token_path() -> Path:
    """The token lives with the rest of the runtime state, under `run/`."""
    from memory_state import STATE_DIR

    return Path(STATE_DIR) / "mcp-http" / "token"


def _private_mode_is_enforceable() -> bool:
    """Windows does not express 0600 in `st_mode`, so do not pretend to check."""
    return os.name == "posix"


def _require_private_mode(path: Path) -> None:
    if not _private_mode_is_enforceable():
        return
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise HttpSurfaceError(
            f"token file {path} is reachable by other users (mode {mode:04o}); "
            "fix it with chmod 600 or delete it to mint a new token"
        )


def _read_existing_token(path: Path) -> str | None:
    """Reuse a token across restarts so agent configuration stays valid."""
    if not path.is_file():
        return None
    _require_private_mode(path)
    token = path.read_text(encoding="utf-8").strip()
    return token or None


def _write_new_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, TOKEN_DIR_MODE)
    token = secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, TOKEN_FILE_MODE
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    with contextlib.suppress(OSError):
        os.chmod(path, TOKEN_FILE_MODE)
    return token


def ensure_token(path: Path) -> str:
    """Return the shared secret, minting one on first run."""
    existing = _read_existing_token(path)
    if existing is not None:
        return existing
    return _write_new_token(path)


# ---------------------------------------------------------------------------
# Request guard
# ---------------------------------------------------------------------------


def _header_map(scope) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers") or ():
        name = raw_name.decode("latin-1").lower()
        headers.setdefault(name, raw_value.decode("latin-1"))
    return headers


def _peer_host(scope) -> str | None:
    client = scope.get("client")
    if not client:
        return None
    return str(client[0])


def _refuse_peer(guard, scope, headers) -> tuple[int, str] | None:
    """Defence in depth behind the bind: judge the peer when we can see it."""
    peer = _peer_host(scope)
    if peer is None or peer in LOOPBACK_PEERS:
        return None
    return 403, "peer is not loopback"


def _refuse_host(guard, scope, headers) -> tuple[int, str] | None:
    host = headers.get("host")
    if host in guard.allowed_hosts:
        return None
    return 421, "invalid Host header"


def _refuse_origin(guard, scope, headers) -> tuple[int, str] | None:
    """Any Origin at all is a browser, and no browser is a legitimate client.

    Spec 2025-11-25: "If the `Origin` header is present and invalid, servers
    MUST respond with HTTP 403 Forbidden."  Here every present Origin is
    invalid, because this server serves no page that could produce one.
    """
    if "origin" not in headers:
        return None
    return 403, "Origin header is not accepted by the local MCP surface"


def _presented_token(headers) -> str | None:
    value = headers.get("authorization", "")
    if len(value) > MAX_AUTHORIZATION_HEADER_BYTES:
        return None
    if not value.lower().startswith(BEARER_PREFIX):
        return None
    return value[len(BEARER_PREFIX):].strip()


def _refuse_authorization(guard, scope, headers) -> tuple[int, str] | None:
    presented = _presented_token(headers)
    if presented is not None and hmac.compare_digest(presented, guard.token):
        return None
    return 401, "a valid bearer token is required"


_GUARD_CHECKS = (
    _refuse_peer,
    _refuse_host,
    _refuse_origin,
    _refuse_authorization,
)


async def _send_refusal(send, status: int, reason: str) -> None:
    body = f"{reason}\n".encode()
    headers = [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(body)).encode()),
    ]
    if status == 401:
        headers.append((b"www-authenticate", b'Bearer realm="llm-wiki"'))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class LoopbackGuard:
    """The only door. Everything the transport sees has passed all four checks."""

    def __init__(self, app, *, token: str, allowed_hosts) -> None:
        self.app = app
        self.token = token
        self.allowed_hosts = frozenset(allowed_hosts)

    def refusal(self, scope) -> tuple[int, str] | None:
        headers = _header_map(scope)
        for check in _GUARD_CHECKS:
            refusal = check(self, scope, headers)
            if refusal is not None:
                return refusal
        return None

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        refusal = self.refusal(scope)
        if refusal is None:
            await self.app(scope, receive, send)
            return
        await _send_refusal(send, refusal[0], refusal[1])


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def _security_settings(host: str, port: int):
    """The SDK's own rebinding check, inside the transport, saying the same thing."""
    from mcp.server.transport_security import TransportSecuritySettings

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(allowed_host_headers(host, port)),
        allowed_origins=[],
    )


def _session_manager(server, host: str, port: int, json_response: bool):
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    return StreamableHTTPSessionManager(
        app=server,
        json_response=json_response,
        security_settings=_security_settings(host, port),
    )


class _StreamableEndpoint:
    """A callable object, not a function, so Starlette routes it as a raw ASGI app.

    A `Mount` would answer `/mcp` with a 307 to `/mcp/`, which every agent
    configuration would then have to know about; the endpoint path stays exact.
    """

    def __init__(self, manager) -> None:
        self.manager = manager

    async def __call__(self, scope, receive, send) -> None:
        await self.manager.handle_request(scope, receive, send)


def _mounted_app(manager):
    from starlette.applications import Starlette
    from starlette.routing import Route

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            yield

    route = Route(
        MCP_ENDPOINT_PATH,
        _StreamableEndpoint(manager),
        methods=["GET", "POST", "DELETE"],
    )
    return Starlette(routes=[route], lifespan=lifespan)


def build_app(
    *,
    token: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    json_response: bool = True,
    server=None,
):
    """Build the guarded ASGI application around one shared MCP server."""
    require_loopback_host(host)
    manager = _session_manager(
        server if server is not None else mcp_server.build_server(),
        host,
        port,
        json_response,
    )
    return LoopbackGuard(
        _mounted_app(manager),
        token=token,
        allowed_hosts=allowed_host_headers(host, port),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _require_mcp_available() -> None:
    if mcp_server.MCP_AVAILABLE:
        return
    raise HttpSurfaceError(
        "MCP package not installed. Run: uv sync --extra mcp-server"
    )


def _announce(host: str, port: int, token_path: Path) -> None:
    """Name the endpoint and where the secret is - never the secret itself."""
    print(f"llm-wiki MCP over HTTP: http://{host}:{port}{MCP_ENDPOINT_PATH}", file=sys.stderr)
    print(f"bearer token file: {token_path}", file=sys.stderr)
    if not _private_mode_is_enforceable():
        print(
            "warning: file permissions are not enforced on this platform; "
            "the token file is only as private as its directory",
            file=sys.stderr,
        )


def _shutdown() -> None:
    import time

    with contextlib.suppress(BaseException):
        mcp_server._close_navigation_session_manager(
            time.monotonic() + mcp_server.MCP_OPERATION_SECONDS
        )


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    token_path: Path | None = None,
) -> int:
    """Serve the shared surface until interrupted. Returns an exit code."""
    import uvicorn

    _require_mcp_available()
    host = require_loopback_host(host)
    path = Path(token_path) if token_path is not None else default_token_path()
    token = ensure_token(path)
    app = build_app(token=token, host=host, port=port)
    mcp_server._start_encoder_warmup()
    _announce(host, port, path)
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", access_log=False
    )
    try:
        uvicorn.Server(config).run()
    finally:
        _shutdown()
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve the LLM-Wiki MCP tools over local HTTP (opt-in; stdio stays the default)."
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="literal loopback address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token-file", default=None, help="where the bearer token lives")
    parser.add_argument(
        "--print-token",
        action="store_true",
        help="print the token to stdout and exit, without serving",
    )
    return parser


def _print_token(token_path: Path | None) -> int:
    path = Path(token_path) if token_path is not None else default_token_path()
    print(ensure_token(path))
    return 0


def main(argv=None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.print_token:
            return _print_token(arguments.token_file)
        return serve(arguments.host, arguments.port, arguments.token_file)
    except HttpSurfaceError as error:
        print(str(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
