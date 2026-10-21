from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any
from uuid import uuid4

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .db_stats import async_analyze_db, async_count_entity_rows_before
from .storage import async_reload_data, async_save_data

_LOGGER = logging.getLogger(__name__)

_PURGE_POLL_INTERVAL = 0.5
_PURGE_TIMEOUT_SECONDS = 2 * 60 * 60
_PURGE_STALL_TIMEOUT_SECONDS = 10 * 60
_PURGE_HISTORY_LIMIT = 20
_BLOCK_API_WARNED = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utcnow().isoformat()


def _default_progress() -> dict[str, Any]:
    return {
        "job_id": None,
        "active": False,
        "status": "idle",
        "kind": None,
        "label": None,
        "entity_count": 0,
        "total_rows": 0,
        "remaining_rows": 0,
        "deleted_rows": 0,
        "percent": 0.0,
        "determinate": True,
        "repack": False,
        "analyze_after": True,
        "stats_refreshed": False,
        "started_at": None,
        "finished_at": None,
        "updated_at": None,
        "db_size_before": None,
        "db_size_after": None,
        "reclaimed_bytes": None,
        "warning": None,
        "error": None,
    }


def purge_progress(hass: HomeAssistant) -> dict[str, Any]:
    """Return the current in-memory purge job snapshot."""
    return dict(
        hass.data.setdefault(DOMAIN, {}).get("purge_progress", _default_progress())
    )


