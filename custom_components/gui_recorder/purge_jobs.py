from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Any
from uuid import uuid4

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .db_stats import async_analyze_db, async_count_entity_rows_before
from .storage import async_update_data

_LOGGER = logging.getLogger(__name__)

# Counting a large target set is itself a non-trivial indexed query. Polling at
# sub-second cadence can slow the delete batches it is meant to observe.
_PURGE_POLL_INTERVAL = 2.0
_PURGE_STALL_TIMEOUT_SECONDS = 10 * 60
_PURGE_HISTORY_LIMIT = 20
_PURGE_CHECKPOINT_INTERVAL_SECONDS = 30
_NOTIFICATION_MIN_SECONDS = 60


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
        "last_progress_at": None,
        "checkpointed_at": None,
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


def _finish_recorder_future(
    future: asyncio.Future[None], error: Exception | None = None
) -> None:
    """Resolve a recorder completion future safely on HA's event loop."""
    if future.done():
        return
    if error is None:
        future.set_result(None)
    else:
        future.set_exception(error)


def _queue_tracked_recorder_purge(
    hass: HomeAssistant,
    *,
    cutoff: datetime,
    entity_ids: list[str] | None,
    repack: bool,
) -> asyncio.Future[None]:
    """Queue a Recorder task that resolves only after its final batch.

    Home Assistant's public purge services intentionally return after enqueuing
    their first bounded batch. Its PurgeTask then requeues itself, which means a
    normal recorder synchronization marker can overtake the next batch. This
    wrapper uses the same recorder purge functions and owns the requeue loop,
    giving GUI Recorder an authoritative completion signal including cleanup
    and repack.
    """
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder import purge as recorder_purge
    from homeassistant.components.recorder.tasks import RecorderTask

    instance = get_instance(hass)
    loop = asyncio.get_running_loop()
    completed: asyncio.Future[None] = loop.create_future()
    normalized = frozenset(entity_ids or [])

    class _TrackedPurgeTask(RecorderTask):
        commit_before = True

        def run(self, recorder_instance) -> None:
            try:
                if normalized:
                    done = recorder_purge.purge_entity_data(
                        recorder_instance,
                        normalized.__contains__,
                        cutoff,
                    )
                else:
                    done = recorder_purge.purge_old_data(
                        recorder_instance,
                        cutoff,
                        repack,
                        False,
                    )
                if not done:
                    recorder_instance.queue_task(self)
                    return

                if not normalized:
                    from homeassistant.components.recorder.util import (
                        periodic_db_cleanups,
                    )

                    # Match Home Assistant's PurgeTask finalization. Older HA
                    # releases refreshed this manager in the task itself;
                    # current releases do it in purge_old_data. Repeating the
                    # read is harmless and keeps the whole supported range
                    # consistent before the periodic WAL/DB cleanup runs.
                    with recorder_instance.get_session() as session:
                        recorder_instance.recorder_runs_manager.load_from_db(session)
                    periodic_db_cleanups(recorder_instance)
            except Exception as err:  # noqa: BLE001 - propagate to HA loop
                loop.call_soon_threadsafe(_finish_recorder_future, completed, err)
                return

            loop.call_soon_threadsafe(_finish_recorder_future, completed)

    instance.queue_task(_TrackedPurgeTask())
    return completed


async def _wait_for_tracked_completion(
    hass: HomeAssistant,
    completion: asyncio.Future[None],
    *,
    long_status: str,
) -> None:
    """Wait indefinitely but make a long-running job visible and persistent."""
    try:
        await asyncio.wait_for(
            asyncio.shield(completion), timeout=_PURGE_STALL_TIMEOUT_SECONDS
        )
        return
    except asyncio.TimeoutError:
        progress = set_purge_progress(
            hass,
            status=long_status,
            warning=(
                "Recorder is still processing this operation after "
                f"{_PURGE_STALL_TIMEOUT_SECONDS // 60} minutes. It remains "
                "tracked and overlapping maintenance is blocked."
            ),
        )
        await _persist_job_checkpoint(hass, progress, force=True)
    await completion


