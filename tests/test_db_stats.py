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

    async def async_update_data(hass, mutator):
        data = hass.data["gui_recorder"]["data"]
        result = mutator(data)
        return data, result

    storage.async_update_data = async_update_data
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


class MariaDbHealthTests(unittest.TestCase):
    def test_healthy_supported_mariadb(self):
        result = db_stats._mariadb_health(
            _Session(("10.11.8-MariaDB", "utf8mb4", "utf8mb4_bin", "InnoDB", 0))
        )

        self.assertEqual(result["db_product"], "MariaDB")
        self.assertEqual(result["db_health_status"], "ok")
        self.assertEqual(result["db_health_issues"], [])

    def test_reports_recorder_relevant_warnings(self):
        result = db_stats._mariadb_health(
            _Session(("10.6.8-MariaDB", "latin1", "latin1_swedish_ci", "MyISAM", 2))
        )

        self.assertEqual(result["db_health_status"], "warning")
        self.assertEqual(len(result["db_health_issues"]), 4)
        self.assertTrue(any("performance regression" in issue for issue in result["db_health_issues"]))
        self.assertTrue(any("utf8mb4" in issue for issue in result["db_health_issues"]))

    def test_health_permission_failure_is_non_fatal(self):
        result = db_stats._mariadb_health(
            _Session(error=PermissionError("metadata denied"))
        )

        self.assertEqual(result["db_health_status"], "unavailable")
        self.assertIn("metadata denied", result["db_health_error"])


class AnalysisFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_failure_preserves_last_successful_counts(self):
        previous = {
            "entity_counts": {"sensor.a": 123},
            "generated_at": "2026-10-21T10:00:00+00:00",
            "backend": "mariadb",
            "database": "homeassistant @ db",
            "db_size_bytes": 999,
            "total_rows": 123,
            "error": None,
        }
        hass = type(
            "Hass",
            (),
            {"data": {"gui_recorder": {"data": {"stats": previous}}}},
        )()

        class Instance:
            async def async_add_executor_job(self, *_args):
                await asyncio.sleep(0)
                raise RuntimeError("temporary database error")

        original_backend = db_stats.get_recorder_backend
        original_instance = db_stats._get_recorder_instance
        db_stats.get_recorder_backend = lambda _hass: "mariadb"
        db_stats._get_recorder_instance = lambda _hass: Instance()
        try:
            result = await db_stats.async_analyze_db(hass)
        finally:
            db_stats.get_recorder_backend = original_backend
            db_stats._get_recorder_instance = original_instance

        self.assertEqual(result["entity_counts"], {"sensor.a": 123})
        self.assertEqual(result["total_rows"], 123)
        self.assertEqual(result["db_size_bytes"], 999)
        self.assertEqual(result["generated_at"], "2026-10-21T10:00:00+00:00")
        self.assertIn("temporary database error", result["error"])
        self.assertIsNotNone(result["analysis_attempted_at"])
        self.assertFalse(hass.data["gui_recorder"]["analysis_in_progress"])

    async def test_concurrent_analysis_callers_share_one_scan(self):
        hass = type(
            "Hass",
            (),
            {"data": {"gui_recorder": {"data": {"stats": {}}}}},
        )()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def analyze_once(_hass):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            result = {"total_rows": 42, "error": None}
            _hass.data["gui_recorder"]["data"]["stats"] = result
            return result

        original = db_stats._async_analyze_db_locked
        db_stats._async_analyze_db_locked = analyze_once
        try:
            first = asyncio.create_task(db_stats.async_analyze_db(hass))
            await started.wait()
            second = asyncio.create_task(db_stats.async_analyze_db(hass))
            await asyncio.sleep(0)
            release.set()
            first_result, second_result = await asyncio.gather(first, second)
        finally:
            db_stats._async_analyze_db_locked = original

        self.assertEqual(calls, 1)
        self.assertEqual(first_result["total_rows"], 42)
        self.assertEqual(second_result["total_rows"], 42)


if __name__ == "__main__":
    unittest.main()
