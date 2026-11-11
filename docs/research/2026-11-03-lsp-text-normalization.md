# LSP terminal-text normalization boundary

Date: 2026-11-03. Base: 8f61a0e4. Designed under the approved decomposition mandate.
Move only _is_control, _collapses_to_space, _LogScanner and _normalize_log_text
from lsp_security to scripts/llm_wiki/lsp_text.py. The owner uses unicodedata only.
Existing path validation imports the same _is_control function via its root alias;
existing redaction calls its root _normalize_log_text alias, retaining stage-order
fault instrumentation. No second body or new text limit is introduced.

## Primary sources checked today

- XTerm's maintainer documents modal CSI/OSC/string controls and terminators;
  treating every printable character inside a control string as ordinary text
  would leak control payload. This move preserves the existing scanner and does
  not claim full terminal-emulator conformance.
  https://invisible-island.net/xterm/ctlseqs/ctlseqs.html
- OWASP Logging Cheat Sheet separates input sanitization and sensitive-data
  exclusion. Normalizing terminal/control text alone is not secret redaction.
  https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html
- Unicode UAX #44 defines General_Category properties. Preserve the interpreter's
  existing unicodedata classifications, including Cf/Zl/Zp policy; do not silently
  upgrade the Unicode database or change supported Python versions.
  https://www.unicode.org/reports/tr44/#General_Category_Values

Alternatives: a regex replacement loses the existing state-machine behavior;
merging with generic secret_redact conflates different policies; transferring all
repository/path/credential redaction at once couples independent runtime concerns.
Choose the existing pure normalization stage, keeping every method body unchanged.

## Preserved surrounding authority

lsp_security remains responsible for input/type bounds, credential and URL-userinfo
redaction, home/repository path detection, Windows short-name resolution, final
normalization and output clipping. Its order remains: validate/bound, normalize,
redact, normalize, clip. No part of that order is moved into this lower helper.
Existing security tests all remain intact, including stage instrumentation and
path containment. Fresh graph plus source search must confirm the two root aliases
and no consumer of the removed private scanner/helper definitions.

Add independent pure normalization cases first against the original functions,
then change only their import to the new owner. Cover C0/C1 and Unicode controls,
terminal modes, terminators, nested introducers, incomplete sequences and ordinary
Unicode text. Structural guards must reject the pre-transfer dependency. Full
original security/integration tests remain necessary and are not replaced by the
new focused cases. Include complete source, tests, bootstrap, instructions and
research in a measured <=100000-token losslessly serialized packet.

Qualification completed; evidence below. No security-policy, runtime-root, public-command or live
installation change is authorized by this structural step.


## Qualification evidence

Fresh built-in generation generation-18e46e172fbb56ec-d664fdf8 corresponds to
8f61a0e4. Graph has 16 normalization and 23 control-predicate callers; source
search supplements the incomplete graph. No external scanner/collapse helper
consumer or predicate monkeypatch was found. Root stage-order patches remain.

Original complete security baseline: 185 passed, 24 native skips. Sixty new pure
normalization cases passed against the original functions. Three structural
guards then failed before extraction and pass afterward. Every original
production function/class AST is retained across the two owners. The complete
original security test file is byte-identical. Actual CCN/shape and Ruff pass.

After both registry/text moves: 286 passed, 24 skipped; including the two budget
guards and registry import-declaration guard: Python 3.10 289 passed, 24 skipped.
Focused packets alone: 97 passed. Source/test proofs and logs are retained as
logs/lsp-text-* and logs/lsp-registry-text-*. Consumers/global checks and final
packet sizes follow. No native-platform qualification or whole-project completion
is claimed.


Final qualification: process ownership and stderr consumer suites 215 passed,
44 native skips. Global module, complexity, privacy, CI, structure, ownership and
context completeness guards 1347 passed, 2 skipped. All 55 actual context artifacts
round-tripped every path and byte below 100000 tokens. Before this final paragraph,
registry measured 93262 and text 73079, largest packet 97153. Final weights are
recorded with the committed checkpoint; overlapping totals are not whole-project
unique weight or paired task efficiency. Ownership 432/3162 leaves 2730 files
unassigned. Full 4abf9ccd is still running and predates these changes. Work continues
in the candidate; live installation and native CI remain unperformed.
