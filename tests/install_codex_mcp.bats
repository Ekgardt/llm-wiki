#!/usr/bin/env bats
# The Codex entry install.sh writes keeps the memory tools out of tool search,
# and an existing entry that lacks the line is named, not rewritten
# (docs/research/2026-11-17-every-memory-tool-is-visible.md).

# install.sh has no source-only mode: its top level either installs or stops
# with `fail`. With LLM_WIKI_ROOT naming another directory it stops at its first
# check, "LLM_WIKI_ROOT points to a different checkout", after every function is
# defined and before anything is written; the EXIT trap then runs the call under
# test with the installer's own functions. `set --` keeps this harness's
# arguments from install.sh's argument parser, and the subshell gives a bare
# `return` its own status: inside a trap handler it would report fail's `exit 1`.
in_installer() {
  LLM_WIKI_ROOT="$BATS_TEST_TMPDIR" bash -c \
    'call=$1 installer=$2; set --; exec 3>&1
     trap "set +eu; (eval \"\$call\") >&3 2>&3; exit \$?" EXIT
     source "$installer" >/dev/null 2>&1' \
    _ "$1" "$BATS_TEST_DIRNAME/../install.sh"
}

@test "a new Codex entry asks for the tools in the initial list" {
  config="$BATS_TEST_TMPDIR/codex/config.toml"

  run in_installer "add_codex_mcp_block /vault '$config'"

  [ "$status" -eq 0 ]
  grep -qx '\[mcp_servers.llm-wiki\]' "$config"
  grep -qx 'omit_tools_from = \["deferred"\]' "$config"
}

@test "an entry whose tools wait for tool search is kept and its missing line named" {
  run in_installer "codex_mcp_state_status deferred"

  [ "$status" -eq 0 ]
  [[ "$output" == *'omit_tools_from = ["deferred"]'* ]]
}

@test "an equivalent entry is kept silently" {
  run in_installer "codex_mcp_state_status equivalent"

  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "a conflicting entry is refused" {
  run in_installer "codex_mcp_state_status conflict"

  [ "$status" -eq 2 ]
}
