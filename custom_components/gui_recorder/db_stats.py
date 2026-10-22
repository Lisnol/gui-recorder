from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import re

from sqlalchemy import func, select, text

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .storage import async_update_data

SUPPORTED_RECORDER_BACKENDS = {"sqlite", "mariadb"}


def _get_recorder_instance(hass: HomeAssistant):
    """Return the recorder instance, or None if recorder is not ready."""
    try:
        from homeassistant.components.recorder import get_instance

        return get_instance(hass)
    except Exception:  # noqa: BLE001 - recorder not loaded yet, etc.
        return None


def _get_recorder_db_url(hass: HomeAssistant) -> str | None:
    """Return the configured recorder db_url, or None if recorder isn't ready."""
    instance = _get_recorder_instance(hass)
    return getattr(instance, "db_url", None) if instance is not None else None


def get_recorder_backend(hass: HomeAssistant) -> str | None:
    """Return sqlite, mariadb, unsupported, or None while recorder starts."""
    db_url = _get_recorder_db_url(hass)
    if not db_url:
        return None

    scheme = db_url.split(":", 1)[0].lower()
    if scheme == "sqlite":
        return "sqlite"
    # Home Assistant's documented MariaDB URL uses the mysql or mysql+pymysql
    # SQLAlchemy dialect. The mariadb dialect is accepted defensively too.
    if scheme == "mysql" or scheme.startswith("mysql+") or scheme == "mariadb" or scheme.startswith("mariadb+"):
        return "mariadb"
    return "unsupported"


def is_supported_recorder(hass: HomeAssistant) -> bool | None:
    """Whether the current recorder backend is supported, or None if unknown."""
    backend = get_recorder_backend(hass)
    if backend is None:
        return None
    return backend in SUPPORTED_RECORDER_BACKENDS


def _resolve_sqlite_db_path(hass: HomeAssistant) -> Path:
    """Return the real on-disk SQLite path from recorder.db_url, falling back to default."""
    db_url = _get_recorder_db_url(hass)
    if db_url and db_url.lower().startswith("sqlite://"):
        # SQLAlchemy URL formats supported:
        #   sqlite:///relative/path.db
        #   sqlite:////absolute/path.db   (the 4th slash is the root)
        #   sqlite://         -> in-memory (treat as default file)
        _, _, raw = db_url.partition("://")
        if "?" in raw:
            raw, _, _ = raw.partition("?")
        raw = raw.lstrip("/")
        if raw:
            if db_url.startswith("sqlite:////"):
                return Path("/" + raw)
            return Path(hass.config.path(raw))
    return Path(hass.config.path("home-assistant_v2.db"))


