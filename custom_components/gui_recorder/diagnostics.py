from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, INTEGRATION_VERSION


_JOB_FIELDS = (
    "kind",
    "status",
    "entity_count",
    "total_rows",
    "remaining_rows",
    "deleted_rows",
    "percent",
    "determinate",
    "repack",
    "analyze_after",
    "stats_refreshed",
    "started_at",
    "finished_at",
    "updated_at",
    "last_progress_at",
    "checkpointed_at",
    "db_size_before",
    "db_size_after",
    "reclaimed_bytes",
)


def _redact_job(job: Any) -> dict[str, Any] | None:
    if not isinstance(job, dict):
        return None
    # job_id and label can contain correlating or entity/device information.
    return {
        **{key: job.get(key) for key in _JOB_FIELDS},
        "has_warning": bool(job.get("warning")),
        "has_error": bool(job.get("error")),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    _entry: ConfigEntry,
) -> dict[str, Any]:
    """Return useful diagnostics without paths, hosts, secrets or entity IDs."""
    domain_data = hass.data.get(DOMAIN, {})
    data = domain_data.get("data", {})
    stats = data.get("stats") or {}
    migration = domain_data.get("migration") or {}
    active_progress = domain_data.get("purge_progress")

    return {
        "integration_version": INTEGRATION_VERSION,
        "loaded": bool(domain_data.get("loaded")),
        "configuration": {
            "excluded_entity_count": len(data.get("excluded_entities") or []),
            "rescued_entity_count": len(data.get("rescued_entities") or []),
            "has_manual_filters": bool(
                str(data.get("manual_exclusions_yaml") or "").strip()
            ),
            "uses_db_url_secret": bool(data.get("db_url_secret")),
            "has_custom_sqlite_url": bool(data.get("db_url")),
            "pending_restart": bool(data.get("pending_restart")),
            "purge_keep_days": data.get("purge_keep_days"),
            "auto_purge": data.get("auto_purge"),
            "auto_repack": data.get("auto_repack"),
            "commit_interval": data.get("commit_interval"),
            "auto_update_data": data.get("auto_update_data"),
            "repack_after_manual_purge": data.get(
                "repack_after_manual_purge"
            ),
            "notify_on_purge_finish": data.get("notify_on_purge_finish"),
        },
        "database": {
            "generated_at": stats.get("generated_at"),
            "analysis_attempted_at": stats.get("analysis_attempted_at"),
            "backend": stats.get("backend"),
            "entity_count": len(stats.get("entity_counts") or {}),
            "total_rows": stats.get("total_rows"),
            "db_size_bytes": stats.get("db_size_bytes"),
            "db_data_bytes": stats.get("db_data_bytes"),
            "db_index_bytes": stats.get("db_index_bytes"),
            "db_free_bytes": stats.get("db_free_bytes"),
            "db_product": stats.get("db_product"),
            "db_server_version": stats.get("db_server_version"),
            "db_character_set": stats.get("db_character_set"),
            "db_collation": stats.get("db_collation"),
            "db_default_engine": stats.get("db_default_engine"),
            "db_non_innodb_tables": stats.get("db_non_innodb_tables"),
            "db_health_status": stats.get("db_health_status"),
            "db_health_issues": list(stats.get("db_health_issues") or []),
            "has_db_health_error": bool(stats.get("db_health_error")),
            "has_analysis_error": bool(stats.get("error")),
        },
        "migration": {
            "gui_include_active": migration.get("gui_include_active"),
            "gui_ready": migration.get("gui_ready"),
            "legacy_active": migration.get("legacy_active"),
            "legacy_detected": migration.get("legacy_detected"),
            "legacy_source_type": migration.get("legacy_source_type"),
            "legacy_parse_ok": migration.get("legacy_parse_ok"),
            "legacy_block_present": migration.get("legacy_block_present"),
            "recorder_yaml_exists": migration.get("recorder_yaml_exists"),
        },
        "active_maintenance": _redact_job(active_progress),
        "maintenance_history": [
            redacted
            for job in list(data.get("purge_history") or [])[:20]
            if (redacted := _redact_job(job)) is not None
        ],
    }
