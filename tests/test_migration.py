from __future__ import annotations

from datetime import datetime as RealDateTime
import importlib.util
from pathlib import Path
import stat
import sys
import tempfile
from types import ModuleType
import unittest
from unittest import mock


class _YamlError(Exception):
    def __init__(self, message, line=1, column=2):
        super().__init__(message)
        self.problem_mark = type("Mark", (), {"line": line, "column": column})()


def _fake_yaml_module():
    yaml = ModuleType("yaml")

    class SafeLoader:
        @classmethod
        def add_constructor(cls, _tag, _constructor):
            return None

    yaml.SafeLoader = SafeLoader
    yaml.Node = object
    yaml.YAMLError = _YamlError
    yaml.compose = lambda _content: object()
    yaml.safe_dump = lambda *_args, **_kwargs: "exclude:\n  domains:\n  - update\n"

    def load(content, Loader=None):
        del Loader
        if "never-show-this-secret" in content:
            raise _YamlError("parser included never-show-this-secret")
        if "managed-recovery" in content:
            return {
                "auto_purge": False,
                "auto_repack": False,
                "purge_keep_days": 21,
                "commit_interval": 7,
                "exclude": {
                    "entities": ["sensor.noisy"],
                    "domains": ["update"],
                },
                "include": {"entities": ["update.important"]},
            }
        return {}

    yaml.load = load
    return yaml


