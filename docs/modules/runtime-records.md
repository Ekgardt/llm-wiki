# Runtime metadata and reads

`llm_wiki.runtime_records` owns containment, regular-file/size/owner checks,
bounded reads and file identity. It depends on runtime values, environment policy
and lazy Windows handle identity. It owns no connection, transaction, admission,
streaming hash or deadline policy; offline fingerprints have a separate owner.

validate_operational_db_file uses metadata alone. Opening and closing another
file descriptor for a live SQLite inode can drop the process's POSIX advisory
locks; this path must never do that. Regular runtime validation and reads retain
no-follow descriptors and identity/readback checks. Explicit byte budgets keep
their original meaning. Containment follows freshly resolved parent authority;
it is not an OS sandbox against arbitrary external writers.

Public callable pickle paths and old reliable_memory exports remain unchanged.
Run `uv run pytest tests/modules/runtime_records -q`; cross-owner lock preservation
is also covered by test_adoption_preserves_an_open_database_lock.py.

## Canonical ownership reference

Shared canonical contract: [runtime-contracts](runtime-contracts.md).
The complete guide is included in this owner's maintenance packet.
