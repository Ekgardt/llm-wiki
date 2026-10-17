# The download retry does not wait hours

Date: 2026-10-13. Scope: the existing optional Hugging Face transfer dependency.

The lock selected hf-xet1.5.1. Its maintainers reproduced an HTTP retry
configuration defect: a millisecond base value was also used as the exponential
multiplier. With the existing three-second first wait, the second wait became
9000 seconds instead of nine. PR893 corrects the construction and includes
side-by-side delay-sequence and duration-cap regressions. Stable release1.5.2
ships that fix. These are upstream reproduction and regression evidence;
the upstream tests have not been executed locally here.

Our CI2b2c98fa macOS installer was cancelled after twenty minutes fetching
the pinned reranker. The same run's Linux and Windows installers fetched it
in about seven seconds. This rules out its size alone as a sufficient
explanation. No Xet request log or native stack survived that cancelled job,
so the exclusive macOS cause remains unconfirmed. The known locked-library
defect is relevant to this exact download path; it does not establish the
cause of the memory compiler's earlier multi-hour or repeated-read behavior.

The targeted `uv lock --upgrade-package hf-xet==1.5.2` changes only that
package. All17 locked source/wheel URL hashes and sizes match PyPI metadata.
The wheels include the existing Linux, macOS and Windows targets, with abi3
and Python3.14 free-threaded variants. Python>=3.8 includes the product's
Python3.10 minimum. An isolated real Python3.10 import loaded hf-xet1.5.2 and
the unchanged huggingface-hub1.23.0; the snapshot_download revision,
allow_patterns and local_files_only parameters remain. Fifteen existing model
pinning and validation tests pass. None of this is a cold macOS installation
qualification or proof of faster full compilation.

This picks the smallest stable patch carrying the established retry correction.
The newer1.7.0 exists, but unrelated feature changes are unnecessary for this
defect. Keeping1.5.1 retains a known bad wait calculation; disabling Xet,
changing concurrency, shrinking the model, hiding cold downloads with cache,
increasing CI timeouts or retrying until green would not correct that calculation.
No environment contract, model revision, model digest, installer deadline,
runtime root or product version changes. Only the existing lock is updated.
The candidate must complete its full platform matrix before installation;
the installed vault remains on its previous qualified release meanwhile.

Primary sources checked on this date:
- Upstream causal explanation, merged fix and regression names:
  https://github.com/huggingface/xet-core/pull/893
- Upstream stable patch release:
  https://github.com/huggingface/xet-core/releases/tag/v1.5.2
- PyPI release metadata, compatible wheel tags and verified artifact hashes:
  https://pypi.org/project/hf-xet/1.5.2/
- Astral's targeted lock update and locked synchronization contract:
  https://docs.astral.sh/uv/concepts/projects/sync/
- PyPA's hash-checked installation guidance:
  https://pip.pypa.io/en/stable/topics/secure-installs/

The upstream issue and release are one authority, not independent corroboration.
Astral and PyPA independently establish how to constrain and verify the
dependency replacement. Project evidence is `scripts/install_models.py::fetch`,
the install.sh/install.ps1 callers and the semantic/hybrid dependency groups.
The current native graph is partial; the actual download call, lock and
installer configuration were also read directly.
