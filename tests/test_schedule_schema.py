"""HA selector and template compatibility, including the midnight boundary."""

from datetime import time

import pytest
import voluptuous as vol

from custom_components.edilkamin.schedule_schema import EDIT_SCHEMA


@pytest.mark.parametrize("end", ["24:00", "00:00", "00:00:00", time(0)])
@pytest.mark.parametrize("preview", [False, "false", "off", 0])
def test_midnight_and_templated_booleans(end, preview):
    result = EDIT_SCHEMA(
        {
            "days": ["tuesday"],
            "start": "23:30:00",
            "end": end,
            "mode": "comfort",
            "preview_only": preview,
        }
    )
    assert result["start"] == "23:30"
    assert result["end"] == "24:00"
    assert result["preview_only"] is False


@pytest.mark.parametrize("start", ["23:15:00", "23:30:01", "24:00", "not a time"])
def test_invalid_boundaries(start):
    with pytest.raises(vol.Invalid):
        EDIT_SCHEMA(
            {"days": ["tuesday"], "start": start, "end": "24:00", "mode": "comfort"}
        )