def _sqlite_total_size_bytes(db_path: Path) -> int:
    """Sum the SQLite database and its WAL/SHM sidecar files."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix) if suffix else db_path
        try:
            total += sidecar.stat().st_size
        except (FileNotFoundError, OSError):
            pass
    return total


def _mariadb_size_breakdown(session) -> dict[str, int | None]:
    """Return MariaDB/MySQL data, index and reclaimable allocation estimates."""
    try:
        row = session.execute(
            text(
                "SELECT COALESCE(SUM(DATA_LENGTH), 0), "
                "COALESCE(SUM(INDEX_LENGTH), 0), COALESCE(SUM(DATA_FREE), 0) "
                "FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
            )
        ).one()
        data_bytes = int(row[0] or 0)
        index_bytes = int(row[1] or 0)
        return {
            "db_size_bytes": data_bytes + index_bytes,
            "db_data_bytes": data_bytes,
            "db_index_bytes": index_bytes,
            # DATA_FREE is an engine-reported allocation estimate, not a promise
            # that this many filesystem bytes will be reclaimed by a repack.
            "db_free_bytes": int(row[2] or 0),
        }
    except Exception:  # noqa: BLE001 - size is optional; entity counts still work
        return {
            "db_size_bytes": None,
            "db_data_bytes": None,
            "db_index_bytes": None,
            "db_free_bytes": None,
        }


def _database_health_issues(
    product: str,
    version: str,
    character_set: str | None,
    default_engine: str | None,
    non_innodb_tables: int | None,
) -> list[str]:
    """Return actionable Recorder database health warnings."""
    issues: list[str] = []
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version or "")
    parsed = tuple(int(part) for part in match.groups()) if match else None

    if product == "MariaDB" and parsed:
        major, minor, patch = parsed
        fixed_patches = {5: 17, 6: 9, 7: 5, 8: 4}
        affected = (major, minor) in {(10, 3), (10, 4)} or (
            major == 10
            and minor in fixed_patches
            and patch < fixed_patches[minor]
        )
        if (major, minor) < (10, 3):
            issues.append("MariaDB is older than Home Assistant's supported minimum 10.3.")
        elif affected:
            issues.append(
                "This MariaDB version is in a range with a known Home Assistant "
                "history and purge performance regression."
            )
    elif product == "MySQL" and parsed and parsed < (8, 0, 0):
        issues.append("MySQL is older than Home Assistant's supported minimum 8.0.")

    if character_set and character_set.lower() != "utf8mb4":
        issues.append(
            f"Database character set is {character_set}; Home Assistant recommends utf8mb4."
        )
    if default_engine and default_engine.lower() != "innodb":
        issues.append(
            f"Default storage engine is {default_engine}; Recorder requires InnoDB."
        )
    if non_innodb_tables:
        issues.append(
            f"{non_innodb_tables} Recorder database table(s) are not using InnoDB."
        )
    return issues


def _mariadb_health(session) -> dict:
    """Inspect MariaDB/MySQL settings relevant to Home Assistant Recorder."""
    empty = {
        "db_product": None,
        "db_server_version": None,
        "db_character_set": None,
        "db_collation": None,
        "db_default_engine": None,
        "db_non_innodb_tables": None,
        "db_health_status": "unavailable",
        "db_health_issues": [],
        "db_health_error": None,
    }
    try:
        row = session.execute(
            text(
                "SELECT VERSION(), @@character_set_database, "
                "@@collation_database, @@default_storage_engine, "
                "(SELECT COUNT(*) FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE' "
                "AND ENGINE <> 'InnoDB')"
            )
        ).one()
        version = str(row[0] or "")
        product = "MariaDB" if "mariadb" in version.lower() else "MySQL"
        character_set = str(row[1]) if row[1] is not None else None
        collation = str(row[2]) if row[2] is not None else None
        default_engine = str(row[3]) if row[3] is not None else None
        non_innodb = int(row[4] or 0)
        issues = _database_health_issues(
            product, version, character_set, default_engine, non_innodb
        )
        return {
            **empty,
            "db_product": product,
            "db_server_version": version,
            "db_character_set": character_set,
            "db_collation": collation,
            "db_default_engine": default_engine,
            "db_non_innodb_tables": non_innodb,
            "db_health_status": "warning" if issues else "ok",
            "db_health_issues": issues,
        }
    except Exception as err:  # noqa: BLE001 - health metadata is optional
        return {**empty, "db_health_error": str(err)}


def _analyze_recorder_db(hass: HomeAssistant, backend: str) -> dict:
    """Analyze recorder data on Home Assistant's dedicated database executor."""
    from homeassistant.components.recorder.db_schema import States, StatesMeta

    instance = _get_recorder_instance(hass)
    if instance is None:
        raise RuntimeError("Recorder is not ready")

    with instance.get_session() as session:
        rows = session.execute(
            select(StatesMeta.entity_id, func.count(States.state_id))
            .join(States, States.metadata_id == StatesMeta.metadata_id)
            .group_by(StatesMeta.entity_id)
        ).all()

        size_info = {
            "db_size_bytes": None,
            "db_data_bytes": None,
            "db_index_bytes": None,
            "db_free_bytes": None,
        }
        if backend == "mariadb":
            size_info = {
                **_mariadb_size_breakdown(session),
                **_mariadb_health(session),
            }

    entity_counts = {entity_id: int(count) for entity_id, count in rows if entity_id}
    if backend == "sqlite":
        db_path = _resolve_sqlite_db_path(hass)
        database = str(db_path)
        size_info["db_size_bytes"] = _sqlite_total_size_bytes(db_path)
        size_info["db_data_bytes"] = size_info["db_size_bytes"]
    else:
        engine = getattr(instance, "engine", None)
        url = getattr(engine, "url", None)
        database_name = getattr(url, "database", None) or "recorder"
        host = getattr(url, "host", None)
        database = f"{database_name} @ {host}" if host else str(database_name)

    generated_at = datetime.now(timezone.utc).isoformat()
    return {
        "entity_counts": entity_counts,
        "generated_at": generated_at,
        "analysis_attempted_at": generated_at,
        "backend": backend,
        "database": database,
        # Keep db_path for compatibility with stored data from older versions.
        "db_path": database,
        **size_info,
        "total_rows": sum(entity_counts.values()),
        "error": None,
    }


