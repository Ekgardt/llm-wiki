# Provider stdin keeps the counted bytes on Windows

On 2026-10-07, Windows CI at `d42ced6` demonstrated that Codex received CRLF where the prepared and counted payload contained LF. One exact count changed from 272 to 278. The input writer used text mode with `newline=None`; dispatch subsequently read that file in binary mode.

The writer now uses `newline="\n"`. UTF-8 encoding and every original line ending remain unchanged. Binary writing would also preserve bytes, but an explicit newline policy fixes the existing writer without adding another implementation. Normalizing assertions or dispatch input would hide the discrepancy and alter protected evidence.

Three regression cases reproduce Windows newline translation through a real temporary-file writer on any platform. They cover LF, mixed original CRLF/LF, and Unicode. All three fail before the fix. The existing prepared-invocation tests still require exact equality between counted and dispatched input.

Two other CI failures had separate causes. The fact-key test now supplies its existing logical path through `as_posix()`, retaining the normalized-path guard and source-span assertions. The installer rewrite test uses the supported PowerShell installer on Windows and Bash on POSIX. It verifies the preserved model, foreign server, comments, and exact preimage. A bare Windows `bash` had invoked WSL without an installed distribution; its exact executable path was not captured.

Qualification: 211 related tests passed under actual Python 3.10 on Linux in a standalone checkout with the installed compiler unchanged; two existing PowerShell checks skipped because PowerShell was unavailable. All changed functions passed Python 3.10 AST/Lizard complexity and Ruff. Actual execution of the revised PowerShell harness remains a Windows CI requirement. This change does not establish completion of the audit or the full compilation cycle.

Primary sources checked on 2026-10-07: [Python newline semantics](https://docs.python.org/3.10/library/functions.html#open), [Python subprocess stdin](https://docs.python.org/3.10/library/subprocess.html), [Microsoft WSL commands](https://learn.microsoft.com/en-us/windows/wsl/basic-commands), [GitHub Windows shell selection](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idstepsshell), and [Codex non-interactive stdin](https://learn.chatgpt.com/docs/non-interactive-mode). Fresh native navigation at `d42ced6` was supplemented with source inspection because the graph envelope remained partial.
