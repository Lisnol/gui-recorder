from __future__ import annotations

import fnmatch
import re

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, INTEGRATION_VERSION
from .db_stats import async_analyze_db
from .migration import async_detect_sync_status, async_disable_legacy, async_ensure_gui_enabled, async_import_legacy
from .purge_jobs import (
    async_start_purge_job,
    purge_history,
    purge_is_active,
    purge_progress,
)
from .storage import async_reload_data, async_save_data
from .yaml_writer import async_write_yaml, parse_manual_exclusions



def _normalize_entity_id(value: str) -> str:
    return str(value).strip()


def _normalize_excluded(values) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        entity_id = _normalize_entity_id(value)
        if not entity_id or entity_id in seen:
            continue
        seen.add(entity_id)
        normalized.append(entity_id)
    return sorted(normalized)


def _manual_exclude_filters(data: dict) -> tuple[set[str], re.Pattern[str] | None]:
    """Domains and globs from the manual field that hide entities from the recorder.

    The glob pattern is built exactly the way homeassistant.helpers.entityfilter
    builds it (fnmatch.translate joined with "|", matched with .match), so what we
    consider hidden is what the recorder actually skips.
    """
    try:
        manual = parse_manual_exclusions(data.get("manual_exclusions_yaml", ""))
    except ValueError:
        return set(), None

    exclude = manual.get("exclude", {})
    domains = set(exclude.get("domains") or [])
    globs = exclude.get("entity_globs") or []
    pattern = re.compile("(?:" + "|".join(fnmatch.translate(glob) for glob in set(globs)) + ")") if globs else None
    return domains, pattern


def _is_glob_hidden(entity_id: str, domains: set[str], pattern: re.Pattern[str] | None) -> bool:
    """True if one of the user's own manual exclude domains/globs matches this entity."""
    if entity_id.split(".", 1)[0] in domains:
        return True
    return bool(pattern and pattern.match(entity_id))


def _entity_recorded(entity_id: str, excluded: set[str], rescued: set[str], hidden: bool) -> bool:
    """Whether the recorder will actually record this entity.

    A hidden entity is only recorded when it is on the rescue list: with exclude
    domains/globs present, include.entities is the only rule that outranks them
    (entityfilter case 4b), and it also outranks exclude.entities.
    """
    if hidden:
        return entity_id in rescued
    return entity_id not in excluded


def _apply_toggle(data: dict, entity_ids: set[str], recorded: bool, rescue: bool = False) -> bool:
    """Move entity_ids to the wanted state across both lists. True if anything changed.

    rescue=True only for the single-entity toggle. Bulk actions (Select all, a whole
    device) leave entities hidden by the user's own globs alone: rescuing in bulk
    would silently write one include.entities entry per hidden entity and override
    the user's globs wholesale. Rescuing stays an explicit, per-entity decision.
    """
    excluded = set(_normalize_excluded(data.get("excluded_entities", [])))
    rescued = set(_normalize_excluded(data.get("rescued_entities", [])))
    before = (set(excluded), set(rescued))
    domains, pattern = _manual_exclude_filters(data)

    for entity_id in entity_ids:
        if recorded:
            excluded.discard(entity_id)
            # Removing it from exclude.entities is not enough when one of the user's
            # own globs hides it - that needs include.entities, which is what the
            # rescue list becomes.
            if rescue and _is_glob_hidden(entity_id, domains, pattern):
                rescued.add(entity_id)
        else:
            rescued.discard(entity_id)
            excluded.add(entity_id)

    if (excluded, rescued) == before:
        return False

    data["excluded_entities"] = _normalize_excluded(excluded)
    data["rescued_entities"] = _normalize_excluded(rescued)
    return True

async def _save_and_reload(hass: HomeAssistant, data: dict) -> dict:
    await async_save_data(hass, data)
    await async_write_yaml(hass)
    reloaded = await async_reload_data(hass)
    return reloaded


async def _set_entity_recorded_state(hass: HomeAssistant, entity_id: str, recorded: bool) -> tuple[dict, bool]:
    data = await async_reload_data(hass)
    entity_id = _normalize_entity_id(entity_id)

    changed = _apply_toggle(data, {entity_id}, recorded, rescue=True)
    if not changed:
        return data, False

    data["pending_restart"] = True
    reloaded = await _save_and_reload(hass, data)
    return reloaded, True


