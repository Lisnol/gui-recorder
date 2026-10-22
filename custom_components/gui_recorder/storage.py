from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from typing import Any, TypeVar

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DEFAULT_STORAGE, DOMAIN, STORAGE_KEY, STORAGE_VERSION

_ENTITY_LIST_KEYS = ("excluded_entities", "rescued_entities")
_T = TypeVar("_T")


def _normalize_entity_list(values) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        entity_id = str(value).strip()
        if not entity_id or entity_id in seen:
            continue
        seen.add(entity_id)
        normalized.append(entity_id)
    return sorted(normalized)


def _normalize_entity_lists(data: dict) -> dict:
    for key in _ENTITY_LIST_KEYS:
        data[key] = _normalize_entity_list(data.get(key))
    return data


def get_store(hass: HomeAssistant) -> Store:
    """Return the one Store instance used by this integration."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    store = domain_data.get("store")
    if store is None:
        # Atomic writes prevent an interrupted host write from leaving invalid
        # JSON in .storage. Reusing the same Store also preserves its own write
        # serialization instead of creating a fresh lock for every reload.
        store = Store(
            hass,
            STORAGE_VERSION,
            STORAGE_KEY,
            atomic_writes=True,
        )
        domain_data["store"] = store
    return store


def _get_lock(hass: HomeAssistant) -> asyncio.Lock:
    domain_data = hass.data.setdefault(DOMAIN, {})
    lock = domain_data.get("storage_lock")
    if lock is None:
        lock = asyncio.Lock()
        domain_data["storage_lock"] = lock
    return lock


def _normalized_copy(data: dict | None) -> dict:
    normalized = deepcopy(DEFAULT_STORAGE)
    if isinstance(data, dict):
        normalized.update(deepcopy(data))
        # Storage schema version 1 predates several optional statistics fields.
        # Merge that nested mapping separately so an upgrade does not discard
        # newer defaults merely because an older ``stats`` object exists.
        default_stats = DEFAULT_STORAGE.get("stats")
        stored_stats = data.get("stats")
        if isinstance(default_stats, dict):
            normalized_stats = deepcopy(default_stats)
            if isinstance(stored_stats, dict):
                normalized_stats.update(deepcopy(stored_stats))
            normalized["stats"] = normalized_stats
    return _normalize_entity_lists(normalized)


async def async_load_data(hass: HomeAssistant) -> dict:
    async with _get_lock(hass):
        store = get_store(hass)
        stored = await store.async_load()
        data = _normalized_copy(stored)
        domain_data = hass.data[DOMAIN]
        # Keep this fact until initialization has either recovered the active
        # generated YAML or explicitly persisted first-run defaults. Saving
        # defaults here would destroy the only reliable recovery signal.
        domain_data["storage_was_missing"] = stored is None
        domain_data["data"] = data
        return deepcopy(data)


async def async_save_data(hass: HomeAssistant, data: dict) -> dict:
    """Replace storage with a snapshot.

    Runtime read-modify-write paths should use async_update_data so a stale
    snapshot cannot overwrite an unrelated concurrent change.
    """
    async with _get_lock(hass):
        store = get_store(hass)
        normalized = _normalized_copy(data)
        await store.async_save(normalized)
        hass.data[DOMAIN]["data"] = normalized
        return deepcopy(normalized)


async def async_update_data(
    hass: HomeAssistant,
    mutator: Callable[[dict], _T],
) -> tuple[dict, _T]:
    """Atomically mutate and persist the latest integration data.

    The callback is synchronous on purpose: it runs while the integration lock
    is held and must not yield control. It receives an isolated deep copy, so
    readers always see either the old complete snapshot or the new one.
    """
    async with _get_lock(hass):
        store = get_store(hass)
        current = _normalized_copy(hass.data[DOMAIN].get("data"))
        result = mutator(current)
        normalized = _normalized_copy(current)
        await store.async_save(normalized)
        hass.data[DOMAIN]["data"] = normalized
        return deepcopy(normalized), result


async def async_patch_data(hass: HomeAssistant, **updates: Any) -> dict:
    """Atomically update top-level storage keys."""

    def _patch(data: dict) -> None:
        data.update(deepcopy(updates))

    data, _ = await async_update_data(hass, _patch)
    return data


async def async_reload_data(hass: HomeAssistant) -> dict:
    async with _get_lock(hass):
        store = get_store(hass)
        stored = await store.async_load()
        data = _normalized_copy(stored)
        hass.data[DOMAIN]["data"] = data
        return deepcopy(data)
