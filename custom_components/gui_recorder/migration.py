from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import shutil
from typing import Any

import yaml
from sqlalchemy.engine import make_url

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .file_utils import atomic_write_utf8
from .storage import async_update_data


@dataclass
class BlockInfo:
    start: int
    end: int
    key: str
    line: str
    lines: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


@dataclass(frozen=True)
class SecretReference:
    """A Home Assistant !secret reference, never the resolved secret value."""

    name: str


class _RecorderConfigLoader(yaml.SafeLoader):
    """Safe YAML loader that preserves Home Assistant secret references."""


def _construct_secret(loader: _RecorderConfigLoader, node: yaml.Node) -> SecretReference:
    return SecretReference(loader.construct_scalar(node).strip())


_RecorderConfigLoader.add_constructor("!secret", _construct_secret)

_SUPPORTED_RECORDER_KEYS = {
    "auto_purge",
    "auto_repack",
    "purge_keep_days",
    "commit_interval",
    "db_url",
    "exclude",
    "include",
}
_SUPPORTED_FILTER_KEYS = {
    "exclude": {"entities", "domains", "entity_globs", "event_types"},
    "include": {"entities", "domains", "entity_globs"},
}


def _config_path(hass: HomeAssistant) -> Path:
    return Path(hass.config.path("configuration.yaml"))


def _safe_include_path(hass: HomeAssistant, include_target: str) -> Path:
    """Resolve a Recorder include without allowing writes outside config."""
    config_root = _config_path(hass).parent.resolve()
    raw_path = Path(_normalize_include_target(include_target))
    candidate = (raw_path if raw_path.is_absolute() else config_root / raw_path).resolve()
    try:
        candidate.relative_to(config_root)
    except ValueError as err:
        raise ValueError(
            "The included recorder file must be inside the Home Assistant "
            "configuration directory."
        ) from err
    return candidate


def _migration_lock(hass: HomeAssistant) -> asyncio.Lock:
    domain_data = hass.data.setdefault(DOMAIN, {})
    lock = domain_data.get("migration_lock")
    if lock is None:
        lock = asyncio.Lock()
        domain_data["migration_lock"] = lock
    return lock


