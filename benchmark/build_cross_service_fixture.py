"""Build the two-repository fixture the cross-service parity tasks ask about.

This repository serves no HTTP route, so a cross-service question against it
would be vacuous. The fixture is two tiny Git repositories -- one client, one
service -- so both tools under comparison can index them the ordinary way:

    uv run python benchmark/build_cross_service_fixture.py --out /tmp/parity-cs

It writes `<out>/service-repo` and `<out>/client-repo`, commits both, and
prints the client path, which is the `--directory` of the parity run:

    uv run python benchmark/run_code_parity.py \\
      --tasks benchmark/code-parity-cross-service-v1.json \\
      --directory /tmp/parity-cs/client-repo

Nothing here touches the vault, and the fixture is disposable.
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

SERVICE_FILES = {
    "orders/__init__.py": "",
    "orders/api.py": (
        "from fastapi import APIRouter\n"
        "\n"
        "from orders.store import place_order\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        "\n"
        '@router.post("/orders")\n'
        "def create_order(payload):\n"
        "    return place_order(payload, 1)\n"
        "\n"
        "\n"
        '@router.get("/orders")\n'
        "def list_orders():\n"
        "    return []\n"
    ),
    "orders/store.py": (
        "def place_order(record, attempt):\n"
        "    return (record, attempt)\n"
    ),
}

CLIENT_FILES = {
    "shop/__init__.py": "",
    "shop/checkout.py": (
        "import requests\n"
        "\n"
        "\n"
        "def submit(basket):\n"
        '    return requests.post("https://orders.internal/orders", json=basket)\n'
        "\n"
        "\n"
        "def history():\n"
        '    return requests.get("https://orders.internal/orders")\n'
    ),
}


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def _write(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def build_repository(root: Path, files: dict[str, str]) -> Path:
    """One committed Git repository; an existing directory is refused, not reused."""
    root.mkdir(parents=True)
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "parity@example.invalid")
    _git(root, "config", "user.name", "parity fixture")
    _write(root, files)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "cross-service parity fixture")
    return root


def build(out: Path) -> tuple[Path, Path]:
    service = build_repository(out / "service-repo", SERVICE_FILES)
    client = build_repository(out / "client-repo", CLIENT_FILES)
    return service, client


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    arguments = parser.parse_args()
    service, client = build(arguments.out.resolve())
    print(f"service: {service}")
    print(f"client:  {client}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
