"""Validate service forms with HA's schema and all user-facing translations."""

import json
from pathlib import Path

from homeassistant.helpers.service import _SERVICES_SCHEMA
import yaml

from custom_components.edilkamin.schedule_errors import MESSAGES

ROOT = Path(__file__).parents[1] / "custom_components/edilkamin"


def test_service_form_and_translation_coverage():
    service = yaml.safe_load((ROOT / "services.yaml").read_text())
    _SERVICES_SCHEMA(service)
    fields = service["set_schedule"]["fields"]
    assert fields["start"]["selector"] == {"time": None}
    assert fields["end"]["selector"] == {"time": None}
    for filename in ("strings.json", "translations/en.json", "translations/de.json"):
        text = json.loads((ROOT / filename).read_text())
        assert set(fields) == set(text["services"]["set_schedule"]["fields"])
        assert set(MESSAGES) <= set(text["exceptions"])
        for field in fields.values():
            if (
                key := field.get("selector", {})
                .get("select", {})
                .get("translation_key")
            ):
                assert set(field["selector"]["select"]["options"]) == set(
                    text["selector"][key]["options"]
                )
