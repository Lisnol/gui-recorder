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
    const = ModuleType("custom_components.gui_recorder.const")
    const.DOMAIN = "gui_recorder"
    file_utils = ModuleType("custom_components.gui_recorder.file_utils")
    file_utils.atomic_write_utf8 = lambda *_args, **_kwargs: None
    storage = ModuleType("custom_components.gui_recorder.storage")

    async def placeholder_update(*_args, **_kwargs):
        raise AssertionError("test must replace async_update_data")

    storage.async_update_data = placeholder_update
    yaml = ModuleType("yaml")
    yaml.YAMLError = ValueError
    yaml.safe_load = lambda _text: {}
    yaml.safe_dump = lambda *_args, **_kwargs: ""
    sys.modules.update(
        {
            "custom_components": custom_components,
            "custom_components.gui_recorder": package,
            "custom_components.gui_recorder.const": const,
            "custom_components.gui_recorder.file_utils": file_utils,
            "custom_components.gui_recorder.storage": storage,
            "homeassistant": homeassistant,
            "homeassistant.core": core,
            "yaml": yaml,
        }
    )
    path = Path("custom_components/gui_recorder/yaml_writer.py")
    spec = importlib.util.spec_from_file_location(
        "custom_components.gui_recorder.yaml_writer_under_test", path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


yaml_writer = _load_module()


class _Hass:
    def __init__(self):
        self.data = {"gui_recorder": {"data": {"generation": 1}}}

        class Config:
            @staticmethod
            def path(name):
                return f"/config/{name}"

        self.config = Config()

    async def async_add_executor_job(self, function, *args):
        return function(*args)


class YamlWriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_rewrites_if_storage_changes_during_atomic_file_write(self):
        hass = _Hass()
        written_generations = []

        async def fake_write(current_hass):
            source = current_hass.data["gui_recorder"]["data"]
            written_generations.append(source["generation"])
            if source["generation"] == 1:
                current_hass.data["gui_recorder"]["data"] = {"generation": 2}
            return "/config/gui_recorder.yaml", source

        original = yaml_writer._async_write_yaml_locked
        yaml_writer._async_write_yaml_locked = fake_write
        try:
            result = await yaml_writer.async_write_yaml(hass)
        finally:
            yaml_writer._async_write_yaml_locked = original

        self.assertEqual(result, "/config/gui_recorder.yaml")
        self.assertEqual(written_generations, [1, 2])

    async def test_refuses_tampered_generated_path(self):
        hass = _Hass()
        hass.data["gui_recorder"]["data"]["generated_path"] = "../configuration.yaml"

        with self.assertRaisesRegex(ValueError, "outside gui_recorder.yaml"):
            await yaml_writer._async_write_yaml_locked(hass)

    async def test_refuses_invalid_stored_manual_filters(self):
        hass = _Hass()
        hass.data["gui_recorder"]["data"]["manual_exclusions_yaml"] = "bad"
        original = yaml_writer.parse_manual_exclusions
        yaml_writer.parse_manual_exclusions = lambda _text: (_ for _ in ()).throw(
            ValueError("invalid")
        )
        try:
            with self.assertRaisesRegex(ValueError, "refusing to replace"):
                await yaml_writer._async_write_yaml_locked(hass)
        finally:
            yaml_writer.parse_manual_exclusions = original

    async def test_refuses_literal_non_sqlite_database_url(self):
        hass = _Hass()
        hass.data["gui_recorder"]["data"]["db_url"] = (
            "mysql://user:password@example/homeassistant"
        )

        with self.assertRaisesRegex(ValueError, "must be stored in secrets.yaml"):
            await yaml_writer._async_write_yaml_locked(hass)

    async def test_failed_yaml_write_rolls_back_only_configuration_keys(self):
        hass = _Hass()
        hass.data["gui_recorder"]["data"] = {
            "purge_keep_days": 10,
            "stats": {"total_rows": 1},
        }

        async def update_data(current_hass, mutator):
            current = dict(current_hass.data["gui_recorder"]["data"])
            result = mutator(current)
            current_hass.data["gui_recorder"]["data"] = current
            return current, result

        async def fail_write(current_hass):
            # Simulate an unrelated analysis completing while the executor was
            # attempting the YAML write.
            current_hass.data["gui_recorder"]["data"]["stats"] = {
                "total_rows": 2
            }
            raise OSError("disk full")

        original_update = yaml_writer.async_update_data
        original_write = yaml_writer.async_write_yaml
        yaml_writer.async_update_data = update_data
        yaml_writer.async_write_yaml = fail_write
        try:
            with self.assertRaisesRegex(OSError, "disk full"):
                await yaml_writer.async_update_and_write_yaml(
                    hass,
                    lambda data: data.update(
                        purge_keep_days=30,
                        pending_restart=True,
                    ),
                )
        finally:
            yaml_writer.async_update_data = original_update
            yaml_writer.async_write_yaml = original_write

        data = hass.data["gui_recorder"]["data"]
        self.assertEqual(data["purge_keep_days"], 10)
        self.assertNotIn("pending_restart", data)
        self.assertEqual(data["stats"], {"total_rows": 2})

    async def test_rollback_baseline_is_captured_inside_storage_update(self):
        hass = _Hass()
        hass.data["gui_recorder"]["data"] = {
            "purge_keep_days": 10,
            "stats": {"total_rows": 1},
        }
        update_calls = 0

        async def update_data(current_hass, mutator):
            nonlocal update_calls
            update_calls += 1
            if update_calls == 1:
                # Simulate an analysis that finished after the configuration
                # operation was requested but before it acquired storage's lock.
                current_hass.data["gui_recorder"]["data"]["stats"] = {
                    "total_rows": 2
                }
            current = dict(current_hass.data["gui_recorder"]["data"])
            result = mutator(current)
            current_hass.data["gui_recorder"]["data"] = current
            return current, result

        async def fail_write(_current_hass):
            raise OSError("disk full")

        original_update = yaml_writer.async_update_data
        original_write = yaml_writer.async_write_yaml
        yaml_writer.async_update_data = update_data
        yaml_writer.async_write_yaml = fail_write
        try:
            with self.assertRaisesRegex(OSError, "disk full"):
                await yaml_writer.async_update_and_write_yaml(
                    hass,
                    lambda data: data.update(purge_keep_days=30),
                )
        finally:
            yaml_writer.async_update_data = original_update
            yaml_writer.async_write_yaml = original_write

        data = hass.data["gui_recorder"]["data"]
        self.assertEqual(data["purge_keep_days"], 10)
        self.assertEqual(data["stats"], {"total_rows": 2})


if __name__ == "__main__":
    unittest.main()
