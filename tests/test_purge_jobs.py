from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest import mock


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
    storage.async_update_data = placeholder

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
_real_queue_tracked_recorder_purge = purge_jobs._queue_tracked_recorder_purge


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

        async def update_data(hass, mutator):
            data = dict(hass.data["gui_recorder"]["data"])
            result = mutator(data)
            hass.data["gui_recorder"]["data"] = data
            return data, result

        def queue_completed(*_args, **_kwargs):
            future = asyncio.get_running_loop().create_future()
            future.set_result(None)
            return future

        purge_jobs.async_update_data = update_data
        purge_jobs._queue_tracked_recorder_purge = queue_completed

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
        self.assertEqual(self.hass.services.calls, [])
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
        self.assertEqual(self.hass.services.calls, [])

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
        self.assertEqual(self.hass.services.calls[-1][0], "persistent_notification")

    async def test_checkpoint_persists_latest_active_progress(self):
        progress = purge_jobs.set_purge_progress(
            self.hass,
            job_id="checkpoint-job",
            active=True,
            status="purging",
            total_rows=100,
            remaining_rows=40,
            started_at="2026-10-21T10:00:00+00:00",
        )
        self.hass.data["gui_recorder"]["data"]["active_purge"] = {
            "job_id": "checkpoint-job",
            "active": True,
        }

        await purge_jobs._persist_job_checkpoint(self.hass, progress, force=True)

        checkpoint = self.hass.data["gui_recorder"]["data"]["active_purge"]
        self.assertEqual(checkpoint["remaining_rows"], 40)
        self.assertEqual(checkpoint["deleted_rows"], 60)
        self.assertIsNotNone(checkpoint["checkpointed_at"])

    async def test_long_success_notifies_but_short_success_stays_quiet(self):
        long_job = {
            "status": "completed",
            "label": "sensor.a",
            "determinate": True,
            "deleted_rows": 10,
            "started_at": "2026-10-21T10:00:00+00:00",
            "finished_at": "2026-10-21T10:01:01+00:00",
        }
        await purge_jobs._notify_job_result(self.hass, long_job)
        self.assertEqual(self.hass.services.calls[-1][0], "persistent_notification")

        self.hass.services.calls.clear()
        short_job = {
            **long_job,
            "finished_at": "2026-10-21T10:00:05+00:00",
        }
        await purge_jobs._notify_job_result(self.hass, short_job)
        self.assertEqual(self.hass.services.calls, [])

    async def test_tracked_recorder_task_requeues_until_private_purge_finishes(self):
        calls = []
        outcomes = iter((False, False, True))

        class RecorderTask:
            pass

        class Instance:
            def __init__(self):
                self.queue = []

            def queue_task(self, task):
                self.queue.append(task)

        instance = Instance()
        recorder = ModuleType("homeassistant.components.recorder")
        recorder.get_instance = lambda _hass: instance
        recorder_purge = ModuleType("homeassistant.components.recorder.purge")

        def purge_entity_data(_instance, entity_filter, cutoff):
            calls.append((entity_filter("sensor.a"), cutoff))
            return next(outcomes)

        recorder_purge.purge_entity_data = purge_entity_data
        tasks = ModuleType("homeassistant.components.recorder.tasks")
        tasks.RecorderTask = RecorderTask
        recorder.purge = recorder_purge
        modules = {
            "homeassistant.components": ModuleType("homeassistant.components"),
            "homeassistant.components.recorder": recorder,
            "homeassistant.components.recorder.purge": recorder_purge,
            "homeassistant.components.recorder.tasks": tasks,
        }

        with mock.patch.dict(sys.modules, modules):
            cutoff = purge_jobs._utcnow()
            future = _real_queue_tracked_recorder_purge(
                self.hass,
                cutoff=cutoff,
                entity_ids=["sensor.a"],
                repack=False,
            )
            while instance.queue:
                instance.queue.pop(0).run(instance)
            await asyncio.sleep(0)
            await future

        self.assertEqual(len(calls), 3)
        self.assertTrue(all(matched for matched, _cutoff in calls))

    async def test_tracked_task_reports_requeue_failure(self):
        class RecorderTask:
            pass

        class Instance:
            def __init__(self):
                self.queue = []
                self.queue_calls = 0

            def queue_task(self, task):
                self.queue_calls += 1
                if self.queue_calls > 1:
                    raise RuntimeError("recorder stopped")
                self.queue.append(task)

        instance = Instance()
        recorder = ModuleType("homeassistant.components.recorder")
        recorder.get_instance = lambda _hass: instance
        recorder_purge = ModuleType("homeassistant.components.recorder.purge")
        recorder_purge.purge_entity_data = lambda *_args: False
        tasks = ModuleType("homeassistant.components.recorder.tasks")
        tasks.RecorderTask = RecorderTask
        recorder.purge = recorder_purge
        modules = {
            "homeassistant.components": ModuleType("homeassistant.components"),
            "homeassistant.components.recorder": recorder,
            "homeassistant.components.recorder.purge": recorder_purge,
            "homeassistant.components.recorder.tasks": tasks,
        }

        with mock.patch.dict(sys.modules, modules):
            future = _real_queue_tracked_recorder_purge(
                self.hass,
                cutoff=purge_jobs._utcnow(),
                entity_ids=["sensor.a"],
                repack=False,
            )
            instance.queue.pop().run(instance)
            await asyncio.sleep(0)
            with self.assertRaisesRegex(RuntimeError, "recorder stopped"):
                await future

    async def test_global_tracked_task_finishes_after_recorder_cleanup(self):
        calls = []

        class RecorderTask:
            pass

        class Session:
            def __enter__(self):
                calls.append("session-enter")
                return self

            def __exit__(self, *_args):
                calls.append("session-exit")

        class RunsManager:
            @staticmethod
            def load_from_db(_session):
                calls.append("runs-refreshed")

        class Instance:
            recorder_runs_manager = RunsManager()

            def __init__(self):
                self.queue = []

            def queue_task(self, task):
                self.queue.append(task)

            @staticmethod
            def get_session():
                return Session()

        instance = Instance()
        recorder = ModuleType("homeassistant.components.recorder")
        recorder.get_instance = lambda _hass: instance
        recorder_purge = ModuleType("homeassistant.components.recorder.purge")
        recorder_purge.purge_old_data = lambda *_args: calls.append("purged") or True
        tasks = ModuleType("homeassistant.components.recorder.tasks")
        tasks.RecorderTask = RecorderTask
        util = ModuleType("homeassistant.components.recorder.util")
        util.periodic_db_cleanups = lambda _instance: calls.append("cleanup")
        recorder.purge = recorder_purge
        modules = {
            "homeassistant.components": ModuleType("homeassistant.components"),
            "homeassistant.components.recorder": recorder,
            "homeassistant.components.recorder.purge": recorder_purge,
            "homeassistant.components.recorder.tasks": tasks,
            "homeassistant.components.recorder.util": util,
        }

        with mock.patch.dict(sys.modules, modules):
            future = _real_queue_tracked_recorder_purge(
                self.hass,
                cutoff=purge_jobs._utcnow(),
                entity_ids=None,
                repack=True,
            )
            instance.queue.pop().run(instance)
            await asyncio.sleep(0)
            await future

        self.assertEqual(
            calls,
            [
                "purged",
                "session-enter",
                "runs-refreshed",
                "session-exit",
                "cleanup",
            ],
        )

    async def test_background_task_start_failure_does_not_leave_active_lock(self):
        def fail_to_start(_coroutine, _name):
            raise RuntimeError("scheduler unavailable")

        self.hass.async_create_background_task = fail_to_start

        with self.assertRaisesRegex(RuntimeError, "scheduler unavailable"):
            await purge_jobs.async_start_purge_job(
                self.hass,
                kind="entity",
                label="sensor.a",
                entity_ids=["sensor.a"],
            )

        progress = purge_jobs.purge_progress(self.hass)
        self.assertFalse(progress["active"])
        self.assertEqual(progress["status"], "error")
        self.assertEqual(
            self.hass.data["gui_recorder"]["data"]["purge_history"][0]["status"],
            "error",
        )


if __name__ == "__main__":
    unittest.main()