def _load_module():
    custom_components = ModuleType("custom_components")
    package = ModuleType("custom_components.gui_recorder")
    package.__path__ = []
    core = ModuleType("homeassistant.core")
    core.HomeAssistant = object
    homeassistant = ModuleType("homeassistant")
    const = ModuleType("custom_components.gui_recorder.const")
    const.DOMAIN = "gui_recorder"
    storage = ModuleType("custom_components.gui_recorder.storage")

    async def async_update_data(hass, mutator):
        data = hass.data["gui_recorder"]["data"]
        result = mutator(data)
        return data, result

    storage.async_update_data = async_update_data

    engine = ModuleType("sqlalchemy.engine")
    engine.make_url = lambda value: value
    sqlalchemy = ModuleType("sqlalchemy")
    sqlalchemy.engine = engine

    yaml = _fake_yaml_module()
    sys.modules.update(
        {
            "custom_components": custom_components,
            "custom_components.gui_recorder": package,
            "custom_components.gui_recorder.const": const,
            "custom_components.gui_recorder.storage": storage,
            "homeassistant": homeassistant,
            "homeassistant.core": core,
            "sqlalchemy": sqlalchemy,
            "sqlalchemy.engine": engine,
            "yaml": yaml,
        }
    )

    file_utils_path = Path("custom_components/gui_recorder/file_utils.py")
    file_utils_spec = importlib.util.spec_from_file_location(
        "custom_components.gui_recorder.file_utils", file_utils_path
    )
    file_utils = importlib.util.module_from_spec(file_utils_spec)
    sys.modules[file_utils_spec.name] = file_utils
    file_utils_spec.loader.exec_module(file_utils)

    path = Path("custom_components/gui_recorder/migration.py")
    spec = importlib.util.spec_from_file_location(
        "custom_components.gui_recorder.migration_under_test", path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


migration = _load_module()


class _Config:
    def __init__(self, root: Path):
        self.root = root

    def path(self, name: str) -> str:
        return str(self.root / name)


class _Hass:
    def __init__(self, root: Path):
        self.config = _Config(root)


class _FixedDateTime:
    @classmethod
    def now(cls):
        return RealDateTime(2026, 10, 22, 12, 34, 56, 123456)


class MigrationSafetyTests(unittest.TestCase):
    def test_backups_are_collision_safe_and_preserve_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "configuration.yaml"
            path.write_text("default_config:\n", encoding="utf-8")
            path.chmod(0o640)

            original_datetime = migration.datetime
            migration.datetime = _FixedDateTime
            try:
                first = Path(migration._make_backup(path))
                second = Path(migration._make_backup(path))
            finally:
                migration.datetime = original_datetime

            self.assertNotEqual(first, second)
            self.assertIn("20261022_123456_123456", first.name)
            self.assertTrue(second.name.endswith("_2"))
            self.assertEqual(first.read_text(encoding="utf-8"), "default_config:\n")
            self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o640)

    def test_invalid_detected_recorder_yaml_is_reported_and_not_imported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "configuration.yaml").write_text(
                "recorder:\n  db_url: never-show-this-secret [\n",
                encoding="utf-8",
            )
            hass = _Hass(root)

            status = migration._detect_sync_status(hass)
            result = migration._import_legacy_sync(hass)

            self.assertTrue(status["legacy_detected"])
            self.assertFalse(status["legacy_parse_ok"])
            self.assertIn("invalid YAML at line 2, column 3", status["legacy_parse_error"])
            self.assertNotIn("never-show-this-secret", status["legacy_parse_error"])
            self.assertFalse(result["ok"])
            self.assertIn("was not imported", result["error"])
            self.assertIn("Fix the YAML", result["error"])
            self.assertNotIn("never-show-this-secret", result["error"])

    def test_enabling_gui_uses_safe_replacement_and_keeps_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "configuration.yaml"
            path.write_text("default_config:\n", encoding="utf-8")
            path.chmod(0o640)

            result = migration._ensure_gui_sync(_Hass(root))

            self.assertTrue(result["ok"])
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "default_config:\n\nrecorder: !include gui_recorder.yaml\n",
            )
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
            self.assertEqual(list(root.glob(f".{path.name}.*.tmp")), [])

    def test_recovers_missing_storage_from_active_managed_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gui_recorder.yaml").write_text(
                "managed-recovery\n", encoding="utf-8"
            )

            result = migration._recover_managed_yaml_sync(_Hass(root))

            self.assertTrue(result["ok"])
            updates = result["storage_updates"]
            self.assertEqual(updates["purge_keep_days"], 21)
            self.assertEqual(updates["commit_interval"], 7)
            self.assertEqual(updates["excluded_entities"], ["sensor.noisy"])
            self.assertEqual(updates["rescued_entities"], ["update.important"])
            self.assertIn("update", updates["manual_exclusions_yaml"])

    def test_unsupported_recorder_options_are_not_silently_dropped(self):
        error = migration._recorder_structure_error({"db_max_retries": 10})

        self.assertIn("unsupported recorder settings", error)
        self.assertIn("db_max_retries", error)

    def test_include_entity_allowlist_is_not_silently_changed_on_import(self):
        summary = {
            "supported_import": {
                "included_entities": ["sensor.only_this_one"],
            },
            "unsupported_filters": {},
        }

        updates, error, _manual = migration._storage_updates_from_summary(
            summary,
            mark_legacy_imported=True,
            restore_include_entities=True,
        )

        self.assertIsNone(updates)
        self.assertIn("include.entities as an allow-list", error)
        self.assertIn("nothing was imported", error)

    def test_include_entity_exception_is_preserved_with_domain_rules(self):
        summary = {
            "supported_import": {
                "included_entities": ["sensor.keep"],
                "db_url_secret": "recorder_db_url",
            },
            "unsupported_filters": {
                "exclude": {"domains": ["sensor"]},
                "include": {},
            },
        }

        updates, error, _manual = migration._storage_updates_from_summary(
            summary,
            mark_legacy_imported=True,
            restore_include_entities=True,
        )

        self.assertIsNone(error)
        self.assertEqual(updates["rescued_entities"], ["sensor.keep"])
        self.assertEqual(updates["db_url_secret"], "recorder_db_url")
        self.assertIsNone(updates["db_url"])

    def test_disable_rolls_back_configuration_if_include_rename_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "configuration.yaml"
            original = "recorder: !include recorder.yaml\ndefault_config:\n"
            config_path.write_text(original, encoding="utf-8")
            (root / "recorder.yaml").write_text(
                "purge_keep_days: 10\n", encoding="utf-8"
            )

            with mock.patch.object(Path, "rename", side_effect=OSError("denied")):
                with self.assertRaises(OSError):
                    migration._disable_legacy_sync(_Hass(root))

            self.assertEqual(config_path.read_text(encoding="utf-8"), original)
            self.assertTrue((root / "recorder.yaml").exists())

    def test_external_include_is_reported_and_never_renamed(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "config"
            root.mkdir()
            config_path = root / "configuration.yaml"
            original = "recorder: !include ../outside-recorder.yaml\n"
            config_path.write_text(original, encoding="utf-8")
            outside = parent / "outside-recorder.yaml"
            outside.write_text("purge_keep_days: 10\n", encoding="utf-8")
            hass = _Hass(root)

            status = migration._detect_sync_status(hass)
            result = migration._disable_legacy_sync(hass)

            self.assertFalse(status["legacy_parse_ok"])
            self.assertIn("inside the Home Assistant", status["legacy_parse_error"])
            self.assertFalse(result["ok"])
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)
            self.assertTrue(outside.exists())


if __name__ == "__main__":
    unittest.main()
