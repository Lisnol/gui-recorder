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

    const = ModuleType("custom_components.gui_recorder.const")
    const.DOMAIN = "gui_recorder"
    db_stats = ModuleType("custom_components.gui_recorder.db_stats")
    storage = ModuleType("custom_components.gui_recorder.storage")

    async def placeholder(*_args, **_kwargs):
        raise AssertionError("test must replace this dependency")

    db_stats.async_analyze_db = placeholder
    db_stats.async_count_entity_rows_before = placeholder
    storage.async_reload_data = placeholder
    storage.async_save_data = placeholder

    sys.modules.update(
        {
            "custom_components": custom_components,
            "custom_components.gui_recorder": package,
            "custom_components.gui_recorder.const": const,
            "custom_components.gui_recorder.db_stats": db_stats,
            "custom_components.gui_recorder.storage": storage,
            "homeassistant": homeassistant,
            "homeassistant.core": core,
        }
    )
    path = Path("custom_components/gui_recorder/purge_jobs.py")
    spec = importlib.util.spec_from_file_location(
        "custom_components.gui_recorder.purge_jobs", path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


purge_jobs = _load_module()


class FakeServices:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data, blocking):
        self.calls.append((domain, service, data, blocking))


class FakeHass:
    def __init__(self):
        self.data = {
            "gui_recorder": {
                "data": {
                    "stats": {"db_size_bytes": 1000},
                    "auto_update_data": True,
                    "purge_history": [],
                    "active_purge": None,
                }
            }
        }
        self.services = FakeServices()

    def async_create_background_task(self, coroutine, _name):
        return asyncio.create_task(coroutine)


class PurgeJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = FakeHass()

        async def reload_data(hass):
            return hass.data["gui_recorder"]["data"]

        async def save_data(hass, data):
            hass.data["gui_recorder"]["data"] = dict(data)

        purge_jobs.async_reload_data = reload_data
        purge_jobs.async_save_data = save_data

    async def test_analysis_failure_is_a_completed_job_with_warning(self):
        counts = iter((1, 0))

        async def count_rows(*_args):
            return next(counts)

        async def analyze(_hass):
            return {"db_size_bytes": None, "error": "metadata denied"}

        async def recorder_idle(_hass):
            return None

        purge_jobs.async_count_entity_rows_before = count_rows
        purge_jobs.async_analyze_db = analyze
        purge_jobs._wait_recorder_idle = recorder_idle

        await purge_jobs.async_start_purge_job(
            self.hass,
            kind="entity",
            label="sensor.a",
            entity_ids=["sensor.a"],
        )
        await self.hass.data["gui_recorder"]["purge_task"]

        progress = purge_jobs.purge_progress(self.hass)
        self.assertEqual(progress["status"], "completed")
        self.assertFalse(progress["stats_refreshed"])
        self.assertIn("metadata denied", progress["warning"])

    async def test_background_job_survives_start_request(self):
        counts = iter((100, 50, 0))

        async def count_rows(*_args):
            return next(counts)

        async def analyze(hass):
            stats = {"db_size_bytes": 900}
            hass.data["gui_recorder"]["data"]["stats"] = stats
            return stats

        async def recorder_idle(_hass):
            return None

        purge_jobs.async_count_entity_rows_before = count_rows
        purge_jobs.async_analyze_db = analyze
        purge_jobs._wait_recorder_idle = recorder_idle

        started = await purge_jobs.async_start_purge_job(
            self.hass,
            kind="selected",
            label="2 selected entities",
            entity_ids=["sensor.a", "sensor.b"],
        )
        self.assertTrue(started["active"])
        task = self.hass.data["gui_recorder"]["purge_task"]
        await task

        progress = purge_jobs.purge_progress(self.hass)
        self.assertEqual(progress["status"], "completed")
        self.assertEqual(progress["deleted_rows"], 100)
        self.assertEqual(progress["reclaimed_bytes"], 100)
        self.assertTrue(progress["stats_refreshed"])
        self.assertTrue(progress["determinate"])
        self.assertEqual(len(self.hass.services.calls), 1)
        self.assertEqual(self.hass.services.calls[0][1], "purge_entities")
        history = self.hass.data["gui_recorder"]["data"]["purge_history"]
        self.assertEqual(history[0]["job_id"], started["job_id"])

    async def test_auto_update_setting_can_skip_analysis(self):
        self.hass.data["gui_recorder"]["data"]["auto_update_data"] = False
        counts = iter((5, 0))

        async def count_rows(*_args):
            return next(counts)

        async def recorder_idle(_hass):
            return None

        async def unexpected_analysis(_hass):
            raise AssertionError("analysis should be skipped")

        purge_jobs.async_count_entity_rows_before = count_rows
        purge_jobs.async_analyze_db = unexpected_analysis
        purge_jobs._wait_recorder_idle = recorder_idle

        await purge_jobs.async_start_purge_job(
            self.hass,
            kind="entity",
            label="sensor.a",
            entity_ids=["sensor.a"],
        )
        await self.hass.data["gui_recorder"]["purge_task"]

        progress = purge_jobs.purge_progress(self.hass)
        self.assertEqual(progress["status"], "completed")
        self.assertFalse(progress["stats_refreshed"])
        self.assertIsNone(progress["db_size_after"])

    async def test_global_purge_is_background_and_indeterminate(self):
        self.hass.data["gui_recorder"]["data"]["auto_update_data"] = False

        async def recorder_idle(_hass):
            return None

        purge_jobs._wait_recorder_idle = recorder_idle
        started = await purge_jobs.async_start_purge_job(
            self.hass,
            kind="database",
            label="recorder data older than 10 days",
            keep_days=10,
            repack=True,
        )
        await self.hass.data["gui_recorder"]["purge_task"]

        progress = purge_jobs.purge_progress(self.hass)
        self.assertTrue(started["active"])
        self.assertEqual(progress["status"], "completed")
        self.assertFalse(progress["determinate"])
        self.assertEqual(len(self.hass.services.calls), 1)
        self.assertEqual(
            self.hass.services.calls[0],
            (
                "recorder",
                "purge",
                {"keep_days": 10, "repack": True},
                True,
            ),
        )

    async def test_restart_marks_persisted_job_interrupted(self):
        self.hass.data["gui_recorder"]["data"]["active_purge"] = {
            "job_id": "old-job",
            "active": True,
            "status": "purging",
            "kind": "device",
            "label": "Kitchen",
            "started_at": "2026-10-21T10:00:00+00:00",
        }
        await purge_jobs.async_restore_interrupted_purge(self.hass)
        data = self.hass.data["gui_recorder"]["data"]
        self.assertIsNone(data["active_purge"])
        self.assertEqual(data["purge_history"][0]["status"], "interrupted")
        self.assertEqual(purge_jobs.purge_progress(self.hass)["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