async def _purge_entities_and_wait(
    hass: HomeAssistant,
    entity_ids: list[str],
    *,
    cutoff: datetime,
) -> dict[str, Any]:
    """Queue one HA purge_entities task and monitor all of its batches."""
    normalized = sorted({entity_id for entity_id in entity_ids if entity_id})
    progress = set_purge_progress(hass, status="counting", determinate=False)
    await _persist_job_checkpoint(hass, progress, force=True)
    initial_rows = await async_count_entity_rows_before(
        hass, normalized, cutoff.timestamp()
    )
    progress = set_purge_progress(
        hass,
        status="purging",
        determinate=True,
        total_rows=initial_rows,
        remaining_rows=initial_rows,
    )
    await _persist_job_checkpoint(hass, progress, force=True)
    if not normalized or initial_rows == 0:
        return set_purge_progress(hass, status="verifying", remaining_rows=0)

    completion = _queue_tracked_recorder_purge(
        hass,
        cutoff=cutoff,
        entity_ids=normalized,
        repack=False,
    )

    loop = asyncio.get_running_loop()
    last_progress_at = loop.time()
    last_remaining = initial_rows
    warned_stalled = False
    while True:
        try:
            await asyncio.wait_for(
                asyncio.shield(completion), timeout=_PURGE_POLL_INTERVAL
            )
            return set_purge_progress(
                hass, status="verifying", remaining_rows=0
            )
        except asyncio.TimeoutError:
            pass

        remaining = await async_count_entity_rows_before(
            hass, normalized, cutoff.timestamp()
        )
        updates: dict[str, Any] = {
            "status": "stalled" if warned_stalled else "purging",
            "remaining_rows": remaining,
        }
        if remaining < last_remaining:
            last_remaining = remaining
            last_progress_at = loop.time()
            warned_stalled = False
            updates.update(
                status="purging", last_progress_at=_iso_now(), warning=None
            )
        progress = set_purge_progress(hass, **updates)
        await _persist_job_checkpoint(hass, progress)
        now = loop.time()
        if (
            not warned_stalled
            and now - last_progress_at >= _PURGE_STALL_TIMEOUT_SECONDS
        ):
            warned_stalled = True
            progress = set_purge_progress(
                hass,
                status="stalled",
                warning=(
                    "No row-count progress has been observed for "
                    f"{_PURGE_STALL_TIMEOUT_SECONDS // 60} minutes. The Recorder "
                    "task is still tracked and new purges remain blocked until it "
                    "finishes or Home Assistant restarts."
                ),
            )
            await _persist_job_checkpoint(hass, progress, force=True)


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
        "updated_at",
        "last_progress_at",
        "checkpointed_at",
        "db_size_before",
        "db_size_after",
        "reclaimed_bytes",
        "warning",
        "error",
    )
    return {key: progress.get(key) for key in keys}


async def _persist_job_start(hass: HomeAssistant, progress: dict[str, Any]) -> None:
    def _update(data: dict) -> None:
        data["active_purge"] = _history_entry(progress)
        data["active_purge"]["active"] = True
        data["repack_in_progress"] = bool(progress.get("repack"))

    await async_update_data(hass, _update)
    hass.data.setdefault(DOMAIN, {})["purge_checkpoint_monotonic"] = (
        asyncio.get_running_loop().time()
    )


