# One model selection for memory work

Status: implemented in an isolated branch; deployment pending (2026-10-19).

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
Python modules. Therefore activation requires a coordinated client restart and
installer projection reconciliation; source qualification alone is not deployment.
