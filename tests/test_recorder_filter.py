from __future__ import annotations

import fnmatch
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
import unittest


def _generate_filter(
    include_domains,
    include_entities,
    exclude_domains,
    exclude_entities,
    include_globs=None,
    exclude_globs=None,
):
    include_domains = set(include_domains)
    include_entities = set(include_entities)
    exclude_domains = set(exclude_domains)
    exclude_entities = set(exclude_entities)
    include_globs = list(include_globs or [])
    exclude_globs = list(exclude_globs or [])
    have_include = bool(include_domains or include_entities or include_globs)
    have_exclude = bool(exclude_domains or exclude_entities or exclude_globs)

    def matches(globs, entity_id):
        return any(fnmatch.fnmatchcase(entity_id, glob) for glob in globs)

    if not have_include and not have_exclude:
        return bool
    if have_include and not have_exclude:
        return lambda entity_id: (
            entity_id in include_entities
            or entity_id.split(".", 1)[0] in include_domains
            or matches(include_globs, entity_id)
        )
    if have_exclude and not have_include:
        return lambda entity_id: not (
            entity_id in exclude_entities
            or entity_id.split(".", 1)[0] in exclude_domains
            or matches(exclude_globs, entity_id)
        )
    if include_domains or include_globs:
        return lambda entity_id: entity_id in include_entities or (
            entity_id not in exclude_entities
            and (
                matches(include_globs, entity_id)
                or (
                    entity_id.split(".", 1)[0] in include_domains
                    and not matches(exclude_globs, entity_id)
                )
            )
        )
    if exclude_domains or exclude_globs:
        return lambda entity_id: (
            entity_id in include_entities
            if entity_id.split(".", 1)[0] in exclude_domains
            or matches(exclude_globs, entity_id)
            else entity_id not in exclude_entities
        )
    return lambda entity_id: entity_id in include_entities


def _load_module():
    custom_components = ModuleType("custom_components")
    package = ModuleType("custom_components.gui_recorder")
    package.__path__ = []
    homeassistant = ModuleType("homeassistant")
    helpers = ModuleType("homeassistant.helpers")
    entityfilter = ModuleType("homeassistant.helpers.entityfilter")
    entityfilter.generate_filter = _generate_filter
    yaml_writer = ModuleType("custom_components.gui_recorder.yaml_writer")
    yaml_writer.parse_manual_exclusions = lambda text: json.loads(text) if text else {}
    sys.modules.update(
        {
            "custom_components": custom_components,
            "custom_components.gui_recorder": package,
            "custom_components.gui_recorder.yaml_writer": yaml_writer,
            "homeassistant": homeassistant,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.entityfilter": entityfilter,
        }
    )
    path = Path("custom_components/gui_recorder/recorder_filter.py")
    spec = importlib.util.spec_from_file_location(
        "custom_components.gui_recorder.recorder_filter_under_test", path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


recorder_filter = _load_module()


class RecorderFilterTests(unittest.TestCase):
    def test_include_domain_is_an_allowlist(self):
        data = {
            "manual_exclusions_yaml": json.dumps(
                {"include": {"domains": ["sensor"]}}
            ),
            "excluded_entities": [],
            "rescued_entities": [],
        }

        entity_filter = recorder_filter.build_recorder_filter(data)

        self.assertTrue(entity_filter("sensor.temperature"))
        self.assertFalse(entity_filter("light.kitchen"))

    def test_record_on_adds_rescue_outside_manual_allowlist(self):
        data = {
            "manual_exclusions_yaml": json.dumps(
                {"include": {"domains": ["sensor"]}}
            ),
            "excluded_entities": [],
            "rescued_entities": [],
        }

        changed = recorder_filter.apply_recorded_state(
            data, ["light.kitchen", "switch.fan"], True
        )

        self.assertTrue(changed)
        self.assertEqual(
            data["rescued_entities"], ["light.kitchen", "switch.fan"]
        )
        entity_filter = recorder_filter.build_recorder_filter(data)
        self.assertTrue(entity_filter("light.kitchen"))
        self.assertTrue(entity_filter("switch.fan"))

    def test_bulk_record_on_compiles_manual_filter_once(self):
        data = {
            "manual_exclusions_yaml": json.dumps(
                {"exclude": {"domains": ["sensor"]}}
            ),
            "excluded_entities": ["sensor.a", "light.a"],
            "rescued_entities": ["sensor.a", "light.a"],
        }
        calls = 0
        original = recorder_filter.build_recorder_filter

        def counted(current):
            nonlocal calls
            calls += 1
            return original(current)

        recorder_filter.build_recorder_filter = counted
        try:
            recorder_filter.apply_recorded_state(
                data, ["sensor.a", "sensor.b", "light.a"], True
            )
        finally:
            recorder_filter.build_recorder_filter = original

        self.assertEqual(calls, 1)
        self.assertEqual(data["excluded_entities"], [])
        self.assertEqual(data["rescued_entities"], ["sensor.a", "sensor.b"])

    def test_record_off_overrides_included_domain(self):
        data = {
            "manual_exclusions_yaml": json.dumps(
                {"include": {"domains": ["sensor"]}}
            ),
            "excluded_entities": [],
            "rescued_entities": [],
        }

        recorder_filter.apply_recorded_state(data, ["sensor.temperature"], False)

        self.assertFalse(
            recorder_filter.build_recorder_filter(data)("sensor.temperature")
        )

    def test_rescue_overrides_excluded_domain(self):
        data = {
            "manual_exclusions_yaml": json.dumps(
                {"exclude": {"domains": ["sensor"]}}
            ),
            "excluded_entities": [],
            "rescued_entities": [],
        }

        recorder_filter.apply_recorded_state(data, ["sensor.temperature"], True)

        self.assertEqual(data["rescued_entities"], ["sensor.temperature"])
        self.assertTrue(
            recorder_filter.build_recorder_filter(data)("sensor.temperature")
        )

    def test_no_manual_rules_does_not_keep_stale_rescue(self):
        data = {
            "manual_exclusions_yaml": "",
            "excluded_entities": ["sensor.temperature"],
            "rescued_entities": ["sensor.temperature"],
        }

        recorder_filter.apply_recorded_state(data, ["sensor.temperature"], True)

        self.assertEqual(data["excluded_entities"], [])
        self.assertEqual(data["rescued_entities"], [])


if __name__ == "__main__":
    unittest.main()
