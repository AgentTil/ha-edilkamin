"""Durable, conflict-aware one-off windows on the shared schedule writer."""

from __future__ import annotations

import asyncio
import contextlib
from copy import deepcopy
from datetime import date, datetime, time, timedelta
import logging
import re
from uuid import uuid4

from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .schedule_backup import save_result
from .schedule_edit import EXPECTED_ERRORS, ScheduleWriter, changed_slots
from .schedule_errors import fail, operation_error
from .schedule_schema import DAYS, MODES, half_hour

_LOGGER = logging.getLogger(__name__)


def valid_recovery(record: object) -> bool:
    """Reject malformed durable data even when Python assertions are disabled."""
    try:
        deadline = dt_util.parse_datetime(record["restore_at"])
        return (
            isinstance(record["id"], str)
            and re.fullmatch(r"[0-9a-f]{32}", record["id"]) is not None
            and deadline is not None
            and deadline.tzinfo is not None
            and isinstance(record["original"], dict)
            and bool(record["original"])
            and type(record["applied"]) is int
            and record["applied"] in (0, 1, 2)
            and all(
                str(int(key)) == key
                and 0 <= int(key) < 336
                and type(value) is int
                and value in (0, 1, 2)
                and value != record["applied"]
                for key, value in record["original"].items()
            )
        )
    except (KeyError, TypeError, ValueError):
        return False


def window_deadline(request: dict, now: datetime) -> tuple[str, str]:
    """Resolve one occurrence and persist its unambiguous UTC end, including DST."""
    offset = (DAYS.index(request["days"][0]) - now.weekday()) % 7
    chosen = (
        date.fromisoformat(request["date"])
        if "date" in request
        else now.date() + timedelta(days=offset)
    )
    end_slot = half_hour(request["end"], end=True)

    def boundary(day, slot):
        naive = datetime.combine(day, time()) + timedelta(minutes=slot * 30)
        local = naive.replace(tzinfo=now.tzinfo, fold=1)
        if dt_util.as_utc(local).astimezone(now.tzinfo).replace(tzinfo=None) != naive:
            fail("invalid_window_date")
        return local

    end = boundary(chosen, end_slot)
    if "date" not in request and dt_util.as_utc(end) <= dt_util.as_utc(now):
        chosen += timedelta(days=7)
        end = boundary(chosen, end_slot)
    if not 0 <= (chosen - now.date()).days <= 7 or dt_util.as_utc(
        end
    ) <= dt_util.as_utc(now):
        fail("invalid_window_date")
    # A day seven days ahead is safe only after today's occurrence has ended.
    if (chosen - now.date()).days == 7 and dt_util.as_utc(
        boundary(now.date(), end_slot)
    ) > dt_util.as_utc(now):
        fail("invalid_window_date")
    boundary(chosen, half_hour(request["start"]))
    return chosen.isoformat(), dt_util.as_utc(end).isoformat()


