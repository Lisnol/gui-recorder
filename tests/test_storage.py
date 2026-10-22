from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
import unittest


class _FakeStore:
    instances = []

    def __init__(self, _hass, version, key, *, atomic_writes=False):
        self.version = version
        self.key = key
        self.atomic_writes = atomic_writes
        self.value = None
        self.saves = 0
        self.__class__.instances.append(self)

    async def async_load(self):
        await asyncio.sleep(0)
        return self.value

    async def async_save(self, value):
        await asyncio.sleep(0)
        self.value = value
        self.saves += 1


def _load_module():
    custom_components = ModuleType("custom_components")
    package = ModuleType("custom_components.gui_recorder")
    package.__path__ = []
    homeassistant = ModuleType("homeassistant")
    core = ModuleType("homeassistant.core")
    core.HomeAssistant = object
    helpers = ModuleType("homeassistant.helpers")
    storage_helper = ModuleType("homeassistant.helpers.storage")
    storage_helper.Store = _FakeStore

    const = ModuleType("custom_components.gui_recorder.const")
    const.DOMAIN = "gui_recorder"
    const.STORAGE_VERSION = 1
    const.STORAGE_KEY = "gui_recorder.data"
    const.DEFAULT_STORAGE = {
        "excluded_entities": [],
        "rescued_entities": [],
        "stats": {"entity_counts": {}, "generated_at": None},
    }
    sys.modules.update(
        {
            "custom_components": custom_components,
            "custom_components.gui_recorder": package,
            "custom_components.gui_recorder.const": const,
            "homeassistant": homeassistant,
            "homeassistant.core": core,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.storage": storage_helper,
        }
    )
    path = Path("custom_components/gui_recorder/storage.py")
    spec = importlib.util.spec_from_file_location(
        "custom_components.gui_recorder.storage_under_test", path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


storage = _load_module()


class _Hass:
    def __init__(self):
        self.data = {}


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _FakeStore.instances.clear()
        self.hass = _Hass()

    async def test_reuses_atomic_store_and_preserves_missing_signal(self):
        loaded = await storage.async_load_data(self.hass)

        self.assertEqual(loaded["excluded_entities"], [])
        self.assertTrue(self.hass.data["gui_recorder"]["storage_was_missing"])
        self.assertEqual(len(_FakeStore.instances), 1)
        self.assertTrue(_FakeStore.instances[0].atomic_writes)
        self.assertEqual(_FakeStore.instances[0].saves, 0)
        self.assertIs(storage.get_store(self.hass), _FakeStore.instances[0])

    async def test_concurrent_updates_do_not_lose_unrelated_changes(self):
        await storage.async_load_data(self.hass)

        async def update_one():
            await storage.async_patch_data(self.hass, first=1)

        async def update_two():
            await storage.async_patch_data(self.hass, second=2)

        await asyncio.gather(update_one(), update_two())

        data = self.hass.data["gui_recorder"]["data"]
        self.assertEqual(data["first"], 1)
        self.assertEqual(data["second"], 2)
        self.assertEqual(_FakeStore.instances[0].saves, 2)

    async def test_default_nested_data_is_not_shared(self):
        await storage.async_load_data(self.hass)
        self.hass.data["gui_recorder"]["data"]["stats"]["entity_counts"][
            "sensor.changed"
        ] = 1

        another = _Hass()
        loaded = await storage.async_load_data(another)

        self.assertEqual(loaded["stats"]["entity_counts"], {})

    async def test_old_partial_stats_are_merged_with_new_defaults(self):
        store = storage.get_store(self.hass)
        store.value = {"stats": {"entity_counts": {"sensor.old": 4}}}

        loaded = await storage.async_load_data(self.hass)

        self.assertEqual(loaded["stats"]["entity_counts"], {"sensor.old": 4})
        self.assertIsNone(loaded["stats"]["generated_at"])


if __name__ == "__main__":
    unittest.main()
