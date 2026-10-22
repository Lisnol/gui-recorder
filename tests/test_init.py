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
    components = ModuleType("homeassistant.components")
    frontend = ModuleType("homeassistant.components.frontend")
    frontend.removed = []
    frontend.async_remove_panel = lambda hass, path: frontend.removed.append((hass, path))
    components.frontend = frontend
    config_entries = ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    core = ModuleType("homeassistant.core")

    class CoreState:
        running = "running"
        starting = "starting"

    core.CoreState = CoreState
    core.HomeAssistant = object

    const = ModuleType("custom_components.gui_recorder.const")
    const.DOMAIN = "gui_recorder"
    const.PANEL_URL_PATH = "gui-recorder"
    db_stats = ModuleType("custom_components.gui_recorder.db_stats")
    db_stats.is_supported_recorder = lambda _hass: True
    migration = ModuleType("custom_components.gui_recorder.migration")

    async def detect(hass):
        return dict(hass.migration)

    async def recover(hass):
        hass.recovered = True
        hass.data["gui_recorder"]["data"]["purge_keep_days"] = 21
        return {"ok": True}

    migration.async_detect_sync_status = detect
    migration.async_recover_managed_yaml = recover
    panel = ModuleType("custom_components.gui_recorder.panel")

    async def setup_panel(hass):
        hass.data["gui_recorder"]["panel_registered"] = True

    panel.async_setup_panel = setup_panel
    purge_jobs = ModuleType("custom_components.gui_recorder.purge_jobs")
    purge_jobs.purge_is_active = lambda hass: hass.active

    async def restore(_hass):
        return None

    purge_jobs.async_restore_interrupted_purge = restore
    storage = ModuleType("custom_components.gui_recorder.storage")

    async def load(hass):
        domain = hass.data.setdefault("gui_recorder", {})
        domain.setdefault("data", dict(hass.initial_data))
        domain["storage_was_missing"] = hass.storage_missing
        return dict(domain["data"])

    async def patch(hass, **updates):
        hass.data["gui_recorder"]["data"].update(updates)
        hass.patches.append(updates)
        return hass.data["gui_recorder"]["data"]

    async def save(hass, data):
        hass.saved_defaults += 1
        hass.data["gui_recorder"]["data"] = dict(data)
        return data

    storage.async_load_data = load
    storage.async_patch_data = patch
    storage.async_save_data = save
    websocket_api = ModuleType("custom_components.gui_recorder.websocket_api")

    async def setup_ws(_hass):
        return None

    websocket_api.async_setup_websocket_api = setup_ws

    sys.modules.update(
        {
            "custom_components": custom_components,
            "custom_components.gui_recorder": package,
            "custom_components.gui_recorder.const": const,
            "custom_components.gui_recorder.db_stats": db_stats,
            "custom_components.gui_recorder.migration": migration,
            "custom_components.gui_recorder.panel": panel,
            "custom_components.gui_recorder.purge_jobs": purge_jobs,
            "custom_components.gui_recorder.storage": storage,
            "custom_components.gui_recorder.websocket_api": websocket_api,
            "homeassistant": homeassistant,
            "homeassistant.components": components,
            "homeassistant.components.frontend": frontend,
            "homeassistant.config_entries": config_entries,
            "homeassistant.core": core,
        }
    )
    path = Path("custom_components/gui_recorder/__init__.py")
    spec = importlib.util.spec_from_file_location(
        "custom_components.gui_recorder", path, submodule_search_locations=[]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, CoreState, frontend


integration, CoreState, frontend = _load_module()


class _Hass:
    def __init__(self, *, state, storage_missing=False, gui_active=False):
        self.data = {}
        self.state = state
        self.storage_missing = storage_missing
        self.migration = {"gui_include_active": gui_active}
        self.initial_data = {
            "pending_restart": True,
            "repack_in_progress": True,
        }
        self.patches = []
        self.saved_defaults = 0
        self.recovered = False
        self.active = False


class InitSafetyTests(unittest.TestCase):
    def test_live_reload_keeps_pending_restart_flag(self):
        hass = _Hass(state=CoreState.running)

        asyncio.run(integration._async_initialize(hass))

        self.assertTrue(hass.data["gui_recorder"]["data"]["pending_restart"])
        self.assertEqual(hass.patches, [])

    def test_full_start_clears_restart_only_flags(self):
        hass = _Hass(state=CoreState.starting)

        asyncio.run(integration._async_initialize(hass))

        self.assertFalse(hass.data["gui_recorder"]["data"]["pending_restart"])
        self.assertFalse(hass.data["gui_recorder"]["data"]["repack_in_progress"])

    def test_missing_storage_recovers_active_yaml_instead_of_saving_defaults(self):
        hass = _Hass(
            state=CoreState.starting,
            storage_missing=True,
            gui_active=True,
        )

        asyncio.run(integration._async_initialize(hass))

        self.assertTrue(hass.recovered)
        self.assertEqual(hass.saved_defaults, 0)
        self.assertEqual(hass.data["gui_recorder"]["data"]["purge_keep_days"], 21)

    def test_unload_is_refused_during_active_maintenance(self):
        hass = _Hass(state=CoreState.running)
        hass.active = True
        hass.data = {"gui_recorder": {"panel_registered": True}}

        result = asyncio.run(integration.async_unload_entry(hass, object()))

        self.assertFalse(result)
        self.assertTrue(hass.data["gui_recorder"]["panel_registered"])

    def test_unload_removes_panel_when_idle(self):
        frontend.removed.clear()
        hass = _Hass(state=CoreState.running)
        hass.data = {"gui_recorder": {"panel_registered": True}}

        result = asyncio.run(integration.async_unload_entry(hass, object()))

        self.assertTrue(result)
        self.assertFalse(hass.data["gui_recorder"]["panel_registered"])
        self.assertEqual(frontend.removed, [(hass, "gui-recorder")])

    def test_setup_failure_clears_config_entry_reference(self):
        hass = _Hass(state=CoreState.running)
        entry = object()

        async def fail_panel(_hass):
            raise RuntimeError("panel failed")

        original = integration.async_setup_panel
        integration.async_setup_panel = fail_panel
        try:
            with self.assertRaisesRegex(RuntimeError, "panel failed"):
                asyncio.run(integration.async_setup_entry(hass, entry))
        finally:
            integration.async_setup_panel = original

        self.assertIsNone(hass.data["gui_recorder"]["config_entry"])
        self.assertFalse(hass.data["gui_recorder"]["loaded"])


if __name__ == "__main__":
    unittest.main()
