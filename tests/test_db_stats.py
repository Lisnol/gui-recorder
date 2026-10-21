from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType
import unittest


def _load_module():
    custom_components = ModuleType("custom_components")
    package = ModuleType("custom_components.gui_recorder")
    package.__path__ = []
    core = ModuleType("homeassistant.core")
    core.HomeAssistant = object
    homeassistant = ModuleType("homeassistant")

    sqlalchemy = ModuleType("sqlalchemy")
    sqlalchemy.func = object()
    sqlalchemy.select = lambda *_args, **_kwargs: None
    sqlalchemy.text = lambda sql: sql

    const = ModuleType("custom_components.gui_recorder.const")
    const.DOMAIN = "gui_recorder"
    storage = ModuleType("custom_components.gui_recorder.storage")

    async def async_save_data(*_args, **_kwargs):
        return None

    storage.async_save_data = async_save_data
    sys.modules.update(
        {
            "custom_components": custom_components,
            "custom_components.gui_recorder": package,
            "custom_components.gui_recorder.const": const,
            "custom_components.gui_recorder.storage": storage,
            "homeassistant": homeassistant,
            "homeassistant.core": core,
            "sqlalchemy": sqlalchemy,
        }
    )
    path = Path("custom_components/gui_recorder/db_stats.py")
    spec = importlib.util.spec_from_file_location(
        "custom_components.gui_recorder.db_stats_under_test", path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


db_stats = _load_module()


class _Result:
    def __init__(self, row):
        self._row = row

    def one(self):
        return self._row


class _Session:
    def __init__(self, row=None, error=None):
        self.row = row
        self.error = error
        self.query = None

    def execute(self, query):
        self.query = query
        if self.error:
            raise self.error
        return _Result(self.row)


class MariaDbSizeTests(unittest.TestCase):
    def test_returns_data_index_and_free_allocation(self):
        session = _Session((1_000, 250, 125))

        result = db_stats._mariadb_size_breakdown(session)

        self.assertEqual(result["db_size_bytes"], 1_250)
        self.assertEqual(result["db_data_bytes"], 1_000)
        self.assertEqual(result["db_index_bytes"], 250)
        self.assertEqual(result["db_free_bytes"], 125)
        self.assertIn("information_schema.TABLES", session.query)

    def test_metadata_permission_failure_keeps_analysis_optional(self):
        result = db_stats._mariadb_size_breakdown(
            _Session(error=PermissionError("denied"))
        )

        self.assertEqual(
            result,
            {
                "db_size_bytes": None,
                "db_data_bytes": None,
                "db_index_bytes": None,
                "db_free_bytes": None,
            },
        )


if __name__ == "__main__":
    unittest.main()
