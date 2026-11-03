"""Supported queue database identities and supplied-connection validation."""
from __future__ import annotations

import sqlite3

import llm_wiki.compile_journal_schema as compile_progress
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.queue_schema import _QUEUE_V3_INDEX_SQL, _QUEUE_V3_TABLE_SQL, _QUEUE_V3_TRIGGER_SQL
from llm_wiki.sqlite_contracts import OperationalDatabaseContract, OperationalDatabaseContractError
from llm_wiki.sqlite_pragmas import _validate_or_initialize_operational_contract

_QUEUE_V3_CONTRACT = OperationalDatabaseContract(application_id=0x4C575133)

QUEUE_V3_SCHEMA_SHA256 = sha256_bytes(
    canonical_json_bytes(
        {
            "tables": [list(item) for item in _QUEUE_V3_TABLE_SQL],
            "indexes": [list(item) for item in _QUEUE_V3_INDEX_SQL],
            "triggers": [list(item) for item in _QUEUE_V3_TRIGGER_SQL],
            "application_id": _QUEUE_V3_CONTRACT.application_id,
            "user_version": _QUEUE_V3_CONTRACT.user_version,
        }
    )
)

QUEUE_COMPILE_SCHEMA_SHA256 = sha256_bytes(canonical_json_bytes({
    "base": QUEUE_V3_SCHEMA_SHA256,
    "version": compile_progress.QUEUE_SCHEMA_VERSION,
    "journal": list(compile_progress.SCHEMA_STATEMENTS),
}))

_QUEUE_COMPILE_CONTRACT = OperationalDatabaseContract(
    application_id=_QUEUE_V3_CONTRACT.application_id,
    user_version=compile_progress.QUEUE_SCHEMA_VERSION,
)

def _queue_contract_for_version(version: int) -> OperationalDatabaseContract:
    contracts = {_QUEUE_V3_CONTRACT.user_version: _QUEUE_V3_CONTRACT,
                 compile_progress.QUEUE_SCHEMA_VERSION: _QUEUE_COMPILE_CONTRACT}
    if version not in contracts:
        raise OperationalDatabaseContractError("unsupported queue database version")
    return contracts[version]

def _require_supported_queue_contract(database: sqlite3.Connection) -> None:
    version = database.execute("PRAGMA user_version").fetchone()[0]
    contract = _queue_contract_for_version(version)
    _validate_or_initialize_operational_contract(database, contract, initialize=False)
    if version == compile_progress.QUEUE_SCHEMA_VERSION:
        compile_progress._require_schema(database)

def _checked_queue_connection(database: sqlite3.Connection, expected_contract=None) -> sqlite3.Connection:
    try:
        _require_supported_queue_contract(database)
        if expected_contract is not None:
            _validate_or_initialize_operational_contract(database, expected_contract, initialize=False)
        return database
    except BaseException:
        database.close()
        raise