def set_purge_progress(hass: HomeAssistant, **updates: Any) -> dict[str, Any]:
    """Update the current job and derive its display counters."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    progress = domain_data.setdefault("purge_progress", _default_progress())
    progress.update(updates)
    progress["updated_at"] = _iso_now()

    total = max(0, int(progress.get("total_rows", 0) or 0))
    remaining = max(0, int(progress.get("remaining_rows", 0) or 0))
    if remaining > total:
        total = remaining
        progress["total_rows"] = total
    deleted = max(0, total - remaining)
    progress["deleted_rows"] = deleted
    progress["percent"] = round((deleted / total) * 100, 1) if total else 100.0
    return dict(progress)


def purge_is_active(hass: HomeAssistant) -> bool:
    task = hass.data.setdefault(DOMAIN, {}).get("purge_task")
    return bool(purge_progress(hass).get("active")) or bool(
        task is not None and not task.done()
    )


async def _wait_recorder_idle(hass: HomeAssistant) -> None:
    """Wait for recorder work queued ahead of this synchronization point."""
    global _BLOCK_API_WARNED
    try:
        from homeassistant.components.recorder import get_instance

        instance = get_instance(hass)
        block_fn = getattr(instance, "async_block_till_done", None)
        if block_fn is not None:
            await block_fn()
        elif not _BLOCK_API_WARNED:
            _LOGGER.warning(
                "Recorder.async_block_till_done() is unavailable; purge analysis "
                "may briefly show stale data."
            )
            _BLOCK_API_WARNED = True
    except Exception as err:  # noqa: BLE001
        if not _BLOCK_API_WARNED:
            _LOGGER.warning("Could not wait for recorder to be idle: %s", err)
            _BLOCK_API_WARNED = True
    await asyncio.sleep(0.3)


async def _purge_entities_and_wait(
    hass: HomeAssistant,
    entity_ids: list[str],
    *,
    cutoff: datetime,
) -> dict[str, Any]:
    """Queue one HA purge_entities task and monitor all of its batches."""
    normalized = sorted({entity_id for entity_id in entity_ids if entity_id})
    set_purge_progress(hass, status="counting", determinate=False)
    initial_rows = await async_count_entity_rows_before(
        hass, normalized, cutoff.timestamp()
    )
    set_purge_progress(
        hass,
        status="purging",
        determinate=True,
        total_rows=initial_rows,
        remaining_rows=initial_rows,
    )
    if not normalized or initial_rows == 0:
        return set_purge_progress(hass, status="verifying", remaining_rows=0)

    # Home Assistant requeues bounded PurgeEntitiesTask batches itself. Calling
    # the service again here would create overlapping jobs, so enqueue only once.
    await hass.services.async_call(
        "recorder",
        "purge_entities",
        {"entity_id": normalized, "keep_days": 0},
        blocking=True,
    )

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _PURGE_TIMEOUT_SECONDS
    last_progress_at = loop.time()
    last_remaining = initial_rows
    while True:
        await _wait_recorder_idle(hass)
        remaining = await async_count_entity_rows_before(
            hass, normalized, cutoff.timestamp()
        )
        updates: dict[str, Any] = {"status": "purging", "remaining_rows": remaining}
        if remaining < last_remaining:
            last_remaining = remaining
            last_progress_at = loop.time()
            updates["last_progress_at"] = _iso_now()
        set_purge_progress(hass, **updates)
        if remaining == 0:
            set_purge_progress(hass, status="verifying", remaining_rows=0)
            # A synchronization marker can be overtaken by a purge batch that
            # requeued itself behind it. One final barrier covers that batch.
            await _wait_recorder_idle(hass)
            return purge_progress(hass)

        now = loop.time()
        if now - last_progress_at >= _PURGE_STALL_TIMEOUT_SECONDS:
            raise TimeoutError(
                "Purge monitoring stopped because the row count did not decrease "
                f"for {_PURGE_STALL_TIMEOUT_SECONDS // 60} minutes; {remaining} "
                "pre-existing rows remain. Home Assistant may still continue its "
                "queued purge in the background."
            )
        if now >= deadline:
            raise TimeoutError(
                f"Purge did not finish within {_PURGE_TIMEOUT_SECONDS // 3600} "
                f"hours; {remaining} pre-existing rows remain. Home Assistant may "
                "still continue its queued purge in the background."
            )
        await asyncio.sleep(_PURGE_POLL_INTERVAL)


def _history_entry(progress: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "job_id",
        "kind",
        "label",
        "status",
        "entity_count",
        "total_rows",
        "deleted_rows",
        "remaining_rows",
        "determinate",
        "repack",
        "analyze_after",
        "stats_refreshed",
        "started_at",
        "finished_at",
        "db_size_before",
        "db_size_after",
        "reclaimed_bytes",
        "warning",
        "error",
    )
    return {key: progress.get(key) for key in keys}


async def _persist_job_start(hass: HomeAssistant, progress: dict[str, Any]) -> None:
    data = await async_reload_data(hass)
    data["active_purge"] = _history_entry(progress)
    data["active_purge"]["active"] = True
    data["repack_in_progress"] = bool(progress.get("repack"))
    await async_save_data(hass, data)


async def _persist_job_finish(hass: HomeAssistant, progress: dict[str, Any]) -> None:
    data = await async_reload_data(hass)
    history = list(data.get("purge_history") or [])
    history.insert(0, _history_entry(progress))
    data["purge_history"] = history[:_PURGE_HISTORY_LIMIT]
    data["active_purge"] = None
    data["repack_in_progress"] = False
    await async_save_data(hass, data)


async def async_restore_interrupted_purge(hass: HomeAssistant) -> None:
    """Turn a persisted active job into an honest interrupted history entry."""
    data = hass.data[DOMAIN]["data"]
    active = data.get("active_purge")
    if not isinstance(active, dict) or not active.get("active"):
        return

    interrupted = dict(active)
    interrupted.update(
        {
            "active": False,
            "status": "interrupted",
            "finished_at": _iso_now(),
            "error": (
                "Home Assistant restarted while this job was active. Any recorder "
                "batch already queued may have completed, but GUI Recorder did not "
                "assume or restart it. Refresh the analysis before retrying."
            ),
        }
    )
    set_purge_progress(hass, **interrupted)
    history = list(data.get("purge_history") or [])
    history.insert(0, _history_entry(interrupted))
    data["purge_history"] = history[:_PURGE_HISTORY_LIMIT]
    data["active_purge"] = None
    data["repack_in_progress"] = False
    await async_save_data(hass, data)


async def _run_purge_job(
    hass: HomeAssistant,
    *,
    entity_ids: list[str],
    keep_days: int | None,
) -> None:
    progress = purge_progress(hass)
    before_size = progress.get("db_size_before")
    try:
        if keep_days is not None:
            set_purge_progress(hass, status="purging", determinate=False)
            await hass.services.async_call(
                "recorder",
                "purge",
                {
                    "keep_days": keep_days,
                    "repack": bool(progress.get("repack")),
                },
                blocking=True,
            )
            await _wait_recorder_idle(hass)
        else:
            await _purge_entities_and_wait(
                hass,
                entity_ids,
                cutoff=datetime.fromisoformat(progress["started_at"]),
            )
            if progress.get("repack"):
                set_purge_progress(hass, status="repacking", determinate=False)
                await hass.services.async_call(
                    "recorder",
                    "purge",
                    {"keep_days": 36500, "repack": True},
                    blocking=True,
                )
                await _wait_recorder_idle(hass)

        analyze_after = bool(progress.get("analyze_after", True))
        after_size = None
        analysis_warning = None
        stats_refreshed = False
        if analyze_after:
            set_purge_progress(hass, status="analyzing", determinate=False)
            stats = await async_analyze_db(hass)
            if stats.get("error"):
                analysis_warning = (
                    "Purge completed, but database analysis failed: "
                    f"{stats['error']}"
                )
            else:
                stats_refreshed = True
                after_size = stats.get("db_size_bytes")
        reclaimed = None
        if before_size is not None and after_size is not None:
            reclaimed = max(0, int(before_size) - int(after_size))
        progress = set_purge_progress(
            hass,
            active=True,
            status="finalizing",
            determinate=keep_days is None,
            remaining_rows=0,
            finished_at=_iso_now(),
            db_size_after=after_size,
            reclaimed_bytes=reclaimed,
            stats_refreshed=stats_refreshed,
            warning=analysis_warning,
            error=None,
        )
        completed = {**progress, "active": False, "status": "completed"}
        await _persist_job_finish(hass, completed)
        set_purge_progress(hass, **completed)
    except asyncio.CancelledError:
        # Leave active_purge persisted. The next integration startup records it
        # as interrupted instead of pretending the recorder work was completed.
        set_purge_progress(
            hass,
            active=False,
            status="interrupted",
            determinate=keep_days is None,
            finished_at=_iso_now(),
            error="Purge monitoring stopped because Home Assistant shut down.",
        )
        raise
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("GUI Recorder purge job failed")
        progress = set_purge_progress(
            hass,
            active=True,
            status="error",
            determinate=keep_days is None,
            finished_at=_iso_now(),
            error=str(err),
        )
        failed = {**progress, "active": False}
        await _persist_job_finish(hass, failed)
        set_purge_progress(hass, **failed)


async def async_start_purge_job(
    hass: HomeAssistant,
    *,
    kind: str,
    label: str,
    entity_ids: list[str] | None = None,
    keep_days: int | None = None,
    repack: bool = False,
) -> dict[str, Any]:
    """Create a purge job that is independent from its WebSocket connection."""
    if purge_is_active(hass):
        raise RuntimeError("Another purge is already in progress")

    normalized = sorted({entity_id.strip() for entity_id in entity_ids or [] if entity_id.strip()})
    data = hass.data[DOMAIN]["data"]
    stats = data.get("stats") or {}
    started_at = _iso_now()
    progress = set_purge_progress(
        hass,
        job_id=uuid4().hex,
        active=True,
        status="queued",
        kind=kind,
        label=label,
        entity_count=len(normalized),
        total_rows=0,
        remaining_rows=0,
        deleted_rows=0,
        percent=0.0,
        determinate=keep_days is None,
        repack=bool(repack),
        analyze_after=bool(data.get("auto_update_data", True)),
        stats_refreshed=False,
        started_at=started_at,
        finished_at=None,
        last_progress_at=started_at,
        db_size_before=stats.get("db_size_bytes"),
        db_size_after=None,
        reclaimed_bytes=None,
        warning=None,
        error=None,
    )
    await _persist_job_start(hass, progress)

    coroutine = _run_purge_job(
        hass,
        entity_ids=normalized,
        keep_days=keep_days,
    )
    task_name = f"{DOMAIN} purge {progress['job_id']}"
    config_entry = hass.data[DOMAIN].get("config_entry")
    if config_entry is not None and hasattr(
        config_entry, "async_create_background_task"
    ):
        task = config_entry.async_create_background_task(
            hass, coroutine, task_name
        )
    else:
        # Compatibility fallback for older Home Assistant versions and tests.
        task = hass.async_create_background_task(coroutine, task_name)
    hass.data[DOMAIN]["purge_task"] = task

    def _clear_finished(done_task: asyncio.Task) -> None:
        if hass.data.get(DOMAIN, {}).get("purge_task") is done_task:
            hass.data[DOMAIN]["purge_task"] = None

    task.add_done_callback(_clear_finished)
    return progress


def purge_history(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return newest-first persisted purge history."""
    return list(hass.data[DOMAIN]["data"].get("purge_history") or [])
