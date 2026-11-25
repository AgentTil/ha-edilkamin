"""Translated input/state errors and cloud/storage failures for schedule actions."""

from typing import NoReturn

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import DOMAIN

MESSAGES = {
    "invalid_recovery": (
        "Saved temporary-window recovery data is invalid; no command sent."
    ),
    "invalid_window_date": (
        "Choose a matching occurrence within seven days that has not ended. "
        "Check DST boundaries."
    ),
    "window_overlap": (
        "This range belongs to a temporary window; await its restoration."
    ),
    "window_expired": "The temporary window has ended; no command sent.",
    "invalid_target": "Select exactly one loaded Edilkamin device or entity.",
    "manual_mode": "Schedule editing is currently supported only in automatic mode.",
    "representations_differ": (
        "Chrono representations differ; check the plan in The Mind."
    ),
    "schedule_changed": (
        "The schedule changed while preparing this edit; no command sent."
    ),
    "invalid_cloud_schedule": (
        "The cloud did not return a complete valid weekly schedule."
    ),
    "readback_mismatch": "Cloud readback does not match the expected schedule.",
    "write_unconfirmed": (
        "Schedule write not confirmed; it was not retried. Check The Mind."
    ),
    "schedule_failed": (
        "Schedule editing failed; check cloud connectivity and backup storage."
    ),
}


def fail(key: str) -> NoReturn:
    """Reject an action's target or the stove's current operating state."""
    raise ServiceValidationError(
        MESSAGES[key], translation_domain=DOMAIN, translation_key=key
    )


def operation_error(key: str) -> HomeAssistantError:
    """Build an actionable, translated cloud or storage error."""
    return HomeAssistantError(
        MESSAGES[key], translation_domain=DOMAIN, translation_key=key
    )