def _build_stats_payload(hass: HomeAssistant) -> dict:
    data = hass.data[DOMAIN]["data"]
    stats = data.get("stats", {})
    entity_counts = stats.get("entity_counts", {}) or {}
    total_rows = stats.get("total_rows")
    if total_rows is None:
        total_rows = sum(entity_counts.values())
    return {
        "entity_counts": entity_counts,
        "generated_at": stats.get("generated_at"),
        "backend": stats.get("backend"),
        "database": stats.get("database", stats.get("db_path", "home-assistant_v2.db")),
        "db_path": stats.get("db_path", "home-assistant_v2.db"),
        "total_rows": total_rows,
        "db_size_bytes": int(stats["db_size_bytes"]) if stats.get("db_size_bytes") is not None else None,
        "db_data_bytes": int(stats["db_data_bytes"]) if stats.get("db_data_bytes") is not None else None,
        "db_index_bytes": int(stats["db_index_bytes"]) if stats.get("db_index_bytes") is not None else None,
        "db_free_bytes": int(stats["db_free_bytes"]) if stats.get("db_free_bytes") is not None else None,
        "db_product": stats.get("db_product"),
        "db_server_version": stats.get("db_server_version"),
        "db_character_set": stats.get("db_character_set"),
        "db_collation": stats.get("db_collation"),
        "db_default_engine": stats.get("db_default_engine"),
        "db_non_innodb_tables": stats.get("db_non_innodb_tables"),
        "db_health_status": stats.get("db_health_status"),
        "db_health_issues": list(stats.get("db_health_issues") or []),
        "db_health_error": stats.get("db_health_error"),
        "error": stats.get("error"),
    }


