# One model selection for memory work

Status: installed in the running vault on 2026-10-19; broader point 7 remains open.

Observed defect: installer choice is copied to agent environment blocks and
scheduler definitions. Local systemd overrides can supersede a newer base unit.
A detached compiler without the copied variables uses the host Codex model and
its own reasoning default. Thus equal vault roots do not imply equal models.

Use the existing vault settings file as the authority. Copying more environment
variables retains drift; changing host-global settings affects unrelated agent
work. The central choice is explicit provider/model plus reasoning; consumers use
one resolver, and installation no longer persists competing copies after migration.
Invalid settings fail visibly; they never silently become another provider.

Primary research checked 2026-10-19:

- [OpenAI account catalogs and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference):
  catalog discovery is not the same as completing inference on that account.
- [Codex app-server](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server):
  a model list may be bundled/cached; a completed request establishes access for
  that request. No new OAuth implementation or token copying is needed here.
- [systemd environment](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html):
  process environments are assembled at launch; they are unsuitable as the only
  mutable vault-wide authority shared with already-running MCP processes.
- [Python atomic replacement](https://docs.python.org/3/library/os.html#os.replace):
  publish a complete configuration on the same filesystem, under existing writer
  coordination. Atomic rename alone does not serialize read-modify-write.
- [TOML](https://toml.io/en/v1.0.0) and
  [TOML Kit](https://tomlkit.readthedocs.io/en/latest/): preserve comments and valid
  TOML rather than invent a partial line-based parser. Dependency choice/versions
  must be recorded with implementation evidence.

Installed Codex CLI0.162.1 exposes `debug models`. Read-only discovery returned
nine records, including two hidden entries; the UI must respect visibility and
reasoning capabilities. No inference was performed during catalog discovery.
Existing source caps on catalog capture require review: truncating JSON is not
valid bounded parsing, and subprocess capture can allocate before truncation.

## Implementation and qualification

The resolver takes one immutable selection snapshot per candidate resolution.
Long-lived updated clients reread file content rather than cache by mtime.
The existing writer gate and coordinator transaction serialize configuration
writers; a verification call holds no writer lock. Saving compares the complete
pre-verification settings bytes and refuses intervening changes. Existing durable
publication writes the whole TOML file. External non-cooperating editors retain
the repository's documented CAS limitation; this is not an OS filesystem sandbox.

TOML Kit0.15.2 (Python>=3.9, no runtime dependencies) preserves comments while
editing keys. The lock adds only this package; existing resolutions are unchanged.
It is constrained to the qualified0.15 release line and pinned with hashes by uv.
Python3.10/3.12 environments are built from the lock. Python3.10 compatibility is
not a claim of ongoing upstream security support.

Codex discovery now spools subprocess stdout/stderr to temporary files and checks
metadata bytes before loading JSON; it no longer allocates unbounded subprocess
output then truncates JSON. HTTP catalogs refuse oversized input instead of
parsing a truncated prefix. The former1MiB bound is exposed as
`provider.catalog_max_bytes` through the shared settings registry. Current catalog
measurement:561046bytes,9entries,2182523-byte tracemalloc parse peak,0.003s. The
default admits roughly twice the measured input as a metadata memory budget;
raise/requalify if catalogs grow. It does not claim a maximum number of models
or worst-case parser allocation. Discovery uses the existing configurable provider
call deadline, removing its separate hardcoded30-second timeout. Hidden entries
are excluded and Codex reasoning choices come from its catalog.

Legacy environment fallback remains only for unconfigured installs and the explicit
fake test mode. New configured installs do not copy model/provider variables to
agent hooks, shell profiles, cron, systemd, launchd or Windows environment resources.
Endpoint/local-only controls remain projected. Old owned projections require
installer reconciliation; unrelated user settings must not be erased. The old
chooser's Claude-settings reader and duplicate selection algorithm were removed.
OpenCode's current HTTP adapter does not carry a selected model, so it remains
available in unconfigured compatibility mode and is not advertised as a selectable
model adapter. This is an explicit capability gap, not a fabricated model list.

The initial four tests reproduced stale provider/model resolution, stale long-lived
selection, unsupported settings section and invalid-selection fallback. A real
protected short request through the installed Codex ChatGPT login accepted
`gpt-6.1-sol` with `medium` and saved only in an isolated temporary vault.
No private knowledge was sent. Exact test totals and rollout status are recorded
in the execution report after final checks.

Upgrade constraint: already-running old Python processes validate llm-wiki.toml
against the old schema. Activating [llm] before stopping/restarting those processes
would make their settings reads fail. A source update alone cannot reload imported
Python modules. The existing MCP supervisor reloads workers before the next request when no
request is in flight, replaying initialization while keeping the client connected.
Its 11 reload and graceful-shutdown regression tests passed on Python 3.12.
Activation therefore requires stopping old background work, coordinating the
maintenance fence and reconciling installer projections; unsupervised clients
need a restart. Source qualification alone is not deployment.


## Installation evidence (2026-10-19)

The live checkout advanced from `995f18b8` to `f91a1cd7`. The existing maintenance
admission was held for configuration publication and installer reconciliation.
The old compiler and its provider child exited after SIGINT; the interrupted call
is not claimed completed and this upgrade did not automatically retry it.
A first activation attempt safely rejected the new section because the upgrade
process itself had imported the old settings module before updating the checkout.
No settings bytes changed on that attempt. A fresh process completed publication.
The standard MCP supervisor replaced its worker and answered the next request
without closing the client connection.

The installed chooser completed another protected short call through the current
Codex login and saved `codex/gpt-6.1-sol` / `medium`. Deliberately stale provider,
model and reasoning environment values no longer overrode this choice. Installer
reconciliation removed model copies from the owned shell, Claude, Codex hooks and
scheduler fragments. The owned Codex MCP entry was replaced with its standard
verified preimage mechanism; unrelated interactive Codex settings were preserved.
The two model-only scheduler drop-ins were removed after preserving preimages.

Before installation, related regression suites passed on locked Python 3.12
(356 passed, 20 skipped) and Python 3.10 (355 passed, 21 skipped). Readme checks
passed 20; supervisor checks passed 11. Static analysis, shell syntax, lockfile
validation and dependency consistency passed. All 82 initially changed functions
met the law-5 thresholds. TOMLKit 0.15.2 has no runtime dependencies; its locked
artifact was hash-verified and its package vulnerability audit found no known
vulnerabilities. This is not a whole-environment vulnerability audit.

An installed-checkout regression run exposed six tests reading the real selected
model instead of an unconfigured fixture. A wider configured-checkout run exposed six more cases with the same assumption.
Four legacy test modules now request a shared empty-settings fixture; no assertions
or production selection rules were weakened. The same run's knowledge-write
guard observed a genuine concurrent tool capture from this session; that record
was retained. The final configured-checkout run passed 376 tests with 20 skips on Python 3.12;
the affected provider/selection set passed 66 with 9 skips on Python 3.10.
All 83 final changed functions meet the law-5 thresholds. Native Windows/macOS runtime behavior and opt-in Codex protocol tests
that were skipped are not claimed qualified.

Operational doctor confirms roots, model configuration admission and install state,
but still reports existing capture history, failed nightly/weekly maintenance,
retained queue/transaction and LSP evidence, and capacity warnings. This change
does not close those issues or prove completion of the separate point-7 refactor.
