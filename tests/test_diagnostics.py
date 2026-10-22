from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
import unittest


def _load_module():
    custom_components = ModuleType("custom_components")
    package = ModuleType("custom_components.gui_recorder")
    package.__path__ = []
    homeassistant = ModuleType("homeassistant")
    core = ModuleType("homeassistant.core")
    core.HomeAssistant = object
    config_entries = ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    const = ModuleType("custom_components.gui_recorder.const")
    const.DOMAIN = "gui_recorder"
    const.INTEGRATION_VERSION = "test"
    sys.modules.update(
        {
            "custom_components": custom_components,
            "custom_components.gui_recorder": package,
            "custom_components.gui_recorder.const": const,
            "homeassistant": homeassistant,
            "homeassistant.core": core,
            "homeassistant.config_entries": config_entries,
        }
    )
    path = Path("custom_components/gui_recorder/diagnostics.py")
    spec = importlib.util.spec_from_file_location(
        "custom_components.gui_recorder.diagnostics_under_test", path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


diagnostics = _load_module()


class _Hass:
    def __init__(self):
        self.data = {
            "gui_recorder": {
                "loaded": True,
                "data": {
                    "db_url_secret": "mariadb_url",
                    "excluded_entities": ["sensor.private"],
                    "rescued_entities": [],
                    "manual_exclusions_yaml": "exclude:\n  domains: [sensor]",
                    "purge_history": [
                        {
                            "job_id": "secret-correlation-id",
                            "label": "sensor.private",
                            "kind": "entity",
                            "status": "completed",
                            "deleted_rows": 12,
                        }
                    ],
                    "stats": {
                        "database": "homeassistant @ 192.168.1.89",
                        "db_path": "/config/private.db",
                        "backend": "mariadb",
                        "entity_counts": {"sensor.private": 12},
                        "total_rows": 12,
                    },
                },
                "purge_progress": {
                    "job_id": "active-secret-id",
                    "label": "Kitchen",
                    "kind": "device",
                    "status": "purging",
                },
                "migration": {
                    "legacy_source_path": "/config/configuration.yaml",
                    "gui_ready": True,
                },
            }
        }


class DiagnosticsTests(unittest.TestCase):
    def test_diagnostics_are_useful_and_redacted(self):
        result = asyncio.run(
            diagnostics.async_get_config_entry_diagnostics(_Hass(), object())
        )
        rendered = repr(result)

        self.assertEqual(result["database"]["backend"], "mariadb")
        self.assertEqual(result["database"]["entity_count"], 1)
        self.assertEqual(result["maintenance_history"][0]["deleted_rows"], 12)
        self.assertNotIn("sensor.private", rendered)
        self.assertNotIn("192.168.1.89", rendered)
        self.assertNotIn("/config/private.db", rendered)
        self.assertNotIn("mariadb_url", rendered)
        self.assertNotIn("secret-correlation-id", rendered)


if __name__ == "__main__":
    unittest.main()
