# A user flag belongs to its command

Research date: 2026-09-29.

The redactor treated every `-u name:value` as HTTP credentials, irrespective of
the command. A time format after `date -u` therefore changed under redaction.
The publication DLP boundary scans the whole proposed file, so an old daily
entry containing such a command prevented unrelated later appends. Repeated
retries could not repair that deterministic refusal.

The remedy is to recognize the credential option in the command that defines
it. It does not exempt a particular date format, user, file or fingerprint.
Curl command regions retain quoted arguments and stop at unquoted shell
separators. Inside those regions, credential arguments are redacted whole,
including quoted passwords containing spaces. The rest of the secret rules and
the fail-closed publication boundary continue to run.

Primary sources checked:

- [curl's manual](https://curl.se/docs/manpage.html#-u): `--user`/`-u` specifies
  a user and password separated by the first colon.
- [GNU date options](https://www.gnu.org/software/coreutils/manual/html_node/Options-for-date.html):
  `-u` selects UTC; it does not take a credential argument.
- [POSIX shell command language](https://pubs.opengroup.org/onlinepubs/9799919799/utilities/V3_chap02.html):
  arguments belong to individual commands; quoting protects shell separators.

Alternatives considered: allowlisting the affected daily file would also admit
future secrets there; allowing one date string would miss other noncredential
uses of `-u`; deleting refused transactions would destroy evidence without
repairing the writer. A full shell interpreter is neither needed nor appropriate
in the dependency-light text redactor. Command recognition is intentionally
lexical: it does not resolve aliases, shell variables or generated commands.

Compatibility: this is a standard-library regex change within Python 3.10+.
Regression coverage must preserve date and sort arguments, redact actual curl
credentials on either side of command separators, handle quoted arguments, and
remain idempotent. Real refused work may be retried only after its content passes
the unchanged DLP boundary; the policy is not relaxed to force recovery.
## Follow-up invocation qualification

The first command-scoped implementation missed quoted executable paths,
uppercase `CURL.EXE`, and Bash/PowerShell line continuations. Four regression
cases reproduced those misses on 2026-09-29. Command recognition now preserves
these invocation forms; an unescaped newline still ends the command so a later
`date -u` stays unchanged. No shell command is executed by the redactor.

The curl option contract was rechecked against the
[upstream manual](https://curl.se/docs/manpage.html), and quoting against
[Microsoft's PowerShell reference](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_quoting_rules?view=powershell-7.6).
The installed GNU Bash manual, QUOTING section, specifies backslash-newline
continuation. Its local primary-source copy was consulted because the online GNU
page timed out and the Open Group page returned HTTP 403 during this follow-up.
These are three independent maintainers. This lexical redaction does not execute
or resolve aliases, variables containing executable names, or arbitrary shell
programs; it is not a claim of complete shell interpretation.
