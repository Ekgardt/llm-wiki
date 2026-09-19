# The second installer gets the first one's guarantees

Date: 2026-09-17. Audit 3, code intelligence, findings B21 and the scratch-sweep
half of B22.

Files: `scripts/pinned_download.py` (new), `scripts/install_language_server.py`,
`scripts/install_pyright.py`,
`tests/test_the_second_installer_gets_the_first_ones_guarantees.py`,
`tests/test_native_language_server.py`

## What was found

`scripts/install_pyright.py` is 3 373 lines of fail-closed installation: an
opener with no proxy and no redirect handler, a status and URL check, a
`Content-Length` ceiling, streamed hashing straight to a descriptor, an absolute
deadline re-clamped onto the socket before every read, an `O_EXCL` install lock
with stale-owner quarantine, `fsync` on every file and directory, create-only
staging and a no-replace atomic publish.

`scripts/install_language_server.py` is 547 lines and installs the other three
servers — `typescript-language-server` (~13 MB of npm tarballs), `gopls` (a
70 MB Go toolchain, then a compile) and `rust-analyzer` (five archives, ~160 MB
compressed, over a gigabyte unpacked). It had:

* `urllib.request.urlopen(request, timeout=60)` — the default opener, which
  installs `ProxyHandler()` and `HTTPRedirectHandler()`, so `http_proxy` /
  `https_proxy` in the environment redirect a pinned download and a 30x is
  followed before the URL check can see it;
* a 60 s **per-socket** timeout and no overall deadline: a server that drips one
  byte every 59 s keeps the install alive forever;
* `response.read(limit + 1)` — the whole archive in memory (160 MB for Rust)
  and then a second full copy into a `NamedTemporaryFile` before extraction;
* no install lock: two installers race on the same managed root;
* no `fsync`: a crash after the rename can leave an empty or partial tree that
  looks installed;
* `_require_absent`: an existing install is refused rather than validated, so a
  re-run after a lost race or an interrupted run is an error, not a no-op;
* `.install-*` staging removed only on a handled exception — `SIGKILL`, a power
  cut or a `SIGTERM` leaves it, and for Rust that is about 3 GB;
* `_install_component` always passing `local=None`, so `--artifact` gave an
  offline install of the main archive while the four Rust components were still
  downloaded: there was no offline Rust install at all.

`install_pyright` leaks its own scratch the same way: `.install-pyright-*`
(about 96 MB) survives an abrupt death and nothing ever removes it (B22).

## Sources

- Python `urllib.request` documentation, `build_opener` and `urlopen`
  (https://docs.python.org/3/library/urllib.request.html, read 2026-09-17):
  "If the Python installation has SSL support ... `urlopen` ... opens the URL
  ... The *opener* ... `build_opener(*handlers)` ... instances of the following
  classes are added to the handlers: `ProxyHandler` (if proxy settings are
  detected), ... `HTTPRedirectHandler`". So the plain `urlopen` path is
  proxy-aware and redirect-following by construction, and the only way to have
  neither is to build an opener — which is exactly what `install_pyright`
  already does.
- Python `socket` documentation, `settimeout`
  (https://docs.python.org/3/library/socket.html, read 2026-09-17): the timeout
  governs one blocking operation, not the transfer, which is why a per-socket
  timeout is not a deadline.
- The pins themselves, measured and recorded in
  `docs/research/2026-09-12-installing-go-and-building-gopls.md` and
  `docs/research/2026-09-12-installing-rust-for-precise-navigation.md`: Go
  1.27.1 is 67-79 MB compressed and 244 MB unpacked; the Rust set is about
  160 MB compressed and over a gigabyte unpacked.

## Alternatives

1. Rewrite `install_language_server` on top of `install_pyright`'s `_Handle` /
   `_Stage` machinery. Rejected: that machinery is descriptor-relative,
   `dir_fd`-based, Windows-file-ID-aware and 1 500 lines deep; lifting it out is
   a rewrite of the installer that 2 523 lines of tests pin, with no finding
   asking for it.
2. Copy the download loop into the second installer. Rejected outright: it is
   the same code with the same bugs to keep in step, which is the class of
   defect this round is closing.
3. Extract the part that is genuinely one thing — *how a pinned URL is opened* —
   into one module both installers import, and build the second installer's
   streamed, deadline-bounded transfer there too. Give the second installer the
   remaining guarantees in its own idiom: an `O_EXCL` lock with a stale-owner
   check, `fsync`, validation of an existing install, and a sweep.

## Decision

Alternative 3.

* New `scripts/pinned_download.py`. It holds `RejectRedirect` and
  `open_pinned_url` — moved verbatim out of `install_pyright`, which now imports
  `open_pinned_url` under the private name its own tests monkeypatch, so its
  behaviour and every existing test are unchanged — plus
  `download_pinned(url, write, deadline=..., limit=...)`, which opens that way,
  refuses anything but `200`, refuses a URL that drifted, refuses a
  `Content-Length` over the limit, and streams with the remaining deadline
  re-clamped onto the socket before every read.
* `install_language_server` streams to a scratch file inside its own staging
  directory and extracts from that handle: one copy on disk instead of 160 MB in
  memory plus a copy.
* One absolute deadline for the whole install
  (`DEFAULT_INSTALL_TIMEOUT_SECONDS = 1800`, `--timeout` on the CLI), checked
  before each archive and carried into every download.
* An install lock, `.install-<profile>-lock` beside the managed root, created
  `O_EXCL` and carrying nonce, pid and process-start identity. A lock whose
  owner is provably gone (`operational_ownership.process_start_identity`) is
  removed once and the lock retried; otherwise the installer waits until the
  deadline. The lock is released only if it is still the one this process wrote.
* `fsync` on every file written into staging, on the staging directories, and on
  the managed parent after the publish rename. POSIX only for directories:
  Windows cannot `os.open` a directory, and the no-replace rename is the
  durability point there.
  The cost is real and accepted: one `fsync` per unpacked file on a Go
  toolchain of 17 353 members, and more for the Rust set. That is seconds to a
  minute or two on top of a multi-minute download and, for gopls, a compile --
  paid once, by an explicit operator action, to make "installed" mean the tree
  is actually on the disk. `install_pyright` already pays it for 5 423 files.
* An existing managed root is now **validated, not refused**: the receipt is read
  and checked with `lsp_identity.validate_install_manifest`, the server (and the
  pinned runtime, when the profile names one) are re-hashed against it, and a
  valid install is returned unchanged. An invalid one is still an `InstallError`
  naming the root.
* Sweep: while the install lock is held, no other installer for this profile can
  be running, so every `.install-*` entry beside the managed root is abandoned
  scratch and is removed. `install_pyright` gets the same rule for
  `.install-pyright-*` under its own lock.
* `--component-artifact NAME=PATH` (repeatable) threads an operator-supplied
  archive into `_install_component`, so `--artifact` plus four
  `--component-artifact` flags is a fully offline Rust install. The path is
  still verified against the pinned SHA-256 before anything is unpacked.

### Resume: deliberately not implemented

Decided and stated rather than left open. A resumable download needs a partial
file that outlives the process, a `Range` request the origin is free to ignore,
and a hash-over-prefix protocol to prove the bytes already on disk belong to the
pinned artifact. `install_pyright` has none of it either, none of the five
origins (registry.npmjs.org, dl.google.com, static.rust-lang.org) promises
`Range` for these URLs, and with the lock and the sweep a failed install now
costs one retry and no disk. Adding resume would add a second way to be
half-installed, which is the thing every other guarantee here exists to prevent.
