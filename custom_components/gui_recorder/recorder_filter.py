from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.helpers.entityfilter import generate_filter

from .yaml_writer import parse_manual_exclusions


def _manual_sections(data: dict) -> tuple[dict, dict]:
    try:
        manual = parse_manual_exclusions(data.get("manual_exclusions_yaml", ""))
    except ValueError:
        manual = {}
    return manual.get("include", {}), manual.get("exclude", {})


def _has_domain_or_glob(include: dict, exclude: dict) -> bool:
    return any(
        section.get(key)
        for section in (include, exclude)
        for key in ("domains", "entity_globs")
    )


def build_recorder_filter(data: dict) -> Callable[[str], bool]:
    """Build the same six-case entity filter as Home Assistant Recorder."""
    include, exclude = _manual_sections(data)
    rescued = list(data.get("rescued_entities") or [])
    include_entities = rescued if _has_domain_or_glob(include, exclude) else []
    return generate_filter(
        list(include.get("domains") or []),
        include_entities,
        list(exclude.get("domains") or []),
        list(data.get("excluded_entities") or []),
        list(include.get("entity_globs") or []),
        list(exclude.get("entity_globs") or []),
    )


def build_manual_filter(data: dict) -> Callable[[str], bool]:
    """Build only the user's domain/glob rules for explanatory UI flags."""
    include, exclude = _manual_sections(data)
    return generate_filter(
        list(include.get("domains") or []),
        [],
        list(exclude.get("domains") or []),
        [],
        list(include.get("entity_globs") or []),
        list(exclude.get("entity_globs") or []),
    )


def apply_recorded_state(
    data: dict,
    entity_ids: Iterable[str],
    recorded: bool,
) -> bool:
    """Make every entity reach the requested effective Recorder state."""
    excluded = set(data.get("excluded_entities") or [])
    rescued = set(data.get("rescued_entities") or [])
    before = (set(excluded), set(rescued))

    normalized_ids = {
        value.strip()
        for value in entity_ids
        if isinstance(value, str) and value.strip()
    }
    if recorded:
        excluded.difference_update(normalized_ids)
        # Remove all selected IDs before evaluating them. Compiling Home
        # Assistant's glob/domain filter once makes "Record all" effectively
        # O(n) instead of reparsing YAML and recompiling regexes for every row.
        rescued.difference_update(normalized_ids)
        candidate = {
            **data,
            "excluded_entities": sorted(excluded),
            "rescued_entities": sorted(rescued),
        }
        without_new_rescues = build_recorder_filter(candidate)
        rescued.update(
            entity_id
            for entity_id in normalized_ids
            if not without_new_rescues(entity_id)
        )
    else:
        rescued.difference_update(normalized_ids)
        excluded.update(normalized_ids)

    if (excluded, rescued) == before:
        return False
    data["excluded_entities"] = sorted(excluded)
    data["rescued_entities"] = sorted(rescued)
    return True