@callback
def _build_rows(hass: HomeAssistant) -> dict:
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    data = hass.data[DOMAIN]["data"]
    excluded = set(data.get("excluded_entities", []))
    rescued = set(data.get("rescued_entities", []))
    manual_domains, manual_pattern = _manual_exclude_filters(data)

    def _row_flags(entity_id: str) -> tuple[bool, bool]:
        hidden = _is_glob_hidden(entity_id, manual_domains, manual_pattern)
        return _entity_recorded(entity_id, excluded, rescued, hidden), hidden

    stats_payload = _build_stats_payload(hass)
    entity_counts = stats_payload.get("entity_counts", {})

    devices: dict[str, dict] = {}
    orphan_entities: list[dict] = []
    obsolete_entities: list[dict] = []
    current_entity_ids: set[str] = set()

    def row_sort_key(entity: dict) -> tuple:
        return (-int(entity.get("record_count", 0)), entity["entity_id"])

    for entity_entry in entity_registry.entities.values():
        current_entity_ids.add(entity_entry.entity_id)
        row_recorded, row_hidden = _row_flags(entity_entry.entity_id)
        row = {
            "entity_id": entity_entry.entity_id,
            "name": entity_entry.original_name or entity_entry.name or entity_entry.entity_id,
            "disabled_by": str(entity_entry.disabled_by) if entity_entry.disabled_by else None,
            "hidden_by": str(entity_entry.hidden_by) if entity_entry.hidden_by else None,
            "platform": entity_entry.platform,
            "domain": entity_entry.domain,
            "device_id": entity_entry.device_id,
            "recorded": row_recorded,
            "glob_hidden": row_hidden,
            "record_count": int(entity_counts.get(entity_entry.entity_id, 0)),
        }

        if entity_entry.device_id:
            device = device_registry.async_get(entity_entry.device_id)
            if device:
                dev = devices.setdefault(
                    device.id,
                    {
                        "device_id": device.id,
                        "name": device.name_by_user or device.name or "Unnamed device",
                        "manufacturer": device.manufacturer,
                        "model": device.model,
                        "entities": [],
                        "record_count": 0,
                    },
                )
                dev["entities"].append(row)
                dev["record_count"] += row["record_count"]
                continue

        orphan_entities.append(row)

    live_entity_ids = set(hass.states.async_entity_ids())

    for entity_id, count in entity_counts.items():
        if entity_id in current_entity_ids:
            continue
        domain = entity_id.split(".", 1)[0] if "." in entity_id else "unknown"
        if entity_id in live_entity_ids:
            # Has a live state but no entity_registry entry (e.g. legacy YAML
            # platforms like snmp/template/command_line without unique_id).
            # It's not obsolete, just unregistered - list it as an orphan.
            state_obj = hass.states.get(entity_id)
            friendly_name = state_obj.attributes.get("friendly_name") if state_obj else None
            current_entity_ids.add(entity_id)
            orphan_entities.append(
                {
                    "entity_id": entity_id,
                    "name": friendly_name or entity_id,
                    "disabled_by": None,
                    "hidden_by": None,
                    "platform": domain,
                    "domain": domain,
                    "device_id": None,
                    "recorded": _row_flags(entity_id)[0],
                    "glob_hidden": _row_flags(entity_id)[1],
                    "record_count": int(count),
                }
            )
            continue
        obsolete_entities.append(
            {
                "entity_id": entity_id,
                "name": entity_id,
                "platform": domain,
                "domain": domain,
                "device_id": None,
                "recorded": _row_flags(entity_id)[0],
                "glob_hidden": _row_flags(entity_id)[1],
                "record_count": int(count),
                "obsolete": True,
            }
        )

    device_rows = sorted(devices.values(), key=lambda d: (-int(d.get("record_count", 0)), d["name"].lower()))
    for dev in device_rows:
        dev["entities"] = sorted(dev["entities"], key=row_sort_key)

    orphan_entities = sorted(orphan_entities, key=row_sort_key)
    obsolete_entities = sorted(obsolete_entities, key=row_sort_key)

    # An entity that exists right now but has no rows in the database (because it
    # is excluded and its history was already purged) is still a valid exclusion.
    # Registry-less entities (sun.sun, YAML platforms without unique_id) only reach
    # current_entity_ids through the entity_counts loop above, so they would drop
    # out of it as soon as their records are gone and be reported as unmatched.
    known_entity_ids = current_entity_ids | live_entity_ids

    matched_exclusions = sorted(entity_id for entity_id in excluded if entity_id in known_entity_ids)
    unmatched_exclusions = sorted(entity_id for entity_id in excluded if entity_id not in known_entity_ids)

    current_rows = sum(int(entity_counts.get(entity_id, 0)) for entity_id in current_entity_ids)
    obsolete_rows = sum(int(item["record_count"]) for item in obsolete_entities)
    stats_payload["current_rows"] = current_rows
    stats_payload["obsolete_rows"] = obsolete_rows
    stats_payload["obsolete_entity_count"] = len(obsolete_entities)
    stats_payload["configured_exclusions"] = len(excluded)
    stats_payload["matched_exclusions"] = len(matched_exclusions)
    stats_payload["unmatched_exclusions"] = len(unmatched_exclusions)

    return {
        "devices": device_rows,
        "orphans": orphan_entities,
        "obsolete": obsolete_entities,
        "rescued_entities": sorted(rescued),
        "pending_restart": bool(data.get("pending_restart", False)),
        "purge_keep_days": int(data.get("purge_keep_days", 10)),
        "auto_purge": bool(data.get("auto_purge", True)),
        "auto_repack": bool(data.get("auto_repack", True)),
        "commit_interval": int(data.get("commit_interval", 5)),
        "auto_update_data": bool(data.get("auto_update_data", True)),
        "repack_after_manual_purge": bool(data.get("repack_after_manual_purge", False)),
        "notify_on_purge_finish": bool(data.get("notify_on_purge_finish", True)),
        "repack_in_progress": bool(data.get("repack_in_progress", False)),
        "stats": stats_payload,
        "migration": hass.data[DOMAIN].get("migration", {}),
        "matched_exclusions": matched_exclusions,
        "unmatched_exclusions": unmatched_exclusions,
        "manual_exclusions_yaml": data.get("manual_exclusions_yaml", ""),
        "version": INTEGRATION_VERSION,
    }


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "gui_recorder/get_tree"})
@callback
def ws_get_tree(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    connection.send_result(msg["id"], _build_rows(hass))


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command(
    {
        vol.Required("type"): "gui_recorder/set_entity",
        vol.Required("entity_id"): str,
        vol.Required("recorded"): bool,
    }
)
async def ws_set_entity(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    _, changed = await _set_entity_recorded_state(hass, msg["entity_id"], msg["recorded"])
    connection.send_result(
        msg["id"],
        {
            "ok": True,
            "changed": changed,
            "restart_required": changed,
            "removed": changed if msg["recorded"] else False,
        },
    )


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command(
    {
        vol.Required("type"): "gui_recorder/set_device",
        vol.Required("device_id"): str,
        vol.Required("recorded"): bool,
    }
)
async def ws_set_device(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    entity_registry = er.async_get(hass)
    data = await async_reload_data(hass)
    device_entity_ids = {
        entry.entity_id
        for entry in entity_registry.entities.values()
        if entry.device_id == msg["device_id"]
    }

    changed = _apply_toggle(data, device_entity_ids, msg["recorded"])
    generated_file = None
    if changed:
        data["pending_restart"] = True
        await async_save_data(hass, data)
        generated_file = await async_write_yaml(hass)

    connection.send_result(msg["id"], {"ok": True, "generated_file": generated_file, "restart_required": changed, "changed": changed})


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command(
    {
        vol.Required("type"): "gui_recorder/set_entities_bulk",
        vol.Required("entity_ids"): [str],
        vol.Required("recorded"): bool,
    }
)
async def ws_set_entities_bulk(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    data = await async_reload_data(hass)
    entity_ids = {_normalize_entity_id(entity_id) for entity_id in msg["entity_ids"]}

    changed = _apply_toggle(data, entity_ids, msg["recorded"])
    generated_file = None
    if changed:
        data["pending_restart"] = True
        await async_save_data(hass, data)
        generated_file = await async_write_yaml(hass)

    connection.send_result(
        msg["id"],
        {"ok": True, "generated_file": generated_file, "restart_required": changed, "changed": changed, "count": len(entity_ids)},
    )


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command(
    {
        vol.Required("type"): "gui_recorder/set_manual_exclusions",
        vol.Required("yaml_text"): str,
    }
)
async def ws_set_manual_exclusions(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    yaml_text = msg["yaml_text"]
    try:
        parse_manual_exclusions(yaml_text)
    except ValueError as err:
        connection.send_result(msg["id"], {"ok": False, "error": str(err)})
        return

    data = await async_reload_data(hass)
    changed = data.get("manual_exclusions_yaml", "") != yaml_text
    generated_file = None
    if changed:
        data["manual_exclusions_yaml"] = yaml_text
        data["pending_restart"] = True
        await async_save_data(hass, data)
        generated_file = await async_write_yaml(hass)

    connection.send_result(
        msg["id"],
        {"ok": True, "generated_file": generated_file, "restart_required": changed, "changed": changed},
    )


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command(
    {
        vol.Required("type"): "gui_recorder/remove_exclusion",
        vol.Required("entity_id"): str,
    }
)
async def ws_remove_exclusion(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    _, changed = await _set_entity_recorded_state(hass, msg["entity_id"], True)
    connection.send_result(
        msg["id"],
        {
            "ok": True,
            "removed": changed,
            "restart_required": changed,
        },
    )


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command({vol.Required("type"): "gui_recorder/remove_unmatched_exclusions"})
async def ws_remove_unmatched_exclusions(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    entity_registry = er.async_get(hass)
    # Same rule as _build_rows: an entity with a live state but no registry entry
    # still exists, so its exclusion is not unmatched and must not be removed here.
    known_entity_ids = {entry.entity_id for entry in entity_registry.entities.values()}
    known_entity_ids |= set(hass.states.async_entity_ids())
    data = await async_reload_data(hass)
    excluded = _normalize_excluded(data.get("excluded_entities", []))
    removed = sorted(entity_id for entity_id in excluded if entity_id not in known_entity_ids)

    if removed:
        remaining = [eid for eid in excluded if eid not in set(removed)]
        data["excluded_entities"] = _normalize_excluded(remaining)
        data["pending_restart"] = True
        await _save_and_reload(hass, data)

    connection.send_result(
        msg["id"],
        {
            "ok": True,
            "removed": removed,
            "restart_required": bool(removed),
        },
    )

@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command(
    {
        vol.Required("type"): "gui_recorder/set_global_options",
        vol.Required("purge_keep_days"): vol.All(vol.Coerce(int), vol.Range(min=1, max=3650)),
        vol.Required("auto_purge"): bool,
        vol.Required("auto_repack"): bool,
        vol.Required("commit_interval"): vol.All(vol.Coerce(int), vol.Range(min=0, max=86400)),
    }
)
async def ws_set_global_options(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    data = await async_reload_data(hass)
    data["purge_keep_days"] = int(msg["purge_keep_days"])
    data["auto_purge"] = bool(msg["auto_purge"])
    data["auto_repack"] = bool(msg["auto_repack"]) if bool(msg["auto_purge"]) else False
    data["commit_interval"] = int(msg["commit_interval"])
    data["pending_restart"] = True
    await async_save_data(hass, data)
    generated_file = await async_write_yaml(hass)
    await async_reload_data(hass)

    connection.send_result(
        msg["id"],
        {
            "ok": True,
            "generated_file": generated_file,
            "restart_required": True,
            "global_options": {
                "purge_keep_days": data["purge_keep_days"],
                "auto_purge": data["auto_purge"],
                "auto_repack": data["auto_repack"],
                "commit_interval": data["commit_interval"],
            },
        },
    )


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command(
    {
        vol.Required("type"): "gui_recorder/set_auto_update_data",
        vol.Required("auto_update_data"): bool,
    }
)
async def ws_set_auto_update_data(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    data = hass.data[DOMAIN]["data"]
    data["auto_update_data"] = bool(msg["auto_update_data"])
    await async_save_data(hass, data)
    connection.send_result(msg["id"], {"ok": True, "auto_update_data": data["auto_update_data"]})


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command(
    {
        vol.Required("type"): "gui_recorder/set_repack_after_manual_purge",
        vol.Required("repack_after_manual_purge"): bool,
    }
)
async def ws_set_repack_after_manual_purge(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    data = hass.data[DOMAIN]["data"]
    data["repack_after_manual_purge"] = bool(msg["repack_after_manual_purge"])
    await async_save_data(hass, data)
    connection.send_result(msg["id"], {"ok": True, "repack_after_manual_purge": data["repack_after_manual_purge"]})


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command(
    {
        vol.Required("type"): "gui_recorder/set_notify_on_purge_finish",
        vol.Required("notify_on_purge_finish"): bool,
    }
)
async def ws_set_notify_on_purge_finish(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    data = hass.data[DOMAIN]["data"]
    data["notify_on_purge_finish"] = bool(msg["notify_on_purge_finish"])
    await async_save_data(hass, data)
    connection.send_result(
        msg["id"],
        {"ok": True, "notify_on_purge_finish": data["notify_on_purge_finish"]},
    )


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command({vol.Required("type"): "gui_recorder/regenerate_yaml"})
async def ws_regenerate_yaml(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    generated_file = await async_write_yaml(hass)
    connection.send_result(msg["id"], {"ok": True, "generated_file": generated_file})


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command({vol.Required("type"): "gui_recorder/analyze_db"})
async def ws_analyze_db(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if purge_is_active(hass):
        connection.send_error(
            msg["id"],
            "purge_in_progress",
            "Database analysis is unavailable while a purge is active",
        )
        return
    stats = await async_analyze_db(hass)
    connection.send_result(msg["id"], {"ok": True, "stats": stats})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "gui_recorder/get_purge_progress"})
@callback
def ws_get_purge_progress(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    connection.send_result(
        msg["id"],
        {
            **purge_progress(hass),
            "history": purge_history(hass),
        },
    )


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command({vol.Required("type"): "gui_recorder/purge_all"})
async def ws_purge_all(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if purge_is_active(hass):
        connection.send_error(msg["id"], "purge_in_progress", "Another purge is already in progress")
        return
    data = hass.data[DOMAIN]["data"]
    keep_days = int(data.get("purge_keep_days", 10))
    repack = bool(data.get("repack_after_manual_purge", False))
    try:
        job = await async_start_purge_job(
            hass,
            kind="database",
            label=f"recorder data older than {keep_days} days",
            keep_days=keep_days,
            repack=repack,
        )
    except RuntimeError as err:
        connection.send_error(msg["id"], "purge_in_progress", str(err))
        return
    connection.send_result(msg["id"], {"ok": True, "job": job})


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command({vol.Required("type"): "gui_recorder/purge_excluded_entities"})
async def ws_purge_excluded_entities(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if purge_is_active(hass):
        connection.send_error(msg["id"], "purge_in_progress", "Another purge is already in progress")
        return
    data = hass.data[DOMAIN]["data"]
    entity_ids = sorted(set(data.get("excluded_entities", [])))
    repack = bool(data.get("repack_after_manual_purge", False)) and bool(entity_ids)
    try:
        job = await async_start_purge_job(
            hass,
            kind="excluded",
            label=f"{len(entity_ids)} excluded entities",
            entity_ids=entity_ids,
            repack=repack,
        )
    except RuntimeError as err:
        connection.send_error(msg["id"], "purge_in_progress", str(err))
        return
    connection.send_result(msg["id"], {"ok": True, "job": job})


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command({vol.Required("type"): "gui_recorder/purge_entity", vol.Required("entity_id"): str})
async def ws_purge_entity(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if purge_is_active(hass):
        connection.send_error(msg["id"], "purge_in_progress", "Another purge is already in progress")
        return
    try:
        job = await async_start_purge_job(
            hass,
            kind="entity",
            label=msg["entity_id"],
            entity_ids=[msg["entity_id"]],
        )
    except RuntimeError as err:
        connection.send_error(msg["id"], "purge_in_progress", str(err))
        return
    connection.send_result(msg["id"], {"ok": True, "job": job})


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command({vol.Required("type"): "gui_recorder/purge_device", vol.Required("device_id"): str})
async def ws_purge_device(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if purge_is_active(hass):
        connection.send_error(msg["id"], "purge_in_progress", "Another purge is already in progress")
        return
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    device = device_registry.async_get(msg["device_id"])
    device_label = msg["device_id"]
    if device:
        device_label = device.name_by_user or device.name or device_label
    entity_ids = sorted({entry.entity_id for entry in entity_registry.entities.values() if entry.device_id == msg["device_id"]})
    try:
        job = await async_start_purge_job(
            hass,
            kind="device",
            label=device_label,
            entity_ids=entity_ids,
        )
    except RuntimeError as err:
        connection.send_error(msg["id"], "purge_in_progress", str(err))
        return
    connection.send_result(msg["id"], {"ok": True, "job": job})


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command(
    {
        vol.Required("type"): "gui_recorder/purge_selected",
        vol.Required("entity_ids"): vol.All(
            [vol.All(str, vol.Length(min=3, max=255))],
            vol.Length(min=1, max=5000),
        ),
    }
)
async def ws_purge_selected(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Purge a UI-selected set of entities as one tracked recorder job."""
    if purge_is_active(hass):
        connection.send_error(
            msg["id"], "purge_in_progress", "Another purge is already in progress"
        )
        return

    entity_ids = sorted(
        {
            normalized
            for entity_id in msg["entity_ids"]
            if (normalized := _normalize_entity_id(entity_id))
        }
    )
    if not entity_ids:
        connection.send_error(msg["id"], "empty_selection", "No entities selected")
        return

    try:
        job = await async_start_purge_job(
            hass,
            kind="selected",
            label=f"{len(entity_ids)} selected entities",
            entity_ids=entity_ids,
        )
    except RuntimeError as err:
        connection.send_error(msg["id"], "purge_in_progress", str(err))
        return
    connection.send_result(msg["id"], {"ok": True, "job": job})




@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command({vol.Required("type"): "gui_recorder/restart_homeassistant"})
async def ws_restart_homeassistant(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if purge_is_active(hass):
        connection.send_error(
            msg["id"],
            "purge_in_progress",
            "Wait for the active purge to finish before restarting Home Assistant",
        )
        return
    await hass.services.async_call("homeassistant", "restart", {}, blocking=False)
    connection.send_result(msg["id"], {"ok": True, "restart_requested": True})


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command({vol.Required("type"): "gui_recorder/get_migration_status"})
async def ws_get_migration_status(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    status = await async_detect_sync_status(hass)
    hass.data[DOMAIN]["migration"] = status
    connection.send_result(msg["id"], status)


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command({vol.Required("type"): "gui_recorder/import_legacy"})
async def ws_import_legacy(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    result = await async_import_legacy(hass)
    if result.get("ok"):
        data = hass.data[DOMAIN]["data"]
        await async_save_data(hass, data)
        await async_write_yaml(hass)
        hass.data[DOMAIN]["migration"] = await async_detect_sync_status(hass)
    connection.send_result(msg["id"], result)


_IMPORT_REQUIRED_ERROR = (
    "Import the detected configuration first (Step 1). Disabling the legacy recorder "
    "block without importing would replace your settings - including a custom db_url - "
    "with defaults, and Home Assistant would start a new empty database."
)


async def _async_import_pending(hass: HomeAssistant) -> bool:
    """True if a legacy recorder config exists but hasn't been imported yet.

    Running steps 2/3 in that state silently replaces the user's recorder config
    with GUI Recorder's defaults, so both are blocked until Step 1 has run.
    """
    status = await async_detect_sync_status(hass)
    return bool(status.get("legacy_detected")) and not status.get("legacy_imported_at")


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command({vol.Required("type"): "gui_recorder/disable_legacy"})
async def ws_disable_legacy(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if await _async_import_pending(hass):
        connection.send_result(msg["id"], {"ok": False, "error": _IMPORT_REQUIRED_ERROR})
        return
    result = await async_disable_legacy(hass)
    if result.get("ok"):
        data = await async_reload_data(hass)
        data["pending_restart"] = True
        await async_save_data(hass, data)
    hass.data[DOMAIN]["migration"] = await async_detect_sync_status(hass)
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command({vol.Required("type"): "gui_recorder/enable_gui"})
async def ws_enable_gui(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if await _async_import_pending(hass):
        connection.send_result(msg["id"], {"ok": False, "error": _IMPORT_REQUIRED_ERROR})
        return
    result = await async_ensure_gui_enabled(hass)
    if result.get("ok"):
        await async_write_yaml(hass)
        data = await async_reload_data(hass)
        data["pending_restart"] = True
        await async_save_data(hass, data)
    hass.data[DOMAIN]["migration"] = await async_detect_sync_status(hass)
    connection.send_result(msg["id"], result)


async def async_setup_websocket_api(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_get_tree)
    websocket_api.async_register_command(hass, ws_set_entity)
    websocket_api.async_register_command(hass, ws_set_device)
    websocket_api.async_register_command(hass, ws_set_entities_bulk)
    websocket_api.async_register_command(hass, ws_set_manual_exclusions)
    websocket_api.async_register_command(hass, ws_remove_exclusion)
    websocket_api.async_register_command(hass, ws_remove_unmatched_exclusions)
    websocket_api.async_register_command(hass, ws_set_global_options)
    websocket_api.async_register_command(hass, ws_set_auto_update_data)
    websocket_api.async_register_command(hass, ws_set_repack_after_manual_purge)
    websocket_api.async_register_command(hass, ws_set_notify_on_purge_finish)
    websocket_api.async_register_command(hass, ws_regenerate_yaml)
    websocket_api.async_register_command(hass, ws_analyze_db)
    websocket_api.async_register_command(hass, ws_get_purge_progress)
    websocket_api.async_register_command(hass, ws_purge_all)
    websocket_api.async_register_command(hass, ws_purge_excluded_entities)
    websocket_api.async_register_command(hass, ws_purge_entity)
    websocket_api.async_register_command(hass, ws_purge_device)
    websocket_api.async_register_command(hass, ws_purge_selected)
    websocket_api.async_register_command(hass, ws_restart_homeassistant)
    websocket_api.async_register_command(hass, ws_get_migration_status)
    websocket_api.async_register_command(hass, ws_import_legacy)
    websocket_api.async_register_command(hass, ws_disable_legacy)
    websocket_api.async_register_command(hass, ws_enable_gui)