async def _persist_job_checkpoint(
    hass: HomeAssistant, progress: dict[str, Any], *, force: bool = False
) -> None:
    """Persist bounded active-job progress without writing on every poll."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    now = asyncio.get_running_loop().time()
    last = float(domain_data.get("purge_checkpoint_monotonic", 0) or 0)
    if not force and now - last < _PURGE_CHECKPOINT_INTERVAL_SECONDS:
        return

    def _update(data: dict) -> bool:
        active = data.get("active_purge")
        if not isinstance(active, dict) or active.get("job_id") != progress.get(
            "job_id"
        ):
            return False
        checkpoint = _history_entry(progress)
        checkpoint["active"] = True
        checkpoint["checkpointed_at"] = _iso_now()
        data["active_purge"] = checkpoint
        data["repack_in_progress"] = bool(progress.get("repack"))
        return True

    _data, saved = await async_update_data(hass, _update)
    if not saved:
        return
    domain_data["purge_checkpoint_monotonic"] = now


async def _persist_job_finish(hass: HomeAssistant, progress: dict[str, Any]) -> None:
    def _update(data: dict) -> None:
        history = list(data.get("purge_history") or [])
        history.insert(0, _history_entry(progress))
        data["purge_history"] = history[:_PURGE_HISTORY_LIMIT]
        data["active_purge"] = None
        data["repack_in_progress"] = False

    await async_update_data(hass, _update)
    hass.data.setdefault(DOMAIN, {}).pop("purge_checkpoint_monotonic", None)


def _job_duration_seconds(progress: dict[str, Any]) -> float:
    try:
        start = datetime.fromisoformat(str(progress.get("started_at")))
        end_value = progress.get("finished_at") or _iso_now()
        end = datetime.fromisoformat(str(end_value))
        return max(0.0, (end - start).total_seconds())
    except (TypeError, ValueError):
        return 0.0


async def _notify_job_result(hass: HomeAssistant, progress: dict[str, Any]) -> None:
    """Create one replaceable HA notification for long jobs and all failures."""
    data = hass.data[DOMAIN]["data"]
    if not bool(data.get("notify_on_purge_finish", True)):
        return
    status = str(progress.get("status") or "unknown")
    duration = _job_duration_seconds(progress)
    if status == "completed" and duration < _NOTIFICATION_MIN_SECONDS:
        return

    label = str(progress.get("label") or "Recorder maintenance")
    if status == "completed":
        deleted = progress.get("deleted_rows")
        result = (
            "Home Assistant finished the purge."
            if progress.get("determinate") is False
            else f"Home Assistant deleted {int(deleted or 0):,} records."
        )
        title = "GUI Recorder maintenance completed"
    elif status == "interrupted":
        result = str(progress.get("error") or "Home Assistant restarted during the job.")
        title = "GUI Recorder maintenance interrupted"
    else:
        result = str(progress.get("error") or "The maintenance job failed.")
        title = "GUI Recorder maintenance failed"

    minutes, seconds = divmod(int(round(duration)), 60)
    duration_text = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
    try:
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": title,
                "message": f"{label}\n\n{result}\n\nDuration: {duration_text}",
                "notification_id": "gui_recorder_maintenance",
            },
            blocking=False,
        )
    except Exception as err:  # noqa: BLE001 - notifications must never fail a purge
        _LOGGER.warning("Could not create GUI Recorder notification: %s", err)


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
                "batch already executing may have committed, but queued batches do "
                "not resume after restart. Refresh the analysis before retrying."
            ),
        }
    )
    set_purge_progress(hass, **interrupted)

    def _update(current: dict) -> None:
        history = list(current.get("purge_history") or [])
        history.insert(0, _history_entry(interrupted))
        current["purge_history"] = history[:_PURGE_HISTORY_LIMIT]
        current["active_purge"] = None
        current["repack_in_progress"] = False

    await async_update_data(hass, _update)
    await _notify_job_result(hass, interrupted)


async def _run_purge_job(
    hass: HomeAssistant,
    *,
    entity_ids: list[str],
    keep_days: int | None,
) -> None:
    progress = purge_progress(hass)
    before_size = progress.get("db_size_before")
    try:
        domain_data = hass.data.setdefault(DOMAIN, {})
        operation_lock = domain_data.get("db_operation_lock")
        if operation_lock is None:
            operation_lock = asyncio.Lock()
            domain_data["db_operation_lock"] = operation_lock

        # A full count analysis and a purge are both expensive database jobs.
        # Serialize them so a user cannot accidentally run both at once.
        async with operation_lock:
            if keep_days is not None:
                progress = set_purge_progress(
                    hass, status="purging", determinate=False
                )
                await _persist_job_checkpoint(hass, progress, force=True)
                completion = _queue_tracked_recorder_purge(
                    hass,
                    cutoff=_utcnow() - timedelta(days=keep_days),
                    entity_ids=None,
                    repack=bool(progress.get("repack")),
                )
                await _wait_for_tracked_completion(
                    hass, completion, long_status="purging"
                )
            else:
                await _purge_entities_and_wait(
                    hass,
                    entity_ids,
                    cutoff=datetime.fromisoformat(progress["started_at"]),
                )
                if progress.get("repack"):
                    progress = set_purge_progress(
                        hass, status="repacking", determinate=False
                    )
                    await _persist_job_checkpoint(hass, progress, force=True)
                    completion = _queue_tracked_recorder_purge(
                        hass,
                        cutoff=_utcnow() - timedelta(days=36500),
                        entity_ids=None,
                        repack=True,
                    )
                    await _wait_for_tracked_completion(
                        hass, completion, long_status="repacking"
                    )

        analyze_after = bool(progress.get("analyze_after", True))
        after_size = None
        analysis_warning = None
        stats_refreshed = False
        if analyze_after:
            progress = set_purge_progress(
                hass, status="analyzing", determinate=False
            )
            await _persist_job_checkpoint(hass, progress, force=True)
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
        await _notify_job_result(hass, completed)
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
        await _notify_job_result(hass, failed)


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

    normalized = sorted(
        {
            entity_id.strip()
            for entity_id in entity_ids or []
            if isinstance(entity_id, str) and entity_id.strip()
        }
    )
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
    try:
        await _persist_job_start(hass, progress)
    except Exception:
        set_purge_progress(
            hass,
            active=False,
            status="error",
            finished_at=_iso_now(),
            error="The maintenance job could not be saved before it started.",
        )
        raise

    coroutine = _run_purge_job(
        hass,
        entity_ids=normalized,
        keep_days=keep_days,
    )
    task_name = f"{DOMAIN} purge {progress['job_id']}"
    config_entry = hass.data[DOMAIN].get("config_entry")
    try:
        if config_entry is not None and hasattr(
            config_entry, "async_create_background_task"
        ):
            task = config_entry.async_create_background_task(
                hass, coroutine, task_name
            )
        else:
            # Compatibility fallback for older Home Assistant versions and tests.
            task = hass.async_create_background_task(coroutine, task_name)
    except Exception:
        coroutine.close()
        failed = set_purge_progress(
            hass,
            active=False,
            status="error",
            finished_at=_iso_now(),
            error="The maintenance background task could not be started.",
        )
        try:
            await _persist_job_finish(hass, failed)
        except Exception:  # noqa: BLE001 - preserve the original start failure
            _LOGGER.exception("Could not persist GUI Recorder start failure")
        raise
    hass.data[DOMAIN]["purge_task"] = task

    def _clear_finished(done_task: asyncio.Task) -> None:
        if hass.data.get(DOMAIN, {}).get("purge_task") is done_task:
            hass.data[DOMAIN]["purge_task"] = None

    task.add_done_callback(_clear_finished)
    return progress


def purge_history(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return newest-first persisted purge history."""
    return list(hass.data[DOMAIN]["data"].get("purge_history") or [])
