from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import ModuleType
import unittest
from unittest import mock


class _YamlError(Exception):
    pass


def _load_module():
    yaml = ModuleType("yaml")
    yaml.YAMLError = _YamlError

    def compose(content):
        if content == "invalid YAML":
            raise _YamlError("invalid")
        return object()

    yaml.compose = compose
    sys.modules["yaml"] = yaml

    path = Path("custom_components/gui_recorder/file_utils.py")
    spec = importlib.util.spec_from_file_location(
        "custom_components.gui_recorder.file_utils_under_test", path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


file_utils = _load_module()


class AtomicWriteTests(unittest.TestCase):
    def test_replaces_content_and_preserves_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "configuration.yaml"
            path.write_text("old\n", encoding="utf-8")
            path.chmod(0o640)

            file_utils.atomic_write_utf8(path, "new\n", validate_yaml=True)

            self.assertEqual(path.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_invalid_yaml_leaves_existing_file_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "configuration.yaml"
            path.write_text("known-good\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Refusing to write invalid YAML"):
                file_utils.atomic_write_utf8(path, "invalid YAML", validate_yaml=True)

            self.assertEqual(path.read_text(encoding="utf-8"), "known-good\n")
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_replace_failure_cleans_up_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "configuration.yaml"
            path.write_text("old\n", encoding="utf-8")

            with mock.patch.object(file_utils.os, "replace", side_effect=OSError("busy")):
                with self.assertRaisesRegex(OSError, "busy"):
                    file_utils.atomic_write_utf8(path, "new\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "old\n")
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
