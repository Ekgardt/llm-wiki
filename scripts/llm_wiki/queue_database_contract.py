"""Supported queue database identities and supplied-connection validation."""
from __future__ import annotations

import sqlite3

import llm_wiki.compile_journal_schema as compile_progress
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.queue_schema import QUEUE_V3_INDEX_SQL, QUEUE_V3_TABLE_SQL, QUEUE_V3_TRIGGER_SQL
from llm_wiki.sqlite_contracts import OperationalDatabaseContract, OperationalDatabaseContractError
from llm_wiki.sqlite_pragmas import validate_or_initialize_operational_contract

QUEUE_V3_CONTRACT = OperationalDatabaseContract(application_id=0x4C575133)

QUEUE_V3_SCHEMA_SHA256 = sha256_bytes(
    canonical_json_bytes(
        {
            "tables": [list(item) for item in QUEUE_V3_TABLE_SQL],
            "indexes": [list(item) for item in QUEUE_V3_INDEX_SQL],
            "triggers": [list(item) for item in QUEUE_V3_TRIGGER_SQL],
            "application_id": QUEUE_V3_CONTRACT.application_id,
            "user_version": QUEUE_V3_CONTRACT.user_version,
        }
    )
)

QUEUE_COMPILE_SCHEMA_SHA256 = sha256_bytes(canonical_json_bytes({
    "base": QUEUE_V3_SCHEMA_SHA256,
    "version": compile_progress.QUEUE_SCHEMA_VERSION,
    "journal": list(compile_progress.SCHEMA_STATEMENTS),
}))

QUEUE_COMPILE_CONTRACT = OperationalDatabaseContract(
    application_id=QUEUE_V3_CONTRACT.application_id,
    user_version=compile_progress.QUEUE_SCHEMA_VERSION,
)

def queue_contract_for_version(version: int) -> OperationalDatabaseContract:
    contracts = {QUEUE_V3_CONTRACT.user_version: QUEUE_V3_CONTRACT,
                 compile_progress.QUEUE_SCHEMA_VERSION: QUEUE_COMPILE_CONTRACT}
    if version not in contracts:
        raise OperationalDatabaseContractError("unsupported queue database version")
    return contracts[version]

def require_supported_queue_contract(database: sqlite3.Connection) -> None:
    version = database.execute("PRAGMA user_version").fetchone()[0]
    contract = queue_contract_for_version(version)
    validate_or_initialize_operational_contract(database, contract, initialize=False)
    if version == compile_progress.QUEUE_SCHEMA_VERSION:
        compile_progress.require_schema(database)

def checked_queue_connection(database: sqlite3.Connection, expected_contract=None) -> sqlite3.Connection:
    try:
        require_supported_queue_contract(database)
        if expected_contract is not None:
            validate_or_initialize_operational_contract(database, expected_contract, initialize=False)
        return database
    except BaseException:
        database.close()
        raise
