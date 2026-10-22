from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
import json
from functools import partial
import logging
from pathlib import Path
from typing import TypeVar

import yaml

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .file_utils import atomic_write_utf8
from .storage import async_update_data

_ALLOWED_EXCLUDE_KEYS = ("domains", "entity_globs", "event_types")
_ALLOWED_INCLUDE_KEYS = ("domains", "entity_globs")
_ALLOWED_TOP_KEYS = {"exclude": _ALLOWED_EXCLUDE_KEYS, "include": _ALLOWED_INCLUDE_KEYS}
_T = TypeVar("_T")
_LOGGER = logging.getLogger(__name__)


def parse_manual_exclusions(text: str) -> dict[str, dict[str, list[str]]]:
    """Parse and validate the user-provided manual exclusions YAML block.

    Only exclude.{domains,entity_globs,event_types} and include.{domains,entity_globs}
    are accepted here - individual entities stay exclusively managed by the entity
    toggles, so the two systems never fight over the same list. Raises ValueError
    with a user-facing message on anything else.
    """
    text = (text or "").strip()
    if not text:
        return {}

    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise ValueError(f"Invalid YAML: {err}") from err

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError("The manual exclusions block must be a YAML mapping (e.g. 'exclude:' / 'include:').")

    result: dict[str, dict[str, list[str]]] = {}
    for top_key, value in loaded.items():
        if top_key not in _ALLOWED_TOP_KEYS:
            raise ValueError(f"Unsupported key '{top_key}'. Only 'exclude' and 'include' are allowed here.")
        if not isinstance(value, dict):
            raise ValueError(f"'{top_key}' must be a mapping.")

        section: dict[str, list[str]] = {}
        for sub_key, items in value.items():
            if sub_key not in _ALLOWED_TOP_KEYS[top_key]:
                if sub_key == "entities":
                    raise ValueError(
                        f"'{top_key}.entities' is not allowed here - use the entity toggles in the panel "
                        "for individual entities instead."
                    )
                raise ValueError(
                    f"Unsupported key '{top_key}.{sub_key}'. Allowed: {', '.join(_ALLOWED_TOP_KEYS[top_key])}."
                )
            if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
                raise ValueError(f"'{top_key}.{sub_key}' must be a list of strings.")
            cleaned = sorted({item.strip() for item in items if item.strip()})
            if cleaned:
                section[sub_key] = cleaned

        if section:
            result[top_key] = section

    return result


async def _async_write_yaml_locked(hass: HomeAssistant) -> tuple[str, dict]:
    data = hass.data[DOMAIN]["data"]
    generated_path = data.get("generated_path", "gui_recorder.yaml")
    excluded_entities = sorted(set(data.get("excluded_entities", [])))
    rescued_entities = sorted(set(data.get("rescued_entities", [])))
    purge_keep_days = int(data.get("purge_keep_days", 10))
    auto_purge = bool(data.get("auto_purge", True))
    auto_repack = bool(data.get("auto_repack", True))
    commit_interval = int(data.get("commit_interval", 5))
    db_url = data.get("db_url")
    db_url_secret = data.get("db_url_secret")

    # generated_path is an internal compatibility field, not a user-facing
    # arbitrary path. Refuse a tampered storage value rather than allowing a
    # configuration action to replace an unrelated file.
    if generated_path != "gui_recorder.yaml":
        raise ValueError(
            "Refusing to write Recorder configuration outside gui_recorder.yaml."
        )
    path = Path(hass.config.path(generated_path))

    try:
        manual = parse_manual_exclusions(data.get("manual_exclusions_yaml", ""))
    except ValueError as err:
        # This is normally validated before it reaches storage. If the storage
        # file was edited or damaged, failing closed is safer than silently
        # replacing gui_recorder.yaml without the user's manual filters.
        raise ValueError(
            "Stored manual Recorder filters are invalid; refusing to replace "
            "gui_recorder.yaml."
        ) from err

    # Build ordered dicts (Python 3.7+ preserves insertion order) so safe_dump
    # emits them in our preferred order: entities first under exclude, then the
    # rest. safe_dump handles quoting for values with YAML-significant chars
    # (e.g. globs starting with '*' which YAML would otherwise parse as alias
    # references and reject — see https://github.com/martinandersen84/gui-recorder).
    manual_exclude = manual.get("exclude", {})
    exclude_block: dict[str, list[str]] = {}
    if excluded_entities:
        exclude_block["entities"] = excluded_entities
    for key in _ALLOWED_EXCLUDE_KEYS:
        if manual_exclude.get(key):
            exclude_block[key] = manual_exclude[key]

    manual_include = manual.get("include", {})
    include_block: dict[str, list[str]] = {}

    # Entities rescued from the user's own exclude domains/globs. include.entities is
    # the only rule that outranks those, but it is only safe while such a filter is
    # still configured: that keeps the recorder in entityfilter case 4a/4b, where
    # everything unmatched keeps recording. With no domain or glob filter left, the
    # same key would turn the whole config into an allowlist (case 6) - and nothing
    # would be hidden in the first place, so there is nothing to rescue either.
    has_domain_or_glob = any(
        section.get(key)
        for section, keys in ((manual_exclude, ("domains", "entity_globs")), (manual_include, ("domains", "entity_globs")))
        for key in keys
    )
    if rescued_entities and has_domain_or_glob:
        include_block["entities"] = rescued_entities

    for key in _ALLOWED_INCLUDE_KEYS:
        if manual_include.get(key):
            include_block[key] = manual_include[key]

    parts: list[str] = []
    parts.append("# This file is managed by the GUI Recorder integration.")
    parts.append("# Manual edits will be overwritten.")
    parts.append("")
    parts.append(f"auto_purge: {'true' if auto_purge else 'false'}")
    parts.append(f"auto_repack: {'true' if auto_repack else 'false'}")
    parts.append(f"purge_keep_days: {purge_keep_days}")
    parts.append(f"commit_interval: {commit_interval}")
    if isinstance(db_url_secret, str) and db_url_secret.strip():
        # Keep only the secret's name in storage. Home Assistant resolves the
        # actual MariaDB URL when it loads the generated recorder configuration.
        parts.append(f"db_url: !secret {json.dumps(db_url_secret.strip())}")
    elif isinstance(db_url, str) and db_url.strip():
        # Custom SQLite paths are not credentials and can be stored directly.
        if not db_url.strip().lower().startswith("sqlite"):
            raise ValueError(
                "A non-SQLite db_url must be stored in secrets.yaml and "
                "referenced by db_url_secret."
            )
        parts.append(yaml.safe_dump({"db_url": db_url.strip()}, default_flow_style=False).rstrip())
    parts.append("")

    if exclude_block:
        parts.append(yaml.safe_dump({"exclude": exclude_block}, sort_keys=False, default_flow_style=False).rstrip())
    else:
        parts.append("exclude: {}")

    if include_block:
        parts.append("")
        parts.append(yaml.safe_dump({"include": include_block}, sort_keys=False, default_flow_style=False).rstrip())

    content = "\n".join(parts) + "\n"
    await hass.async_add_executor_job(
        partial(atomic_write_utf8, path, content, validate_yaml=True)
    )
    return str(path), data


