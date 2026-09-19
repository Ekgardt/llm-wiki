# A platform without a pin is refused by name

Date: 2026-09-17. Audit 3, code intelligence, findings B22 (profile part) and
the C7 leftovers that live in the profile modules.

Files: `scripts/lsp_profiles.py`, `scripts/lsp_server_profile.py`,
`scripts/install_language_server.py`, `scripts/install_pyright.py`,
`tests/test_a_platform_without_a_pin_is_refused_by_name.py`,
`tests/test_lsp_server_profile.py`, `tests/test_native_language_server.py`

## What was found

1. `lsp_profiles._gopls_artifact` and `_rust_artifact` return the 64-bit Linux
   archive when the running platform has no pin. The registry has to stay
   importable on every platform (doctor and the navigation path read it), so
   the fallback itself is sound; what is wrong is that nothing downstream knows
   a fallback happened. On FreeBSD, or Windows on ARM, `install_language_server`
   downloads a Linux toolchain, verifies it against the Linux hash, unpacks it
   and only then fails (gopls: at the build; rust-analyzer: at first launch).
   `_install_component` already refuses a component with no pin for the
   platform; the main archive has no such refusal.
   `LanguageServerProfile.artifact_for_platform` exists for exactly this
   question and has no caller (C7).
2. `PYRIGHT_VERSION = "1.1.411"` is typed twice: `lsp_paths.py:7` and
   `lsp_profiles.py:56`. `pyright_profile.py` already imports the one in
   `lsp_paths`.
3. `TYPESCRIPT_PACKAGE_SHA256` and `TSSERVER_PACKAGE_SHA256` are read by
   nothing. The installer verifies the SHA-512 Subresource Integrity value,
   which is the hash npm itself publishes (`dist.integrity`). A second, weaker
   digest of the same bytes, checked nowhere, is a record that can drift
   silently. `GO_VERSION` is read by nothing either, while the five Go URLs each
   retype `1.27.1`.
4. `lsp_profiles.profile_named` is `REGISTRY.get` under a second name, called
   only by four tests.
5. `response.fp.raw._sock.settimeout` in `install_pyright.py` is a private
   chain. Verified: there is no public API on `http.client.HTTPResponse` that
   changes the socket timeout after the connection is open. The installer
   already fails closed when the chain is missing
   (`test_download_rejects_unadjustable_transport_before_blocking_read`) and a
   test drives a real `http.client.HTTPResponse` through it
   (`test_download_real_http_response_drip_feed_honors_absolute_deadline`), so
   an interpreter that moves the attribute turns CI red instead of hanging an
   operator's install.
6. `LanguageServerProfile.handles_suffix` no longer exists (removed before this
   round). `language_ids` is read by `language_id_for` since 94f0e2e.

## Sources

- Python `urllib.request` documentation
  (https://docs.python.org/3/library/urllib.request.html, fetched 2026-09-17):
  "The optional timeout parameter specifies a timeout in seconds for blocking
  operations like the connection attempt (if not specified, the global default
  timeout setting will be used)." The timeout is fixed at open; nothing public
  re-clamps it as an absolute deadline approaches.
- The repository's own measurement,
  `docs/research/2026-09-12-installing-go-and-building-gopls.md` and
  `docs/research/2026-09-12-installing-rust-for-precise-navigation.md`: the pins
  exist for linux/x86_64, linux/arm64, darwin/x86_64, darwin/arm64 and
  windows/x86_64 only.

## Alternatives

1. Make the profile `None` on an unpinned platform. Rejected: the registry,
   doctor and suffix routing read every profile on every platform.
2. Keep the fallback and let the install fail where it fails today. Rejected:
   tens to hundreds of megabytes are downloaded first, and the failure names a
   linker or an exec error, not the cause.
3. Keep the fallback for import, and have the installer ask the profile for
   this platform's pin before it touches the network; no pin is an
   `InstallError` that names the platform.

## Decision

Alternative 3. `install_language_server` refuses, before any download, a
profile that declares `platform_artifacts` and pins none for the running
platform. One `PYRIGHT_VERSION` (in `lsp_paths`, imported by `lsp_profiles`).
The two unread SHA-256 constants and `profile_named` are deleted; `GO_VERSION`
becomes the one place the Go release is typed and the URLs are built from it.
The private socket chain stays, fail-closed and canaried, and its function now
says so.