class TemporaryScheduleWriter(ScheduleWriter):
    """Restore only owned changed slots; reuse the permanent write transaction."""

    def __init__(self, hass, api, coordinator, entry_id: str) -> None:
        super().__init__(hass, api, coordinator)
        self.windows = []
        self._store = Store(
            hass,
            1,
            "edilkamin_schedule_windows_" + entry_id,
        )
        self._loaded = False
        self._cancel_timer = None
        self._task = None
        self._stopped = False

    @property
    def pending_windows(self) -> list[dict]:
        return [
            {
                key: record.get(key)
                for key in (
                    "id",
                    "date",
                    "start",
                    "end",
                    "mode",
                    "restore_at",
                    "phase",
                    "last_error",
                )
            }
            for record in self.windows
        ]

    async def async_start(self) -> None:
        if self._loaded:
            return
        saved = await self._store.async_load()
        records = saved.get("windows", []) if isinstance(saved, dict) else []
        if saved is not None and (
            not isinstance(saved, dict)
            or "windows" not in saved
            or not isinstance(records, list)
        ):
            msg = "invalid_recovery"
            raise operation_error(msg)
        seen_ids, seen_slots = set(), set()
        for record in records:
            if not valid_recovery(record):
                msg = "invalid_recovery"
                raise operation_error(msg)
            slots = set(record["original"])
            if record["id"] in seen_ids or slots & seen_slots:
                msg = "invalid_recovery"
                raise operation_error(msg)
            seen_ids.add(record["id"])
            seen_slots.update(slots)
        self.windows = deepcopy(records)
        self._loaded = True
        self._arm(immediate=True)
        self._notify()

    async def _save(self):
        await self._store.async_save({"windows": deepcopy(self.windows)})

    def _arm(self, *, immediate=False):
        if self._cancel_timer:
            self._cancel_timer()
            self._cancel_timer = None
        if self.windows and not self._stopped:
            nearest = min(
                dt_util.parse_datetime(record["restore_at"]) for record in self.windows
            )
            delay = (nearest - dt_util.utcnow()).total_seconds()
            if delay <= 0:
                delay = 0 if immediate else 30
            self._cancel_timer = async_call_later(self.hass, delay, self._timer_fired)

    @callback
    def _timer_fired(self, _now):
        self._cancel_timer = None
        if not self._stopped and (self._task is None or self._task.done()):
            self._task = self.hass.async_create_task(self.restore_due())

    async def async_stop(self) -> None:
        self._stopped = True
        if self._cancel_timer:
            self._cancel_timer()
            self._cancel_timer = None
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def restore_due(self) -> None:
        """Read before each retry; restore only values still matching this window."""
        if self._stopped or not self.windows:
            return
        if self.lock.locked():
            self._arm()
            return
        async with self.lock:
            for record in list(self.windows):
                if dt_util.parse_datetime(record["restore_at"]) > dt_util.utcnow():
                    continue
                try:
                    before = await self._read()
                    self._check_representations(before)
                    changed, preserved = await self._restore_record(record, before)
                    remaining = [
                        item for item in self.windows if item["id"] != record["id"]
                    ]
                    # Save completion before discarding the in-memory recovery record.
                    await self._store.async_save({"windows": deepcopy(remaining)})
                    self.windows = remaining
                    self.last_result = {
                        "status": "restored_with_external_changes"
                        if preserved
                        else "restored",
                        "window_id": record["id"],
                        "restored_slots": changed,
                        "preserved_external_slots": preserved,
                    }

                except EXPECTED_ERRORS as error:
                    record["phase"] = "restore_pending"
                    record["last_error"] = type(error).__name__
                    self.last_result = {
                        "status": "restore_pending",
                        "window_id": record["id"],
                        "error": type(error).__name__,
                    }
                    _LOGGER.exception("Schedule restoration pending")
                    with contextlib.suppress(*EXPECTED_ERRORS):
                        # The pre-write recovery record remains on disk.
                        await self._save()
            self._arm()
            self._notify()
            await self._refresh()

    async def _restore_record(self, record, before):
        desired = deepcopy(before)
        preserved = []
        for key, original in record["original"].items():
            index = int(key)
            current = before["temperature_ranges"][index]
            if current == record["applied"]:
                desired["temperature_ranges"][index] = original
            elif current != original:
                preserved.append(index)
        changed = changed_slots(
            before["temperature_ranges"], desired["temperature_ranges"]
        )
        if changed:
            request = {
                "operation": "automatic_restore",
                "window_id": record["id"],
            }
            folder = await self._backup(before, request, desired)
            record["restore_backup"] = folder
            record["phase"] = "restoring"
            await self._save()
            if await self._read() != before:
                fail("schedule_changed")
            actual = await self._send_verified(before, desired)
            await self.hass.async_add_executor_job(
                save_result,
                folder,
                {
                    "status": "restored",
                    "window_id": record["id"],
                    "preserved_external_slots": preserved,
                },
                actual,
            )
        return changed, preserved

    def _representations_safe(self, before: dict) -> bool:
        # An accepted write may leave power_ranges unchanged. Allow only the
        # differences explained by our durable, nonoverlapping recovery records.
        owned = {
            int(key): (item["applied"], original)
            for item in self.windows
            for key, original in item["original"].items()
        }
        for index, (temperature, power) in enumerate(
            zip(before["temperature_ranges"], before["power_ranges"], strict=True)
        ):
            if temperature != power and owned.get(index) != (temperature, power):
                return False

        return True

    async def _before_write(self, request, result, before, modified, folder) -> None:
        window = None
        if request["auto_restore"]:
            if dt_util.parse_datetime(result["restore_at"]) <= dt_util.utcnow():
                fail("window_expired")
            window = {
                "id": uuid4().hex,
                "date": result["date"],
                "start": request["start"],
                "end": request["end"],
                "mode": request["mode"],
                "restore_at": result["restore_at"],
                "phase": "armed",
                "backup": folder,
                "original": {
                    str(index): before["temperature_ranges"][index]
                    for index in modified
                },
                "applied": MODES[request["mode"]],
            }
            self.windows.append(window)
            try:
                await self._save()
            except EXPECTED_ERRORS:
                self.windows.remove(window)
                raise
            result["window_id"] = window["id"]
            self._arm()
            # Storage may have delayed the action past its end. Keep the durable
            # record for a no-op recovery, but never apply an expired window.
            if dt_util.parse_datetime(result["restore_at"]) <= dt_util.utcnow():
                fail("window_expired")

    def _check_representations(self, before: dict) -> None:
        if not self._representations_safe(before):
            fail("representations_differ")

    def _protected_backups(self) -> set[str]:
        return {
            item[key]
            for item in self.windows
            for key in ("backup", "restore_backup")
            if key in item
        }

    def _describe_edit(self, request: dict, before: dict, desired: dict):
        selected = {
            DAYS.index(day) * 48 + slot
            for day in request["days"]
            for slot in range(
                half_hour(request["start"]), half_hour(request["end"], end=True)
            )
        }
        if any(set(map(int, item["original"])) & selected for item in self.windows):
            fail("window_overlap")
        result, modified = super()._describe_edit(request, before, desired)
        if request["auto_restore"]:
            result["recurrence"] = "once"
            result["date"], result["restore_at"] = window_deadline(
                request, dt_util.now()
            )
        return result, modified

    async def _after_verified(self, result: dict) -> None:
        if window_id := result.get("window_id"):
            record = next(item for item in self.windows if item["id"] == window_id)
            record["phase"] = "active"
            await self._save()

    def _notify(self) -> None:
        self.coordinator.schedule_edit_status = {
            "temporary_windows": self.pending_windows,
            "last_schedule_edit": deepcopy(self.last_result),
        }
        self.coordinator.async_update_listeners()
