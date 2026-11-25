"""Register schedule actions once; resolve targets with Home Assistant helpers."""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.helpers import config_validation as cv, service
import voluptuous as vol

from .const import DOMAIN
from .schedule_edit import ScheduleWriter
from .schedule_errors import fail
from .schedule_schema import EDIT_FIELDS, validate_range

ACTION = "set_schedule"
ACTION_SCHEMA = vol.All(cv.make_entity_service_schema(EDIT_FIELDS), validate_range)


async def async_setup_services(hass: HomeAssistant) -> None:
    """Keep the action available even while config entries are unloaded."""

    async def handle(call: ServiceCall) -> ServiceResponse:
        entry_ids = await service.async_extract_config_entry_ids(call)
        entries = [
            hass.config_entries.async_get_entry(entry_id) for entry_id in entry_ids
        ]
        entries = [
            entry for entry in entries if entry is not None and entry.domain == DOMAIN
        ]
        if len(entries) != 1 or entries[0].state is not ConfigEntryState.LOADED:
            fail("invalid_target")
        writer = getattr(entries[0], "runtime_data", None)
        if not isinstance(writer, ScheduleWriter):
            fail("invalid_target")
        request = {
            key: value
            for key, value in call.data.items()
            if key not in cv.ENTITY_SERVICE_FIELDS
        }
        return await writer.set_schedule(request)

    hass.services.async_register(
        DOMAIN,
        ACTION,
        handle,
        schema=ACTION_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
