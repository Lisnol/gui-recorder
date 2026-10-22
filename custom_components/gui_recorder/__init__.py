from __future__ import annotations

import logging

from homeassistant.components import frontend
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant

from .const import DOMAIN, PANEL_URL_PATH
from .db_stats import is_supported_recorder
from .migration import async_detect_sync_status, async_recover_managed_yaml
from .panel import async_setup_panel
from .purge_jobs import async_restore_interrupted_purge, purge_is_active
from .storage import async_load_data, async_patch_data, async_save_data
from .websocket_api import async_setup_websocket_api

_LOGGER = logging.getLogger(__name__)


class ConfigurationRecoveryError(RuntimeError):
    """Raised when missing storage cannot be recovered without data loss."""


async def _async_initialize(hass: HomeAssistant) -> None:
    hass.data.setdefault(DOMAIN, {})

    data = await async_load_data(hass)
    domain_data = hass.data[DOMAIN]
    migration = await async_detect_sync_status(hass)
    if domain_data.get("storage_was_missing"):
        if migration.get("gui_include_active"):
            recovery = await async_recover_managed_yaml(hass)
            if not recovery.get("ok"):
                domain_data["storage_recovery_error"] = recovery.get(
                    "error", "Unknown recovery error"
                )
                raise ConfigurationRecoveryError(
                    "Refusing to replace the active gui_recorder.yaml because "
                    f"integration storage could not be recovered: {domain_data['storage_recovery_error']}"
                )
        else:
            # A genuine first run has no active generated configuration to
            # recover. Persist defaults only after that has been established.
            await async_save_data(hass, data)
        domain_data["storage_was_missing"] = False

    await async_restore_interrupted_purge(hass)
    data = hass.data[DOMAIN]["data"]
    reset_flags = {}
    # A config-entry reload does not reload Recorder YAML. Clear restart-only
    # flags solely during Home Assistant startup, never during a live reload.
    home_assistant_starting = hass.state != CoreState.running
    if data.get("pending_restart") and home_assistant_starting:
        reset_flags["pending_restart"] = False
    if data.get("repack_in_progress") and home_assistant_starting:
        reset_flags["repack_in_progress"] = False
    if reset_flags:
        await async_patch_data(hass, **reset_flags)

    # Never regenerate gui_recorder.yaml merely because the integration starts.
    # The file may be the only surviving copy after a missing/corrupt storage
    # record; configuration writes are reserved for explicit user actions.
    hass.data[DOMAIN]["migration"] = await async_detect_sync_status(hass)

    if not hass.data[DOMAIN].get("websocket_registered"):
        await async_setup_websocket_api(hass)
        hass.data[DOMAIN]["websocket_registered"] = True

    # WebSocket routes are retained for the lifetime of Home Assistant. Open
    # them only after storage recovery and initialization have succeeded, but
    # before exposing the panel that immediately calls them.
    hass.data[DOMAIN]["loaded"] = True
    if not hass.data[DOMAIN].get("panel_registered"):
        await async_setup_panel(hass)


async def async_setup(hass: HomeAssistant, _config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if is_supported_recorder(hass) is False:
        _LOGGER.error(
            "GUI Recorder only supports the SQLite and MariaDB recorder backends. "
            "Detected a different db_url; aborting setup."
        )
        return False
    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data["config_entry"] = entry
    domain_data["loaded"] = False
    try:
        await _async_initialize(hass)
    except ConfigurationRecoveryError as err:
        _LOGGER.error("%s", err)
        domain_data["config_entry"] = None
        domain_data["loaded"] = False
        return False
    except Exception:
        domain_data["config_entry"] = None
        domain_data["loaded"] = False
        raise
    _LOGGER.info("GUI Recorder initialized")
    return True


async def async_unload_entry(hass: HomeAssistant, _entry: ConfigEntry) -> bool:
    domain_data = hass.data.get(DOMAIN, {})
    if purge_is_active(hass):
        # The actual purge runs inside Recorder's queue and cannot be withdrawn
        # safely halfway through a committed batch. Refusing a config-entry
        # reload prevents an orphaned purge from overlapping a newly started
        # one. A full Home Assistant restart remains the explicit escape hatch.
        _LOGGER.warning("Cannot unload GUI Recorder while maintenance is active")
        return False
    if domain_data.get("panel_registered"):
        frontend.async_remove_panel(hass, PANEL_URL_PATH)
        domain_data["panel_registered"] = False
    domain_data["loaded"] = False
    domain_data["config_entry"] = None
    return True
