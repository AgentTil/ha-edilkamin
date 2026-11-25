"""Read/modify/write weekly chrono data without changing operating modes."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from functools import partial
import logging
from typing import TYPE_CHECKING

from aiohttp import ClientError
from homeassistant.exceptions import HomeAssistantError

from .schedule_backup import create_backup, save_result
from .schedule_errors import fail, operation_error
from .schedule_schema import DAYS, MODES, half_hour

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceResponse

    from .api.edilkamin_async_api import EdilkaminAsyncApi
    from .coordinator import EdilkaminCoordinator

_LOGGER = logging.getLogger(__name__)
EXPECTED_ERRORS = (TimeoutError, ClientError, OSError, HomeAssistantError)


def snapshot(info: dict) -> dict:
    """Validate full weekly data, not an empty fallback for missing values."""
    nvm = info.get("nvm") if isinstance(info, dict) else None
    if not isinstance(nvm, dict) or not isinstance(nvm.get("user_parameters"), dict):
        msg = "invalid_cloud_schedule"
        raise operation_error(msg)
    auto = nvm["user_parameters"].get("is_auto")
    if type(auto) not in (bool, int) or auto not in (0, 1):
        msg = "invalid_cloud_schedule"
        raise operation_error(msg)
    if not auto:
        fail("manual_mode")
    chrono = nvm.get("chrono")
    if not isinstance(chrono, dict):
        msg = "invalid_cloud_schedule"
        raise operation_error(msg)
    active = chrono.get("is_active")
    if type(active) not in (bool, int) or active not in (0, 1):
        msg = "invalid_cloud_schedule"
        raise operation_error(msg)
    for key, maximum in (("temperature_ranges", 2), ("power_ranges", 5)):
        values = chrono.get(key)
        if (
            not isinstance(values, list)
            or len(values) != 336
            or any(
                type(value) is not int or not 0 <= value <= maximum for value in values
            )
        ):
            msg = "invalid_cloud_schedule"
            raise operation_error(msg)
    return deepcopy(chrono)


def edit(chrono: dict, request: dict) -> dict:
    result = deepcopy(chrono)
    start, end = half_hour(request["start"]), half_hour(request["end"], end=True)
    for day in set(request["days"]):
        offset = DAYS.index(day) * 48
        result["temperature_ranges"][offset + start : offset + end] = [
            MODES[request["mode"]]
        ] * (end - start)
    return result


def changed_slots(before: list[int], after: list[int]) -> list[int]:
    return [
        index
        for index, (old, new) in enumerate(zip(before, after, strict=True))
        if old != new
    ]


def verified(before: dict, desired: dict, actual: dict) -> bool:
    """The cloud may mirror a shared schedule under both array names."""
    if actual["temperature_ranges"] != desired["temperature_ranges"]:
        return False
    if actual["power_ranges"] not in (
        before["power_ranges"],
        desired["temperature_ranges"],
    ):
        return False
    excluded = {"temperature_ranges", "power_ranges"}
    return {key: value for key, value in before.items() if key not in excluded} == {
        key: value for key, value in actual.items() if key not in excluded
    }


class ScheduleWriter:
    """Serialize edits per config entry, with fresh cloud reads and backups."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: EdilkaminAsyncApi,
        coordinator: EdilkaminCoordinator,
    ) -> None:
        self.hass, self.api, self.coordinator = hass, api, coordinator
        self.lock = asyncio.Lock()
        self.last_result = None

    async def _read(self) -> dict:
        async with asyncio.timeout(20):
            return snapshot(await self.api.get_info())

    async def _send_verified(self, before: dict, desired: dict) -> dict:
        # No automatic write retry: a timeout does not prove a command was rejected.
        async with asyncio.timeout(20):
            await self.api.execute_command(
                {
                    "name": "chrono_temperature_ranges",
                    "value": desired["temperature_ranges"],
                }
            )
        async with asyncio.timeout(45):
            for attempt in range(8):
                if attempt:
                    await asyncio.sleep(3)
                actual = await self._read()
                if verified(before, desired, actual):
                    return actual
        msg = "readback_mismatch"
        raise operation_error(msg)

    def _describe_edit(
        self, request: dict, before: dict, desired: dict
    ) -> tuple[dict, list[int]]:
        modified = changed_slots(
            before["temperature_ranges"], desired["temperature_ranges"]
        )
        result = {
            "status": "preview" if request["preview_only"] else "unchanged",
            "recurrence": "weekly",
            "days": list(dict.fromkeys(request["days"])),
            "start": request["start"],
            "end": request["end"],
            "mode": request["mode"],
            "changed_slots": modified,
            "chrono_active": bool(before["is_active"]),
            "write_attempts": 0,
        }
        return result, modified

    def _representations_safe(self, chrono: dict) -> bool:
        """Reject unexplained divergence, including after a confirmed write."""
        return chrono["temperature_ranges"] == chrono["power_ranges"]

    def _protected_backups(self) -> set[str]:
        """Subclasses may retain evidence needed by pending recovery."""
        return set()

    async def _backup(self, before: dict, request: dict, desired: dict) -> str:
        """Share audited persistence between writes and automatic restoration."""
        save = partial(
            create_backup,
            self.hass.config.path(),
            self.coordinator.get_mac_address(),
            before,
            request,
            desired,
            protected=self._protected_backups(),
        )
        return await self.hass.async_add_executor_job(save)

    async def _before_write(
        self,
        request: dict,
        result: dict,
        before: dict,
        modified: list[int],
        folder: str,
    ) -> None:
        """Extension point to persist recovery before an optional temporary write."""

    async def _after_verified(self, result: dict) -> None:
        """Extension point to acknowledge a durable temporary window."""

    async def set_schedule(self, request: dict) -> ServiceResponse:
        """Write an action-layer-validated request, with no blind command retry."""
        async with self.lock:
            try:
                before = await self._read()
                if not self._representations_safe(before):
                    fail("representations_differ")
                desired = edit(before, request)
                result, modified = self._describe_edit(request, before, desired)
                if request["preview_only"] or not modified:
                    self.last_result = result
                    return result
                folder = await self._backup(before, request, desired)
                result["backup"] = folder
                if await self._read() != before:
                    result["status"] = "conflict"
                    self.last_result = result
                    await self.hass.async_add_executor_job(save_result, folder, result)
                    fail("schedule_changed")
                result.update(status="pending", write_attempts=1)
                await self.hass.async_add_executor_job(save_result, folder, result)
                await self._before_write(request, result, before, modified, folder)
                actual = None
                try:
                    actual = await self._send_verified(before, desired)
                    result.update(
                        status="verified",
                        power_ranges_changed_slots=changed_slots(
                            before["power_ranges"], actual["power_ranges"]
                        ),
                    )
                    await self._after_verified(result)
                except EXPECTED_ERRORS as error:
                    result.update(status="unconfirmed", error=type(error).__name__)
                    self.last_result = result
                    await self.hass.async_add_executor_job(
                        save_result, folder, result, actual
                    )
                    _LOGGER.warning(
                        "Schedule write unconfirmed (%s); backup: %s",
                        type(error).__name__,
                        folder,
                    )
                    msg = "write_unconfirmed"
                    raise operation_error(msg) from error
                if not self._representations_safe(actual):
                    result.update(
                        status="verified_with_warning", warning="representations_differ"
                    )
                    _LOGGER.warning(
                        "Schedule written, but cloud representations differ; "
                        "further edits are refused until they match. "
                        "Check The Mind. Backup: %s",
                        folder,
                    )
                self.last_result = result
                await self.hass.async_add_executor_job(
                    save_result, folder, result, actual
                )
                await self._refresh()
            except HomeAssistantError:
                raise
            except (TimeoutError, ClientError, OSError) as error:
                _LOGGER.warning("Schedule editing failed: %s", type(error).__name__)
                msg = "schedule_failed"
                raise operation_error(msg) from error
            else:
                return result
            finally:
                self._notify()

    async def _refresh(self) -> None:
        """A later refresh failure must not turn a verified write into an error."""
        try:
            async with asyncio.timeout(20):
                await self.coordinator.async_request_refresh()
        except EXPECTED_ERRORS as error:
            _LOGGER.warning(
                "Schedule written; coordinator refresh failed: %s", type(error).__name__
            )

    def _notify(self) -> None:
        """Expose action results through the existing schedule sensor."""
        self.coordinator.schedule_edit_status = {
            "last_schedule_edit": deepcopy(self.last_result)
        }
        self.coordinator.async_update_listeners()
