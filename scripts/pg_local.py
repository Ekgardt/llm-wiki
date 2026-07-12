"""Manage a vault-local PostgreSQL instance (initdb / pg_ctl start-stop).

This module provisions a self-contained PostgreSQL data directory
inside the vault's cache/ zone (gitignored, rebuildable). It is the
recommended setup for users who want the PostgreSQL backend without
a system-level PostgreSQL installation.

The data directory lives at: $LLMWIKI_STATE_ROOT/cache/postgres/
The server listens on:        127.0.0.1:$LLMWIKI_PG_PORT (default 5433)

Usage:
    python scripts/pg_local.py init     # initdb + start + createdb + schema
    python scripts/pg_local.py start    # start existing instance
    python scripts/pg_local.py stop     # stop instance
    python scripts/pg_local.py status   # check if running
    python scripts/pg_local.py reset    # stop + rm data dir (full rebuild)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from memory_state import STATE_ROOT  # noqa: E402

PG_DATA_DIR = STATE_ROOT / "cache" / "postgres"
PG_LOG_FILE = STATE_ROOT / "logs" / "postgres.log"
PG_PID_FILE = STATE_ROOT / "run" / "pg_server.pid"
PG_DEFAULT_PORT = 5433
PG_DEFAULT_DB = "llmwiki"
PG_USER = "postgres"


def _port() -> int:
    return int(os.environ.get("LLMWIKI_PG_PORT", PG_DEFAULT_PORT))


def _dbname() -> str:
    return os.environ.get("LLMWIKI_PG_DB", PG_DEFAULT_DB)


def _dsn(dbname: str | None = None) -> str:
    db = dbname or _dbname()
    return f"host=127.0.0.1 port={_port()} user={PG_USER} dbname={db}"


def pg_bin(name: str) -> str | None:
    """Locate a PostgreSQL binary on PATH or via LLMWIKI_PG_BINDIR."""
    bindir = os.environ.get("LLMWIKI_PG_BINDIR")
    if bindir:
        p = Path(bindir) / name
        return str(p) if p.exists() else None
    return shutil.which(name)


def is_initialized() -> bool:
    """True if the data directory has been initdb'd."""
    return (PG_DATA_DIR / "PG_VERSION").exists()


def is_running() -> bool:
    """True if the local PostgreSQL instance is accepting connections."""
    ctl = pg_bin("pg_ctl")
    if ctl and is_initialized():
        r = subprocess.run(
            [ctl, "-D", str(PG_DATA_DIR), "status"],
            capture_output=True, text=True,
        )
        return r.returncode == 0
    return False


def initdb() -> None:
    """Initialize the data directory. Idempotent — skips if already done."""
    if is_initialized():
        return
    initdb_bin = pg_bin("initdb")
    if not initdb_bin:
        raise FileNotFoundError(
            "initdb not found. Install PostgreSQL or set LLMWIKI_PG_BINDIR."
        )
    PG_DATA_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            initdb_bin,
            "-D", str(PG_DATA_DIR),
            "-U", PG_USER,
            "--auth=trust",
            "--locale=C",
            "--encoding=UTF8",
        ],
        check=True,
        capture_output=True,
    )
    _configure_port()


def _configure_port() -> None:
    """Pin the port and bind localhost only in postgresql.conf."""
    cfg = PG_DATA_DIR / "postgresql.conf"
    if not cfg.exists():
        return
    txt = cfg.read_text(encoding="utf-8")
    addition = (
        f"\n# llm-wiki local instance\n"
        f"port = {_port()}\n"
        f"listen_addresses = '127.0.0.1'\n"
        f"unix_socket_directories = ''\n"
    )
    if "llm-wiki local instance" not in txt:
        cfg.write_text(txt + addition, encoding="utf-8")


def start() -> None:
    """Start the local PostgreSQL instance via pg_ctl."""
    if is_running():
        return
    if not is_initialized():
        initdb()
    ctl = pg_bin("pg_ctl")
    if not ctl:
        raise FileNotFoundError("pg_ctl not found")
    PG_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ctl, "-D", str(PG_DATA_DIR),
            "-l", str(PG_LOG_FILE),
            "-w", "-t", "30",
            "start",
        ],
        check=True,
        capture_output=True,
    )
    PG_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PG_PID_FILE.write_text(str(_port()), encoding="utf-8")


def stop() -> None:
    """Stop the local PostgreSQL instance."""
    ctl = pg_bin("pg_ctl")
    if ctl and is_running():
        subprocess.run(
            [ctl, "-D", str(PG_DATA_DIR), "-m", "fast", "-w", "stop"],
            check=True, capture_output=True,
        )
    if PG_PID_FILE.exists():
        PG_PID_FILE.unlink()


def createdb() -> None:
    """Create the llmwiki database + pgvector extension + schema."""
    import psycopg
    admin_dsn = _dsn("postgres")
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (_dbname(),)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{_dbname()}"')

    from pg_store import _configure_conn, init_schema
    dsn = _dsn()
    with psycopg.connect(dsn, autocommit=True) as conn:
        _configure_conn(conn)
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")

    os.environ["LLMWIKI_PG_DSN"] = dsn
    init_schema(dsn)


def setup_full() -> None:
    """One-command setup: initdb → start → createdb → schema."""
    initdb()
    start()
    createdb()
    print(f"PostgreSQL ready: {_dsn()}")
    print(f"Data dir: {PG_DATA_DIR}")
    print(f"Set LLMWIKI_PG_DSN={_dsn()} to use it.")


def reset() -> None:
    """Stop + delete data directory (full rebuild from Markdown)."""
    stop()
    if PG_DATA_DIR.exists():
        shutil.rmtree(PG_DATA_DIR, ignore_errors=True)
    print("PostgreSQL data directory removed. Run 'init' to rebuild.")


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Manage vault-local PostgreSQL.")
    p.add_argument("command", choices=["init", "start", "stop", "status", "reset", "setup"])
    args = p.parse_args()

    if args.command == "status":
        print(f"Data dir:  {PG_DATA_DIR}")
        print(f"Initialized: {is_initialized()}")
        print(f"Running:   {is_running()}")
        if is_running():
            print(f"DSN:       {_dsn()}")
        return 0

    try:
        if args.command == "init":
            initdb()
            print(f"Initialized: {PG_DATA_DIR}")
        elif args.command == "start":
            start()
            print("Started.")
        elif args.command == "stop":
            stop()
            print("Stopped.")
        elif args.command == "reset":
            reset()
        elif args.command == "setup":
            setup_full()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"Error: {e.stderr.decode() if e.stderr else e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