def _timestamped_sibling(path: Path, marker: str) -> Path:
    """Return an unused, timestamped sibling path."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    candidate = path.with_name(f"{path.name}.{marker}_{stamp}")
    counter = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.{marker}_{stamp}_{counter}")
        counter += 1
    return candidate


def _make_backup(path: Path) -> str:
    backup = _timestamped_sibling(path, "gui_recorder_backup")
    shutil.copy2(path, backup)
    return str(backup)


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _find_top_level_block(lines: list[str], key: str) -> BlockInfo | None:
    start = None
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if line.startswith((" ", "\t")):
            continue
        if stripped.startswith("#"):
            continue
        if re.match(rf"^{re.escape(key)}\s*:", stripped):
            start = idx
            break

    if start is None:
        return None

    end = len(lines)
    for idx in range(start + 1, len(lines)):
        line = lines[idx]
        stripped = line.strip()
        if not stripped:
            continue
        if line.startswith((" ", "\t")):
            continue
        if line.lstrip().startswith("#"):
            continue
        end = idx
        break

    return BlockInfo(start=start, end=end, key=key, line=lines[start], lines=lines[start:end])


def _normalize_include_target(value: str | None) -> str:
    if value is None:
        return ""
    target = str(value).strip().strip('"\'')
    target = target.replace("\\", "/")
    while target.startswith("./"):
        target = target[2:]
    return target.strip()


def _extract_include_target(line: str) -> str | None:
    match = re.match(r"^recorder\s*:\s*!include\s+(.+?)\s*$", line.strip())
    if not match:
        return None
    return _normalize_include_target(match.group(1))


def _is_gui_recorder_include_target(value: str | None) -> bool:
    return _normalize_include_target(value) == "gui_recorder.yaml"


def _detect_active_gui_recorder_include(lines: list[str]) -> bool:
    for line in lines:
        if line.startswith((" ", "\t")):
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _is_gui_recorder_include_target(_extract_include_target(stripped)):
            return True
    return False


def _yaml_error_location(err: yaml.YAMLError) -> str:
    mark = getattr(err, "problem_mark", None)
    if mark is None:
        return ""
    return f" at line {mark.line + 1}, column {mark.column + 1}"


def _parse_yaml_text(text: str, description: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        loaded = yaml.load(text, Loader=_RecorderConfigLoader)
    except yaml.YAMLError as err:
        return None, f"{description} contains invalid YAML{_yaml_error_location(err)}."
    except Exception:  # noqa: BLE001 - never expose parser details or config contents
        return None, f"{description} could not be parsed safely."

    if not isinstance(loaded, dict):
        return None, f"{description} must be a YAML mapping."
    return loaded, None


def _read_recorder_yaml(path: Path, description: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, f"{description} could not be read."
    return _parse_yaml_text(text, description)


def _recorder_structure_error(config: dict[str, Any]) -> str | None:
    """Reject settings that GUI Recorder cannot round-trip without loss."""
    unknown_top = sorted(set(config) - _SUPPORTED_RECORDER_KEYS)
    if unknown_top:
        return f"contains unsupported recorder settings: {', '.join(unknown_top)}."

    for key in ("auto_purge", "auto_repack"):
        if key in config and not isinstance(config[key], bool):
            return f"'{key}' must be true or false."
    for key, minimum in (("purge_keep_days", 1), ("commit_interval", 0)):
        value = config.get(key)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < minimum
        ):
            return f"'{key}' must be an integer of at least {minimum}."
    db_url = config.get("db_url")
    if db_url is not None and not isinstance(db_url, (str, SecretReference)):
        return "'db_url' must be a string or !secret reference."
    if isinstance(db_url, SecretReference) and not db_url.name:
        return "'db_url' must reference a non-empty secret name."

    for section_name, allowed in _SUPPORTED_FILTER_KEYS.items():
        section = config.get(section_name)
        if section is None:
            continue
        if not isinstance(section, dict):
            return f"'{section_name}' must be a mapping."
        unknown = sorted(set(section) - allowed)
        if unknown:
            return (
                f"contains unsupported {section_name} settings: "
                f"{', '.join(unknown)}."
            )
        for key, values in section.items():
            if not isinstance(values, list) or not all(
                isinstance(value, str) for value in values
            ):
                return f"'{section_name}.{key}' must be a list of strings."
    return None


def _safe_db_url_display(value: Any) -> str | None:
    """Describe a recorder URL without exposing credentials."""
    if isinstance(value, SecretReference):
        return f"!secret {value.name}"
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        url = make_url(value.strip())
        backend = "MariaDB" if url.drivername.startswith(("mysql", "mariadb")) else url.drivername
        location = url.database or "database"
        if url.host:
            location = f"{location} @ {url.host}"
        return f"{backend}: {location}"
    except Exception:  # noqa: BLE001
        return "configured (credentials hidden)"


def _extract_summary_from_recorder_config(recorder_cfg: dict[str, Any] | None) -> dict[str, Any]:
    recorder_cfg = recorder_cfg or {}
    exclude = recorder_cfg.get("exclude") or {}
    include = recorder_cfg.get("include") or {}
    db_url = recorder_cfg.get("db_url")
    db_url_secret = db_url.name if isinstance(db_url, SecretReference) else None
    db_url_literal = db_url.strip() if isinstance(db_url, str) and db_url.strip() else None
    db_url_is_sqlite = bool(db_url_literal and db_url_literal.lower().startswith("sqlite"))
    return {
        "auto_purge": recorder_cfg.get("auto_purge"),
        "auto_repack": recorder_cfg.get("auto_repack"),
        "purge_keep_days": recorder_cfg.get("purge_keep_days"),
        "commit_interval": recorder_cfg.get("commit_interval"),
        "db_url": _safe_db_url_display(db_url),
        "exclude_entities_count": len(exclude.get("entities") or []),
        "exclude_domains_count": len(exclude.get("domains") or []),
        "exclude_globs_count": len(exclude.get("entity_globs") or []),
        "exclude_event_types_count": len(exclude.get("event_types") or []),
        "include_entities_count": len(include.get("entities") or []),
        "include_domains_count": len(include.get("domains") or []),
        "include_globs_count": len(include.get("entity_globs") or []),
        "supported_import": {
            "auto_purge": recorder_cfg.get("auto_purge"),
            "auto_repack": recorder_cfg.get("auto_repack"),
            "purge_keep_days": recorder_cfg.get("purge_keep_days"),
            "commit_interval": recorder_cfg.get("commit_interval"),
            "db_url": db_url_literal if db_url_is_sqlite else None,
            "db_url_secret": db_url_secret,
            "db_url_requires_secret": bool(db_url_literal and not db_url_is_sqlite),
            "excluded_entities": list(exclude.get("entities") or []),
            "included_entities": list(include.get("entities") or []),
        },
        # Filters GUI Recorder doesn't manage per-entity but can still carry over
        # verbatim into the manual exclusions block (exclude/include entities are
        # explicitly excluded - those stay exclusive to the entity toggles).
        "unsupported_filters": {
            "exclude": {
                key: list(exclude.get(key) or [])
                for key in ("domains", "entity_globs", "event_types")
                if exclude.get(key)
            },
            "include": {
                key: list(include.get(key) or [])
                for key in ("domains", "entity_globs")
                if include.get(key)
            },
        },
    }


def _detect_sync_status(hass: HomeAssistant) -> dict[str, Any]:
    config_path = _config_path(hass)
    lines = _read_lines(config_path)
    recorder_block = _find_top_level_block(lines, "recorder")

    gui_include_active = _detect_active_gui_recorder_include(lines)

    legacy_source_path: str | None = None
    legacy_source_type: str | None = None
    legacy_config: dict[str, Any] | None = None
    legacy_active = False
    legacy_message = None
    legacy_parse_ok: bool | None = None
    legacy_parse_error: str | None = None

    if recorder_block:
        stripped = recorder_block.line.strip()
        include_target = _extract_include_target(stripped)
        if include_target is not None:
            if not _is_gui_recorder_include_target(include_target):
                legacy_active = True
                legacy_source_type = "include"
                legacy_source_path = include_target
                try:
                    included_path = _safe_include_path(hass, include_target)
                except ValueError as err:
                    legacy_parse_ok = False
                    legacy_parse_error = str(err)
                else:
                    if included_path.exists():
                        legacy_config, legacy_parse_error = _read_recorder_yaml(
                            included_path,
                            f"Included recorder file '{include_target}'",
                        )
                        if legacy_config is not None and (
                            structure_error := _recorder_structure_error(legacy_config)
                        ):
                            legacy_parse_error = (
                                f"Included recorder file '{include_target}' "
                                f"{structure_error}"
                            )
                            legacy_config = None
                        legacy_parse_ok = legacy_parse_error is None
                    else:
                        legacy_parse_ok = False
                        legacy_parse_error = (
                            "The included recorder file does not exist."
                        )
        else:
            legacy_active = True
            legacy_source_type = "inline"
            legacy_source_path = str(config_path)
            loaded, legacy_parse_error = _parse_yaml_text(
                recorder_block.text,
                "The recorder block in configuration.yaml",
            )
            if legacy_parse_error is not None:
                legacy_parse_ok = False
            else:
                recorder_config = loaded.get("recorder") if loaded is not None else None
                if isinstance(recorder_config, dict):
                    if structure_error := _recorder_structure_error(recorder_config):
                        legacy_parse_ok = False
                        legacy_parse_error = (
                            "The recorder block in configuration.yaml "
                            f"{structure_error}"
                        )
                    else:
                        legacy_config = recorder_config
                        legacy_parse_ok = True
                else:
                    legacy_parse_ok = False
                    legacy_parse_error = "The recorder block in configuration.yaml must be a YAML mapping."

    recorder_yaml_path = Path(hass.config.path("recorder.yaml"))
    recorder_yaml_exists = recorder_yaml_path.exists()
    if recorder_yaml_exists and not legacy_source_type and not gui_include_active:
        legacy_source_type = "recorder_yaml"
        legacy_source_path = str(recorder_yaml_path)
        legacy_config, legacy_parse_error = _read_recorder_yaml(
            recorder_yaml_path,
            "recorder.yaml",
        )
        if legacy_config is not None and (
            structure_error := _recorder_structure_error(legacy_config)
        ):
            legacy_parse_error = f"recorder.yaml {structure_error}"
            legacy_config = None
        legacy_parse_ok = legacy_parse_error is None

    if legacy_parse_error:
        legacy_message = legacy_parse_error

    summary = _extract_summary_from_recorder_config(legacy_config)

    return {
        "config_path": str(config_path),
        "gui_include_active": gui_include_active,
        "gui_ready": gui_include_active and not legacy_active,
        "legacy_active": legacy_active,
        "legacy_detected": bool(legacy_source_type),
        "legacy_source_type": legacy_source_type,
        "legacy_source_path": legacy_source_path,
        "legacy_summary": summary,
        "legacy_message": legacy_message,
        "legacy_parse_ok": legacy_parse_ok,
        "legacy_parse_error": legacy_parse_error,
        "legacy_block_present": recorder_block is not None,
        "recorder_yaml_exists": recorder_yaml_exists,
    }


async def async_detect_sync_status(hass: HomeAssistant) -> dict[str, Any]:
    status = await hass.async_add_executor_job(_detect_sync_status, hass)
    data = hass.data.get(DOMAIN, {}).get("data", {})
    status["legacy_imported_at"] = data.get("legacy_imported_at")
    return status


def _comment_block(lines: list[str], block: BlockInfo) -> list[str]:
    new_lines = list(lines)
    for idx in range(block.start, block.end):
        line = new_lines[idx]
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        new_lines[idx] = (" " * indent) + "# " + line[indent:]
    return new_lines


def _disable_legacy_sync(hass: HomeAssistant) -> dict[str, Any]:
    config_path = _config_path(hass)
    lines = _read_lines(config_path)
    recorder_block = _find_top_level_block(lines, "recorder")
    if not recorder_block:
        return {"ok": False, "error": "No active recorder: block was found in configuration.yaml."}

    stripped = recorder_block.line.strip()
    include_target = _extract_include_target(stripped)
    if _is_gui_recorder_include_target(include_target):
        return {"ok": False, "error": "The active recorder config already points to gui_recorder.yaml."}

    included_path: Path | None = None
    if include_target:
        try:
            included_path = _safe_include_path(hass, include_target)
        except ValueError as err:
            return {"ok": False, "error": str(err)}

    backup = _make_backup(config_path)
    updated = _comment_block(lines, recorder_block)
    atomic_write_utf8(config_path, "\n".join(updated) + "\n", validate_yaml=True)

    renamed_include: str | None = None
    if included_path is not None:
        if included_path.exists():
            disabled_path = _timestamped_sibling(included_path, "gui_recorder_disabled")
            try:
                included_path.rename(disabled_path)
            except OSError:
                # Do not leave configuration.yaml with Recorder disabled when
                # the second half of the operation could not be completed.
                original = Path(backup).read_text(encoding="utf-8")
                atomic_write_utf8(config_path, original, validate_yaml=True)
                raise
            renamed_include = str(disabled_path)

    return {
        "ok": True,
        "backup_path": backup,
        "changed_file": str(config_path),
        "renamed_include": renamed_include,
    }


async def async_disable_legacy(hass: HomeAssistant) -> dict[str, Any]:
    async with _migration_lock(hass):
        return await hass.async_add_executor_job(_disable_legacy_sync, hass)


def _ensure_gui_sync(hass: HomeAssistant) -> dict[str, Any]:
    config_path = _config_path(hass)
    lines = _read_lines(config_path)
    recorder_block = _find_top_level_block(lines, "recorder")

    if recorder_block:
        stripped = recorder_block.line.strip()
        include_target = _extract_include_target(stripped)
        if not _is_gui_recorder_include_target(include_target):
            return {
                "ok": False,
                "error": "Another active recorder configuration is still enabled. Disable it before enabling gui_recorder.yaml.",
            }

    gui_include_active = _detect_active_gui_recorder_include(lines)
    backup = _make_backup(config_path)

    updated = list(lines)
    if not gui_include_active:
        if updated and updated[-1].strip():
            updated.append("")
        updated.append("recorder: !include gui_recorder.yaml")

    atomic_write_utf8(config_path, "\n".join(updated) + "\n", validate_yaml=True)
    return {"ok": True, "backup_path": backup, "changed_file": str(config_path)}


async def async_ensure_gui_enabled(hass: HomeAssistant) -> dict[str, Any]:
    async with _migration_lock(hass):
        return await hass.async_add_executor_job(_ensure_gui_sync, hass)


def _import_legacy_sync(hass: HomeAssistant) -> dict[str, Any]:
    status = _detect_sync_status(hass)
    if not status.get("legacy_detected"):
        return {"ok": False, "error": "No legacy recorder configuration was detected to import."}
    if status.get("legacy_parse_ok") is False:
        detail = status.get("legacy_parse_error") or "The detected recorder configuration could not be parsed safely."
        return {
            "ok": False,
            "error": f"The recorder configuration was not imported. {detail} Fix the YAML and try again.",
        }

    summary = status.get("legacy_summary") or {}
    updates, update_error, manual_block = _storage_updates_from_summary(
        summary,
        mark_legacy_imported=True,
        restore_include_entities=True,
    )
    if update_error:
        return {"ok": False, "error": update_error}

    assert updates is not None
    unsupported = {
        "exclude_domains_count": summary.get("exclude_domains_count", 0),
        "exclude_globs_count": summary.get("exclude_globs_count", 0),
        "exclude_event_types_count": summary.get("exclude_event_types_count", 0),
        "include_entities_count": summary.get("include_entities_count", 0),
        "include_domains_count": summary.get("include_domains_count", 0),
        "include_globs_count": summary.get("include_globs_count", 0),
    }

    return {
        "ok": True,
        "storage_updates": updates,
        "imported": {
            "excluded_entities": len(updates["excluded_entities"]),
            "purge_keep_days": updates.get("purge_keep_days"),
            "auto_purge": updates.get("auto_purge"),
            "auto_repack": updates.get("auto_repack"),
            "commit_interval": updates.get("commit_interval"),
            "manual_exclusions": bool(manual_block),
            "db_url": summary.get("db_url") if updates.get("db_url") or updates.get("db_url_secret") else None,
        },
        "unsupported": unsupported,
        "source": status.get("legacy_source_path"),
    }


async def async_import_legacy(hass: HomeAssistant) -> dict[str, Any]:
    from .yaml_writer import async_update_and_write_yaml

    async with _migration_lock(hass):
        result = await hass.async_add_executor_job(_import_legacy_sync, hass)
        if result.get("ok"):
            updates = result.pop("storage_updates", {})
            _data, _changed, generated_file = await async_update_and_write_yaml(
                hass, lambda data: data.update(updates)
            )
            result["generated_file"] = generated_file
        return result


def _storage_updates_from_summary(
    summary: dict[str, Any],
    *,
    mark_legacy_imported: bool,
    restore_include_entities: bool,
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any]]:
    """Build safe storage data from a parsed recorder configuration."""
    supported = summary.get("supported_import") or {}
    unsupported_filters = summary.get("unsupported_filters") or {}
    if supported.get("db_url_requires_secret"):
        return (
            None,
            (
                "For safety, move the MariaDB db_url to secrets.yaml and use "
                "'db_url: !secret recorder_db_url'. GUI Recorder preserves the "
                "secret reference but never stores database credentials."
            ),
            {},
        )

    included_entities = sorted(set(supported.get("included_entities") or []))
    has_domain_or_glob = any(
        section.get(key)
        for section in unsupported_filters.values()
        for key in ("domains", "entity_globs")
    )
    if restore_include_entities and included_entities and not has_domain_or_glob:
        return (
            None,
            (
                "The recorder configuration uses include.entities as an "
                "allow-list. GUI Recorder cannot represent that rule safely, "
                "so nothing was imported. Convert it to domain/glob rules or "
                "remove include.entities before trying again."
            ),
            {},
        )

    updates: dict[str, Any] = {
        "excluded_entities": sorted(set(supported.get("excluded_entities") or [])),
        # Clear the opposite URL representation during repeated imports or
        # managed-file recovery so stale values cannot survive a backend change.
        "db_url": None,
        "db_url_secret": None,
    }
    if restore_include_entities:
        updates["rescued_entities"] = included_entities
    if mark_legacy_imported:
        updates["legacy_imported_at"] = datetime.now().isoformat(timespec="seconds")

    for key in ("purge_keep_days", "commit_interval"):
        value = supported.get(key)
        if value is not None:
            try:
                updates[key] = int(value)
            except (TypeError, ValueError):
                pass
    for key in ("auto_purge", "auto_repack"):
        value = supported.get(key)
        if value is not None:
            updates[key] = bool(value)

    db_url = supported.get("db_url")
    if isinstance(db_url, str) and db_url.strip().lower().startswith("sqlite"):
        updates["db_url"] = db_url.strip()
    db_url_secret = supported.get("db_url_secret")
    if isinstance(db_url_secret, str) and db_url_secret.strip():
        updates["db_url_secret"] = db_url_secret.strip()

    manual_block = {
        section: keys for section, keys in unsupported_filters.items() if keys
    }
    manual_yaml = ""
    if manual_block:
        prefix = (
            "# Imported automatically from the legacy recorder configuration.\n"
            "# Review before relying on this - confirm it still matches your setup.\n"
            if mark_legacy_imported
            else ""
        )
        manual_yaml = prefix + yaml.safe_dump(
            manual_block, sort_keys=True, default_flow_style=False
        )
    updates["manual_exclusions_yaml"] = manual_yaml
    return updates, None, manual_block


def _recover_managed_yaml_sync(hass: HomeAssistant) -> dict[str, Any]:
    """Recover storage from the active generated YAML without exposing secrets."""
    path = Path(hass.config.path("gui_recorder.yaml"))
    if not path.exists():
        return {
            "ok": False,
            "error": "gui_recorder.yaml is active but the file does not exist.",
        }
    recorder_config, parse_error = _read_recorder_yaml(path, "gui_recorder.yaml")
    if parse_error:
        return {"ok": False, "error": parse_error}

    assert recorder_config is not None
    if structure_error := _recorder_structure_error(recorder_config):
        return {
            "ok": False,
            "error": f"gui_recorder.yaml {structure_error}",
        }

    include = recorder_config.get("include") or {}
    has_domain_or_glob = any(
        (recorder_config.get(section_name) or {}).get(key)
        for section_name in ("include", "exclude")
        for key in ("domains", "entity_globs")
    )
    if include.get("entities") and not has_domain_or_glob:
        return {
            "ok": False,
            "error": (
                "gui_recorder.yaml uses include.entities as an allow-list. "
                "GUI Recorder will not replace it automatically."
            ),
        }

    summary = _extract_summary_from_recorder_config(recorder_config)
    updates, update_error, _manual = _storage_updates_from_summary(
        summary,
        mark_legacy_imported=False,
        restore_include_entities=True,
    )
    if update_error:
        return {"ok": False, "error": update_error}
    return {"ok": True, "storage_updates": updates or {}}


async def async_recover_managed_yaml(hass: HomeAssistant) -> dict[str, Any]:
    """Recover missing integration storage from active gui_recorder.yaml."""
    result = await hass.async_add_executor_job(_recover_managed_yaml_sync, hass)
    if result.get("ok"):
        updates = result.pop("storage_updates", {})
        await async_update_data(hass, lambda data: data.update(updates))
    return result
