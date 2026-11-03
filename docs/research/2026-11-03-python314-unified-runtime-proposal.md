# One Python minor version for development, installation and CI

Date: 2026-11-03. Approved by the owner: «Да, перейти на Python 3.14 по плану».
Implementation and runtime qualification are in progress; no live installation.

The owner asked why the project cannot use one recent Python version. It can:
use the Python 3.14 maintenance series consistently in project metadata, a project
interpreter pin, installers and all CI jobs. The proposed supported range is
>=3.14,<3.15. Keep Linux, Windows and macOS testing; remove redundant Python-minor
lanes only after equivalent coverage is preserved. Patch releases can advance
within that series after qualification. The live vault is excluded from this work.

Why 3.14 instead of 3.15: the latter is stable upstream, but the current ONNX
Runtime 1.31.1 has no cp315 or abi3 wheels; tree-sitter 0.26.0 likewise lacks cp315
and abi3 wheels. A source build is not the existing ordinary installation path.
Python 3.14 is the newest common ready-made binary target checked for the current
CI platforms. A disposable resolver probe (outside the candidate) with all current
version constraints and --upgrade produced 122 locked package records. Existing
explicit pins stay pinned; this is not permission to jump every dependency major.

Probe versions: ONNX Runtime 1.31.1, numpy 2.5.4, filelock 4.2.7, msgpack 1.2.4,
setuptools 84.2.1, virtualenv 21.15.2, certifi 2026.10.23, torch 2.15.0/2.15.0+cpu.
Torch's two builds are platform artifacts of one library version, not multiple
Python interpreters. No older-Python filelock fallback remains. Installer/full,
dev and audit dependencies passed uv's --locked --dry-run --no-build check for
Linux x86_64, Windows x86_64 and macOS 15 ARM64. These checks prove resolution and
wheel availability, not actual native execution. Optional benchmark jieba remains
its pre-existing pure-Python source distribution; --all-extras --no-build therefore
refuses it independently of Python version. No benchmark feature is removed.

Platform caveat: current recent ONNX Runtime wheels target macOS ARM64 and macOS
14+; Intel macOS coverage is not established by this proposal. Do not silently
claim that changing Python qualifies an unsupported architecture. Compare with the
existing install baseline before making any platform-contract claim.

Implementation after approval: update metadata/pin/install interpreter selection,
README.md/README.ru.md/README.zh-CN.md together, current support docs and CI;
regenerate uv.lock with the pinned uv; retire proven obsolete <3.14 branches and
fallbacks only after checking consumers. Keep historical evidence intact. Check
all locked variants against both advisory feeds and exact coverage. Qualify the
new dependency environment, public commands, full regression, native installers
and module budgets. No release/version bump or live installation is implied.

Sources checked today:
- PSF lifecycle: https://devguide.python.org/versions/
- ONNX Runtime current metadata/files: https://pypi.org/project/onnxruntime/1.31.1/
- tree-sitter current metadata/files: https://pypi.org/project/tree-sitter/0.26.0/
- uv lock/platform commands, pinned local uv 0.12.3 help and
  https://docs.astral.sh/uv/concepts/projects/layout/
Evidence: private logs/python314-upgrade-resolution.txt and
logs/python314-upgrade-{linux,windows,macos}-dry-run.txt. Disposable proposed
pyproject.toml and uv.lock: /tmp/llm-wiki-python314-upgrade-probe/.

Exact universal OSV coverage: 121 external name/version pairs, no advisories.
Intel macOS full dependency dry runs fail for both baseline ONNX Runtime 1.27.0
and proposed 1.31.1; this limitation predates the proposed Python change.

Implementation notes: installers resolve `uv python find 3.14` once for bootstrap,
then the project pin governs the locked environment. CI exposes that interpreter's
binary directory before isolating HOME/USERPROFILE. The previous compiler-only
3.10 deep-expression preflight is retired; real small-stack and flat-literal tests
remain. The shared ISO normalization continues to enforce its input grammar and
UTC/fraction behavior, so it is not deleted merely because Python can parse Z.

Ruff now targets py314. Safe UTC updates use datetime.UTC, the identical singleton
to timezone.utc. Eight existing string/Enum classes and three TypeVar functions
keep their behavior: narrow UP042/UP047 annotations name the reason, with no global
lint-rule disablement. These are not unsupported-Python fallback branches.
StrEnum changes string/format output; generic syntax can change runtime type
parameter identity. Remove these exceptions only with a separately qualified
compatibility migration. Sources checked 2026-11-03:
https://docs.python.org/3.14/library/enum.html#enum.StrEnum
https://docs.astral.sh/ruff/rules/replace-str-enum/
https://docs.astral.sh/ruff/rules/non-pep695-generic-function/.
The all-function CCN and branch-shape gates stay unchanged.

Native run 41625731881 checks the earlier 3149d5d0, not this upgrade. Its failed
Windows nightly did not retain the underlying redacted capture diagnostic; the
E2E job now uploads that existing file on failure. This adds evidence without
changing the failed step's result or any timeout. Other native fixture failures
(owner-only ACL setup, oversized parameter IDs and executable shim portability)
are separately investigated; Python unification does not claim to fix them.
