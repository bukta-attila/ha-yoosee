from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant

from .api import LiveView, StreamView, async_register_ws
from .const import (CONF_DEVICE_ID, CONF_DEVICES, CONF_LIVE, CONF_RTSP_PASSWORD, CONF_SIDEBAR, CONF_SOURCE, DOMAIN,
                    ENTRY_VERSION, PANEL_ELEMENT, PANEL_URL, PANEL_VERSION, SOURCE_AUTO, STATIC_URL, SUBENTRY_CAMERA)
from .controller import Account, camera_subentries

_FRONTEND = Path(__file__).parent / "frontend"
PLATFORMS = ["camera"]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.version >= ENTRY_VERSION:
        return True
    options = dict(entry.options)
    devices = options.pop(CONF_DEVICES, {})
    sources = options.pop(CONF_SOURCE, {})
    passwords = options.pop(CONF_RTSP_PASSWORD, {})
    live = options.pop(CONF_LIVE, {})
    taken = {str(s.data.get(CONF_DEVICE_ID)) for s in camera_subentries(entry).values()}
    for dev, name in devices.items():
        if str(dev) in taken:
            continue
        data = {CONF_DEVICE_ID: str(dev), CONF_SOURCE: sources.get(dev, SOURCE_AUTO)}
        if passwords.get(dev):
            data[CONF_RTSP_PASSWORD] = passwords[dev]
        if live.get(dev):
            data[CONF_LIVE] = live[dev]
        hass.config_entries.async_add_subentry(entry, ConfigSubentry(
            data=MappingProxyType(data), subentry_type=SUBENTRY_CAMERA, title=name or str(dev), unique_id=str(dev)))
    hass.config_entries.async_update_entry(entry, options=options, version=ENTRY_VERSION)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not hass.data.get(DOMAIN + "_http"):
        await hass.http.async_register_static_paths([StaticPathConfig(STATIC_URL, str(_FRONTEND), False)])
        hass.http.register_view(StreamView())
        hass.http.register_view(LiveView())
        async_register_ws(hass)
        hass.data[DOMAIN + "_http"] = True
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = Account(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _update_panel(hass)
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await hass.data[DOMAIN].pop(entry.entry_id).shutdown()
    _update_panel(hass)
    return True


def _state(entry: ConfigEntry) -> tuple:
    return (dict(entry.options), {sid: dict(s.data) for sid, s in camera_subentries(entry).items()})


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    account = hass.data[DOMAIN].get(entry.entry_id)
    if account and (account.options, account.subentry_state) != _state(entry):
        await hass.config_entries.async_reload(entry.entry_id)


def _update_panel(hass: HomeAssistant) -> None:
    accounts = hass.data[DOMAIN].values()
    frontend.async_remove_panel(hass, PANEL_URL, warn_if_unknown=False)
    if not any(a.cameras for a in accounts):
        return
    frontend.async_register_built_in_panel(
        hass, component_name="custom", sidebar_title="Yoosee", sidebar_icon="mdi:filmstrip-box-multiple",
        frontend_url_path=PANEL_URL, show_in_sidebar=any(a.entry.options.get(CONF_SIDEBAR, True) for a in accounts),
        config={"_panel_custom": {"name": PANEL_ELEMENT, "embed_iframe": False, "trust_external": False,
                                  "module_url": f"{STATIC_URL}/yoosee-panel.js?v={PANEL_VERSION}"}},
    )
