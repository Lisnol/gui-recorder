from __future__ import annotations

from pathlib import Path

from aiohttp import web

from homeassistant.components import frontend
from homeassistant.core import HomeAssistant

from .const import DOMAIN, INTEGRATION_VERSION, PANEL_ICON, PANEL_MODULE_URL, PANEL_TITLE, PANEL_URL_PATH

_PANEL_JS_PATH = Path(__file__).parent / "frontend" / "gui-recorder-panel.js"


def _read_panel_js() -> str:
    return _PANEL_JS_PATH.read_text(encoding="utf-8")


async def async_setup_panel(hass: HomeAssistant) -> None:
    panel_js = await hass.async_add_executor_job(_read_panel_js)
    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data["panel_js"] = panel_js

    async def _serve_panel_js(_request: web.Request) -> web.Response:
        # The HTTP route lives for the lifetime of Home Assistant, while a
        # config entry may be reloaded. Read the current source from hass.data
        # so a reload serves the newly installed panel instead of the closure's
        # original, stale JavaScript.
        current_js = hass.data.get(DOMAIN, {}).get("panel_js", "")
        return web.Response(text=current_js, content_type="application/javascript")

    if not domain_data.get("panel_route_registered"):
        hass.http.app.router.add_get(PANEL_MODULE_URL, _serve_panel_js)
        domain_data["panel_route_registered"] = True

    frontend.async_register_built_in_panel(
        hass,
        component_name="custom",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        frontend_url_path=PANEL_URL_PATH,
        config={
            "_panel_custom": {
                "name": "gui-recorder-panel",
                "module_url": f"{PANEL_MODULE_URL}?v={INTEGRATION_VERSION}",
                "trust_external": False,
            }
        },
        require_admin=True,
        # ``async_panel_exists`` was added after our minimum supported Home
        # Assistant release. Track our own registration state so reloads can
        # update the panel without importing a newer-only frontend API.
        update=bool(domain_data.get("panel_registered")),
    )

    domain_data["panel_registered"] = True
