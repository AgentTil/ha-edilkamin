"""HA target extraction, service calls and service lifecycle."""

from copy import deepcopy
import inspect
from unittest.mock import AsyncMock, Mock

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
    label_registry as lr,
)
import pytest
import pytest_asyncio
import voluptuous as vol

from custom_components.edilkamin import async_setup
from custom_components.edilkamin.const import DOMAIN
from custom_components.edilkamin.schedule_edit import ScheduleWriter
from custom_components.edilkamin.schedule_restore import TemporaryScheduleWriter


@pytest_asyncio.fixture
async def context(tmp_path):
    """Always stop HA, including after failed assertions."""
    hass = HomeAssistant(str(tmp_path))
    entries = {}
    hass.config_entries = Mock(
        async_get_entry=Mock(side_effect=entries.get),
        async_entries=Mock(return_value=[]),
        async_shutdown=AsyncMock(),
        async_wait_initialized=AsyncMock(),
    )
    for registry in (fr, ar, lr, dr, er):
        if (setup := getattr(registry, "async_setup", None)) and inspect.isawaitable(
            result := setup(hass)
        ):
            await result
        await registry.async_load(hass)
    await async_setup(hass, {})
    try:
        yield hass, entries
    finally:
        await hass.async_stop(force=True)


def add_stove(hass, entries, entry_id):
    info = {
        "nvm": {
            "user_parameters": {"is_auto": True},
            "chrono": {
                "is_active": True,
                "temperature_ranges": [0] * 336,
                "power_ranges": [0] * 336,
                "economy_temperature": 20,
                "comfort_temperature": 22,
            },
        }
    }
    api = Mock(
        get_info=AsyncMock(return_value=deepcopy(info)), execute_command=AsyncMock()
    )
    coordinator = Mock(async_request_refresh=AsyncMock())
    coordinator.get_mac_address.return_value = "00:11:22:33:44:55"
    fields = {
        "entry_id": entry_id,
        "domain": DOMAIN,
        "data": {},
        "options": {},
        "version": 1,
        "minor_version": 1,
        "title": entry_id,
        "unique_id": entry_id,
        "source": "user",
        "discovery_keys": {},
        "subentries_data": [],
        "pref_disable_new_entities": False,
    }
    # Optional constructor fields changed across the supported HA versions.
    parameters = inspect.signature(ConfigEntry).parameters
    entry = ConfigEntry(
        **{key: value for key, value in fields.items() if key in parameters}
    )
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    entry.runtime_data = ScheduleWriter(hass, api, coordinator)
    entries[entry_id] = entry
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry_id, identifiers={(DOMAIN, entry_id)}
    )
    entity = er.async_get(hass).async_get_or_create(
        "climate", DOMAIN, entry_id, config_entry=entry, device_id=device.id
    )
    return entry, device, entity, api


def request(**fields):
    return {
        "days": ["tuesday"],
        "start": "23:30:00",
        "end": "00:00:00",
        "mode": "comfort",
        "preview_only": True,
        **fields,
    }


@pytest.mark.asyncio
async def test_action_exists_without_loaded_entries(context):
    hass, _ = context
    assert hass.services.has_service(DOMAIN, "set_schedule")
    with pytest.raises(ServiceValidationError) as error:
        await hass.services.async_call(
            DOMAIN, "set_schedule", request(device_id="missing"), blocking=True
        )
    assert error.value.translation_key == "invalid_target"


@pytest.mark.asyncio
async def test_real_device_and_entity_targets_route_to_one_entry(context):
    hass, entries = context
    first = add_stove(hass, entries, "first")
    second = add_stove(hass, entries, "second")
    for target in ({"device_id": second[1].id}, {"entity_id": second[2].entity_id}):
        result = await hass.services.async_call(
            DOMAIN,
            "set_schedule",
            request(**target),
            blocking=True,
            return_response=True,
        )
        assert result["changed_slots"] == [95]
        assert result["end"] == "24:00"
    first[3].get_info.assert_not_called()
    assert second[3].get_info.await_count == 2
    second[3].execute_command.assert_not_called()


@pytest.mark.asyncio
async def test_unload_keeps_action_and_raises_translated_error(context):
    hass, entries = context
    entry, device, _, api = add_stove(hass, entries, "first")
    entry.runtime_data = None
    entry._async_set_state(hass, ConfigEntryState.NOT_LOADED, None)
    assert hass.services.has_service(DOMAIN, "set_schedule")
    with pytest.raises(ServiceValidationError) as error:
        await hass.services.async_call(
            DOMAIN, "set_schedule", request(device_id=device.id), blocking=True
        )
    assert error.value.translation_domain == DOMAIN
    assert error.value.translation_key == "invalid_target"
    api.get_info.assert_not_called()


@pytest.mark.asyncio
async def test_multi_stove_and_invalid_time_never_read_cloud(context):
    hass, entries = context
    first = add_stove(hass, entries, "first")
    second = add_stove(hass, entries, "second")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "set_schedule",
            request(device_id=[first[1].id, second[1].id]),
            blocking=True,
        )
    with pytest.raises((ServiceValidationError, vol.Invalid)):
        await hass.services.async_call(
            DOMAIN,
            "set_schedule",
            request(device_id=first[1].id, start="23:15"),
            blocking=True,
        )
    first[3].get_info.assert_not_called()
    second[3].get_info.assert_not_called()


@pytest.mark.asyncio
async def test_once_preview_uses_actual_service_schema(context):
    hass, entries = context
    entry, device, _, api = add_stove(hass, entries, "temporary")
    entry.runtime_data = TemporaryScheduleWriter(
        hass,
        api,
        entry.runtime_data.coordinator,
        entry.entry_id,
    )
    await entry.runtime_data.async_start()
    try:
        result = await hass.services.async_call(
            DOMAIN,
            "set_schedule",
            request(device_id=device.id, auto_restore="true"),
            blocking=True,
            return_response=True,
        )
        assert result["recurrence"] == "once"
        assert result["restore_at"]
        assert not entry.runtime_data.windows
        api.execute_command.assert_not_called()
    finally:
        await entry.runtime_data.async_stop()
