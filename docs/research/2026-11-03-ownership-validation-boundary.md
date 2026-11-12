# Ownership input validation and marker representation

Date: 2026-11-03. Investigation under the approved whole-project mandate;
not implemented or qualified by this document.

The registry combines pure checks of incoming values and stored marker fields with
admission, process observation, SQLite transitions and marker filesystem ownership.
A prospective ownership_validation module can own the former without acquiring
leases or deciding whether a process is dead. Keep process probes, role timing,
clock reads, registry SQL, marker publication/removal and heartbeat in the registry.
The existing ownership_contracts remains a value-definition owner.

Candidate scope: bounded UTF-8 text, process/role input validation, marker metadata,
canonical marker JSON encode/decode, SQLite-row decoding, marker presence/path
validation and comparison of supplied before/after identities and content digest.
Reuse existing runtime identity, canonical JSON/hash, restricted path and process
integer contracts. No replacement validators, generic schema framework or new limits.
Preserve all current failure classes, messages, catch boundaries and checks. Parsing
a marker record is not authority to admit a process or delete a file. Keep SQL and
filesystem observations with their current owner. Time formatting/parsing and lease
timing remain in the registry; they are independently testable existing contracts.

Alternatives considered: leave this pure boundary coupled to registry execution;
add a universal validation framework; collect all failures using notifications;
move acquisition and its validation together. The first retains avoidable dependencies,
the second adds machinery without need, notifications change the public failure
contract, and moving acquisition expands the state-owner change unnecessarily.

Primary sources checked on 2026-11-03 (three independent publishers):
- Python Software Foundation, Python 3.14 JSON documentation:
  https://docs.python.org/3.14/library/json.html — decoding and serialization are
  distinct operations; existing bounded input and canonical-byte checks must remain.
- SQLite authors, Datatypes In SQLite:
  https://www.sqlite.org/datatype3.html — SQL values have storage classes; a row
  decoder must retain explicit handling of NULL/TEXT/BLOB rather than assume the
  declared column type proves the in-memory value.
- Martin Fowler, Replacing Throwing Exceptions with Notification in Validations
  (2014, established alternative checked today):
  https://martinfowler.com/articles/replaceThrowWithNotification.html — an alternative
  suited to accumulating user-input errors, rejected here because callers already
  depend on the current fail-fast typed error contract.

Before implementation: refresh/query the built-in graph; inspect all callers and
monkeypatch sites; record the exact approved internal boundary; preserve baseline
valid/invalid outputs and exact AST. Qualification must include isolated imports,
real SQLite row fixtures, malformed/canonical JSON, boundary values, unchanged
registry integration, CCN and the full <=100000-token packet. Native integration
and the current full-regression result remain separate obligations.