async def async_write_yaml(hass: HomeAssistant) -> str:
    """Serialize generated YAML writes so an older snapshot cannot win a race."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    lock = domain_data.get("yaml_write_lock")
    if lock is None:
        lock = asyncio.Lock()
        domain_data["yaml_write_lock"] = lock
    async with lock:
        while True:
            path, source_data = await _async_write_yaml_locked(hass)
            # Storage updates replace the complete in-memory snapshot. If one
            # happened while the executor was writing, immediately rewrite the
            # newest snapshot before releasing the lock. This closes the small
            # cancellation/race window between saving storage and writing YAML.
            if hass.data[DOMAIN].get("data") is source_data:
                return path


async def async_update_and_write_yaml(
    hass: HomeAssistant,
    mutator: Callable[[dict], _T],
    *,
    should_write: Callable[[_T], bool] | None = None,
) -> tuple[dict, _T, str | None]:
    """Persist a configuration mutation and keep YAML/storage consistent.

    The Store write necessarily happens before the generated YAML write. If
    the latter fails, restore only the top-level keys changed by this mutation;
    unrelated statistics or purge checkpoints saved concurrently are retained.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    lock = domain_data.get("configuration_update_lock")
    if lock is None:
        lock = asyncio.Lock()
        domain_data["configuration_update_lock"] = lock

    async with lock:
        before_holder: dict[str, dict] = {}

        def _mutate_with_snapshot(current: dict) -> _T:
            # Capture the rollback baseline while storage's lock is held. A
            # snapshot taken before acquiring that lock could miss an analysis
            # or checkpoint update and later roll that unrelated update back.
            before_holder["data"] = deepcopy(current)
            return mutator(current)

        data, result = await async_update_data(hass, _mutate_with_snapshot)
        before = before_holder["data"]
        if should_write is not None and not should_write(result):
            return data, result, None

        changed_keys = {
            key
            for key in set(before) | set(data)
            if before.get(key) != data.get(key)
            or (key in before) != (key in data)
        }
        try:
            path = await async_write_yaml(hass)
        except Exception:
            def _rollback(current: dict) -> None:
                for key in changed_keys:
                    # Do not overwrite a newer value if an out-of-band update
                    # happened while the YAML write was in the executor.
                    if current.get(key) != data.get(key) or (
                        (key in current) != (key in data)
                    ):
                        continue
                    if key in before:
                        current[key] = deepcopy(before[key])
                    else:
                        current.pop(key, None)

            try:
                await async_update_data(hass, _rollback)
            except Exception:  # noqa: BLE001 - keep the original YAML error
                _LOGGER.exception(
                    "Could not roll back GUI Recorder storage after a YAML write failure"
                )
            raise
        return data, result, path
