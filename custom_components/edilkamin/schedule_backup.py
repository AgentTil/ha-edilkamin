"""User-readable, atomic JSON backups with bounded per-stove retention."""

from pathlib import Path
import re
import shutil
from uuid import uuid4

from homeassistant.helpers.json import save_json
from homeassistant.util import dt as dt_util

BACKUP_LIMIT = 20
BACKUP_NAME = re.compile(r"schedule-\d{8}T\d{6}Z-[0-9a-f]{32}")


def prune_backups(root: Path, protected: set[str]) -> None:
    """Prune only our own directories; never follow links or remove recovery data."""
    folders = sorted(
        (
            path
            for path in root.iterdir()
            if BACKUP_NAME.fullmatch(path.name)
            and path.is_dir()
            and not path.is_symlink()
        ),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    for path in folders[BACKUP_LIMIT:]:
        if str(path) not in protected:
            shutil.rmtree(path)


def create_backup(  # noqa: PLR0913 - five audit inputs and recovery protection
    root: str,
    mac: str,
    before: dict,
    request: dict,
    desired: dict,
    *,
    protected: set[str] | None = None,
) -> str:
    """Save recovery evidence before the command; retain at most 20 recent edits."""
    device = mac.replace(":", "").lower()
    if not re.fullmatch(r"[0-9a-f]{12}", device):
        message = "Invalid stove MAC address for backup storage."
        raise ValueError(message)
    parent = Path(root) / "edilkamin-backups" / device
    folder = parent / (
        "schedule-" + dt_util.utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex
    )
    folder.mkdir(parents=True, exist_ok=False)
    for name, data in {
        "before.json": {"mac_address": mac, "chrono": before},
        "request.json": request,
        "proposed-command.json": {
            "name": "chrono_temperature_ranges",
            "value": desired["temperature_ranges"],
        },
    }.items():
        save_json(str(folder / name), data, private=True)
    prune_backups(parent, (protected or set()) | {str(folder)})
    return str(folder)


def save_result(folder: str, result: dict, actual: dict | None = None) -> None:
    """Use HA's atomic JSON writer rather than a second persistence implementation."""
    save_json(
        str(Path(folder) / "result.json"),
        {"result": result, "readback": actual},
        private=True,
    )
