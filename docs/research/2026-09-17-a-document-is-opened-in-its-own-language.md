# A document is opened in its own language

Date: 2026-09-17. Audit 3, code intelligence, finding B14.

Files: `scripts/lsp_server_profile.py`, `scripts/pyright_session.py`,
`tests/test_a_document_is_opened_in_its_own_language.py`

## What was found

`LanguageServerSession._did_open_params` always sends `languageId: "python"`.
The session class serves four profiles (Pyright, typescript-language-server,
gopls, rust-analyzer); each profile declares `language_ids`, and no script reads
that field. A `.ts`, `.tsx`, `.go` or `.rs` file is announced to its server as
Python. The user-visible effect was not measured here (no language server is
started by a fix agent): gopls and rust-analyzer largely go by the file name,
while typescript-language-server uses the id to tell TypeScript from JavaScript
and JSX from plain files, so the TypeScript profile is the one most likely to
answer wrongly or not at all.

## Sources

- Language Server Protocol 3.17, `TextDocumentItem`
  (https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/,
  fetched 2026-09-17): `languageId` is "The text document's language
  identifier.", and "If a document refers to one of the programming languages
  listed below it is recommended that clients use those ids." The table gives
  `go`, `javascript`, `javascriptreact`, `python`, `rust`, `typescript`,
  `typescriptreact`.

## Decision

The profile answers the question, because the profile is where "everything
language-shaped is read from" (the session class's own docstring).
`LanguageServerProfile.language_id_for(suffix)` looks the file suffix up in one
table of the specification's identifiers and returns it when the profile
declares that id; any other suffix gets the profile's first declared id, which
is what every single-language profile needs and is never "python" for a server
that is not Pyright. `_did_open_params` becomes an instance method and asks the
session's profile.
