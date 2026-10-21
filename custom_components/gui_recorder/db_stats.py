from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select, text

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .storage import async_save_data

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


def _mariadb_size_bytes(session) -> int | None:
    """Return the current MariaDB/MySQL schema size when permitted."""
    try:
        value = session.execute(
            text(
                "SELECT COALESCE(SUM(DATA_LENGTH + INDEX_LENGTH), 0) "
                "FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
            )
        ).scalar_one()
        return int(value or 0)
    except Exception:  # noqa: BLE001 - size is optional; entity counts still work
        return None


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
            .order_by(func.count(States.state_id).desc())
        ).all()

        db_size_bytes: int | None = None
        if backend == "mariadb":
            db_size_bytes = _mariadb_size_bytes(session)

    entity_counts = {entity_id: int(count) for entity_id, count in rows if entity_id}
    if backend == "sqlite":
        db_path = _resolve_sqlite_db_path(hass)
        database = str(db_path)
        db_size_bytes = _sqlite_total_size_bytes(db_path)
    else:
        engine = getattr(instance, "engine", None)
        url = getattr(engine, "url", None)
        database_name = getattr(url, "database", None) or "recorder"
        host = getattr(url, "host", None)
        database = f"{database_name} @ {host}" if host else str(database_name)

    return {
        "entity_counts": entity_counts,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "backend": backend,
        "database": database,
        # Keep db_path for compatibility with stored data from older versions.
        "db_path": database,
        "db_size_bytes": db_size_bytes,
        "total_rows": sum(entity_counts.values()),
        "error": None,
    }


async def async_analyze_db(hass: HomeAssistant) -> dict:
    data = hass.data[DOMAIN]["data"]
    backend = get_recorder_backend(hass)
    database = str(_resolve_sqlite_db_path(hass)) if backend == "sqlite" else "MariaDB"

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
            "total_rows": 0,
            "error": str(err),
        }

    data["stats"] = stats
    await async_save_data(hass, data)
    return stats
