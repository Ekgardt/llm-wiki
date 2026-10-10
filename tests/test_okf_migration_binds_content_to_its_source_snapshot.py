from __future__ import annotations

import hashlib

import migrate_to_okf


def test_migration_plan_uses_the_bytes_its_precondition_names(tmp_path, monkeypatch):
    notes = tmp_path / "knowledge/notes"
    notes.mkdir(parents=True)
    page = notes / "snapshot.md"
    before = b"# Original\n\nThis is the original source.\n"
    after = b"# Changed\n\nThis was written after the snapshot.\n"
    page.write_bytes(before)
    monkeypatch.setattr(migrate_to_okf, "ROOT", tmp_path)
    read = migrate_to_okf.read_stable_bytes

    def change_after_read(path, limit, **kwargs):
        data = read(path, limit, **kwargs)
        page.write_bytes(after)
        return data

    monkeypatch.setattr(migrate_to_okf, "read_stable_bytes", change_after_read)
    plan = migrate_to_okf._plan_migration([page])
    assert plan.source_hashes[page] == hashlib.sha256(before).hexdigest()
    assert len(plan.pages) == 1
    assert plan.pages[0][1].endswith(before.decode())
    assert page.read_bytes() == after


def test_migration_accepts_a_page_within_the_existing_transaction_budget(tmp_path, monkeypatch):
    notes = tmp_path / "knowledge/notes"
    notes.mkdir(parents=True)
    page = notes / "large.md"
    data = b"# Page\n" + b"x" * (16 * 1024 * 1024)
    page.write_bytes(data)
    monkeypatch.setattr(migrate_to_okf, "ROOT", tmp_path)
    plan = migrate_to_okf._plan_migration([page])
    assert plan.counts == {"migrate": 1}
    assert plan.source_hashes[page] == hashlib.sha256(data).hexdigest()
    assert plan.pages[0][1].endswith(data.decode())