def _count_entity_rows_before(
    hass: HomeAssistant, entity_ids: tuple[str, ...], before_ts: float
) -> int:
    """Count target state rows older than a purge request.

    Home Assistant purges entity history in bounded batches and requeues the
    task until it is complete.  Counting only rows older than the request
    timestamp lets GUI Recorder detect completion without being kept alive by
    new states recorded while the purge is running.
    """
    from homeassistant.components.recorder.db_schema import States, StatesMeta

    instance = _get_recorder_instance(hass)
    if instance is None:
        raise RuntimeError("Recorder is not ready")

    normalized = tuple(sorted({entity_id for entity_id in entity_ids if entity_id}))
    if not normalized:
        return 0

    # Leave one bind variable for before_ts. This also keeps the query valid
    # for SQLite installations whose bind-variable ceiling is much lower than
    # MariaDB's.
    chunk_size = max(1, int(getattr(instance, "max_bind_vars", 999)) - 1)
    total = 0
    with instance.get_session() as session:
        for offset in range(0, len(normalized), chunk_size):
            entity_chunk = normalized[offset : offset + chunk_size]
            total += int(
                session.execute(
                    select(func.count(States.state_id))
                    .join(StatesMeta, States.metadata_id == StatesMeta.metadata_id)
                    .where(
                        StatesMeta.entity_id.in_(entity_chunk),
                        States.last_updated_ts < before_ts,
                    )
                ).scalar_one()
                or 0
            )
    return total


async def async_count_entity_rows_before(
    hass: HomeAssistant, entity_ids: list[str] | tuple[str, ...], before_ts: float
) -> int:
    """Count pre-purge target rows on Home Assistant's DB executor."""
    instance = _get_recorder_instance(hass)
    if instance is None:
        raise RuntimeError("Recorder is not ready")
    return await instance.async_add_executor_job(
        _count_entity_rows_before, hass, tuple(entity_ids), before_ts
    )


async def _async_analyze_db_locked(hass: HomeAssistant) -> dict:
    backend = get_recorder_backend(hass)
    database = str(_resolve_sqlite_db_path(hass)) if backend == "sqlite" else "MariaDB"
    previous = dict(hass.data[DOMAIN]["data"].get("stats") or {})

    try:
        if backend not in SUPPORTED_RECORDER_BACKENDS:
            raise RuntimeError(f"Unsupported recorder backend: {backend or 'unknown'}")
        instance = _get_recorder_instance(hass)
        if instance is None:
            raise RuntimeError("Recorder is not ready")
        # Database work must use the recorder executor, not Home Assistant's
        # generic executor. This also reuses HA's existing pooled connection.
        stats = await instance.async_add_executor_job(_analyze_recorder_db, hass, backend)
    except Exception as err:  # noqa: BLE001
        stats = {
            "entity_counts": {},
            "generated_at": None,
            "backend": backend,
            "database": database,
            "db_path": database,
            "db_size_bytes": None,
            "db_data_bytes": None,
            "db_index_bytes": None,
            "db_free_bytes": None,
            "db_product": None,
            "db_server_version": None,
            "db_character_set": None,
            "db_collation": None,
            "db_default_engine": None,
            "db_non_innodb_tables": None,
            "db_health_status": None,
            "db_health_issues": [],
            "db_health_error": None,
            "total_rows": 0,
            **previous,
            # Keep the last successful counts and sizes. Replacing them with
            # zero on a transient DB error makes purge previews misleading and
            # makes every entity appear to have no history.
            "backend": backend or previous.get("backend"),
            "database": previous.get("database") or database,
            "db_path": previous.get("db_path") or database,
            "analysis_attempted_at": datetime.now(timezone.utc).isoformat(),
            "error": str(err),
        }

    await async_update_data(hass, lambda data: data.__setitem__("stats", stats))
    return stats


async def async_analyze_db(hass: HomeAssistant) -> dict:
    """Run one expensive analysis and coalesce concurrent callers."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    lock = domain_data.get("analysis_lock")
    if lock is None:
        lock = asyncio.Lock()
        domain_data["analysis_lock"] = lock
    if lock.locked():
        # A second panel/reconnect should wait for and reuse the result rather
        # than queue another full GROUP BY scan immediately behind the first.
        async with lock:
            return dict(hass.data[DOMAIN]["data"].get("stats") or {})

    operation_lock = domain_data.get("db_operation_lock")
    if operation_lock is None:
        operation_lock = asyncio.Lock()
        domain_data["db_operation_lock"] = operation_lock
    async with lock, operation_lock:
        domain_data["analysis_in_progress"] = True
        try:
            return await _async_analyze_db_locked(hass)
        finally:
            domain_data["analysis_in_progress"] = False
