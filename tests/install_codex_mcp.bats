#!/usr/bin/env bats
# The Codex entry install.sh writes keeps the memory tools out of tool search,
# and an existing entry that lacks the line is named, not rewritten
# (docs/research/2026-11-17-every-memory-tool-is-visible.md).

setup() {
  # shellcheck source=../install.sh
  source "$BATS_TEST_DIRNAME/../install.sh"
  set +u
}

@test "a new Codex entry asks for the tools in the initial list" {
  config="$BATS_TEST_TMPDIR/codex/config.toml"

  add_codex_mcp_block "/vault" "$config"

  grep -qx '\[mcp_servers.llm-wiki\]' "$config"
  grep -qx 'omit_tools_from = \["deferred"\]' "$config"
}

@test "an entry whose tools wait for tool search is kept and its missing line named" {
  run codex_mcp_state_status deferred

  [ "$status" -eq 0 ]
  [[ "$output" == *'omit_tools_from = ["deferred"]'* ]]
}

@test "an equivalent entry is kept silently" {
  run codex_mcp_state_status equivalent

  [ "$status" -eq 0 ]
  [ -z "$output" ]
}
