"""Validate schedule action input once, before selecting a writer."""

from datetime import date, time

from homeassistant.helpers import config_validation as cv
import voluptuous as vol

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MODES = {"off": 0, "economy": 1, "comfort": 2}


def half_hour(value: str, *, end: bool = False) -> int:
    """Convert an already validated wall-clock label to a weekly slot boundary."""
    if end and value == "24:00":
        return 48
    hour, minute = map(int, value.split(":"))
    return hour * 2 + minute // 30


def boundary(value: str | time, *, end: bool = False) -> str:
    """Use HA's time parser; preserve legacy 24:00 and use midnight as day end."""
    if end and value == "24:00":
        return "24:00"
    parsed = cv.time(value)
    if parsed.minute not in (0, 30) or parsed.second or parsed.microsecond:
        message = "Choose a whole or half hour, with zero seconds."
        raise vol.Invalid(message)
    if end and parsed.hour == parsed.minute == 0:
        return "24:00"
    return parsed.strftime("%H:%M")


def validate_range(data: dict) -> dict:
    """Normalize HA selectors and reject zero-length or overnight ranges."""
    data = dict(data)
    data["start"] = boundary(data["start"])
    data["end"] = boundary(data["end"], end=True)
    data["days"] = list(dict.fromkeys(data["days"]))
    if half_hour(data["end"], end=True) <= half_hour(data["start"]):
        message = "End must be after start. Split overnight ranges at midnight."
        raise vol.Invalid(message)
    if data["auto_restore"]:
        if len(data["days"]) != 1:
            message = "Temporary windows require exactly one weekday."
            raise vol.Invalid(message)
        if "date" in data:
            try:
                chosen = date.fromisoformat(data["date"])
            except ValueError as error:
                message = "Date must be YYYY-MM-DD."
                raise vol.Invalid(message) from error
            if (
                chosen.isoformat() != data["date"]
                or DAYS[chosen.weekday()] != data["days"][0]
            ):
                message = "Date and weekday must match."
                raise vol.Invalid(message)
    elif "date" in data:
        message = "Date is allowed only with auto_restore."
        raise vol.Invalid(message)
    return data


EDIT_FIELDS = {
    vol.Required("days"): vol.All([vol.In(DAYS)], vol.Length(min=1, max=7)),
    vol.Required("start"): vol.Any(time, cv.string),
    vol.Required("end"): vol.Any(time, cv.string),
    vol.Required("mode"): vol.All(cv.string, vol.In(MODES)),
    vol.Optional("preview_only", default=False): cv.boolean,
    vol.Optional("auto_restore", default=False): cv.boolean,
    vol.Optional("date"): cv.string,
}
EDIT_SCHEMA = vol.All(vol.Schema(EDIT_FIELDS), validate_range)
