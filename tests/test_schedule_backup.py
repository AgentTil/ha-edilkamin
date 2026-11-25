"""Atomic backup contents and retention never remove unrelated directories."""

import json
from pathlib import Path
from unittest.mock import patch

from custom_components.edilkamin.schedule_backup import create_backup, save_result


def test_retention_and_atomic_contents(tmp_path):
    mac = "00:11:22:33:44:55"
    root = tmp_path / "edilkamin-backups" / "001122334455"
    root.mkdir(parents=True)
    foreign = root / "user-export"
    foreign.mkdir()
    (foreign / "keep.json").write_text("{}")
    before = {"temperature_ranges": [0] * 336}
    folders = []
    for _ in range(23):
        folder = create_backup(
            str(tmp_path), mac, before, {"preview_only": False}, before
        )
        folders.append(Path(folder))
        save_result(folder, {"status": "verified"}, before)
    assert len([p for p in root.iterdir() if p.name.startswith("schedule-")]) == 20
    assert (foreign / "keep.json").exists()
    assert json.loads((folders[-1] / "result.json").read_text())["readback"] == before
    assert not list(root.rglob("*.tmp"))


def test_current_backup_and_recovery_evidence_are_never_pruned(tmp_path):
    mac = "00:11:22:33:44:55"
    before = {"temperature_ranges": [0] * 336}
    protected = create_backup(str(tmp_path), mac, before, {}, before)
    with patch("custom_components.edilkamin.schedule_backup.BACKUP_LIMIT", 1):
        current = create_backup(
            str(tmp_path), mac, before, {}, before, protected={protected}
        )
    assert (Path(protected) / "before.json").exists()
    assert (Path(current) / "before.json").exists()


def test_another_stoves_backups_are_not_pruned(tmp_path):
    before = {"temperature_ranges": [0] * 336}
    first = create_backup(str(tmp_path), "00:11:22:33:44:55", before, {}, before)
    with patch("custom_components.edilkamin.schedule_backup.BACKUP_LIMIT", 1):
        create_backup(str(tmp_path), "aa:bb:cc:dd:ee:ff", before, {}, before)
        create_backup(str(tmp_path), "aa:bb:cc:dd:ee:ff", before, {}, before)
    assert (Path(first) / "before.json").exists()
