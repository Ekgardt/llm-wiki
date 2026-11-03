# Runtime locality and permissions

`llm_wiki.runtime_environment` owns existing mount/network discovery, Windows
reparse detection, local-root refusal, cloud-name warnings and permission checks
and hardening. It depends on runtime_contracts and lazily on windows_acl. Native
mount commands, time/output bounds, platform behavior and error propagation are
unchanged. Unknown mount data remains unknown; cloud-folder names are only a hint.

Reparse refusal precedes network inspection. POSIX permission failures propagate;
unsupported permission bits retain their explicit warning/False result. Windows
ACL work uses the existing owner. SQLite lock probes and admission remain callers'
responsibility. An environment check is not a filesystem sandbox or durable permit.
The former reliable_memory names remain identity aliases; tests replace bindings
where they are actually looked up. Run `uv run pytest tests/modules/runtime_environment -q`.
