from __future__ import annotations

import asyncio
from dataclasses import asdict

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigSubentryFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector as sel

from .cloud_devices import device_names, sign_in
from .const import (CONF_ACCOUNT, CONF_ACCOUNT_PASSWORD, CONF_DEVICE_ID, CONF_DEVICES, CONF_LIVE, CONF_LOGIN_REGION,
                    CONF_P2P, CONF_RTSP_PASSWORD, CONF_SIDEBAR, CONF_SOURCE, DOMAIN, ENTRY_VERSION, KEY_FIELDS,
                    SOURCE_AUTO, SOURCES, SUBENTRY_CAMERA)
from .controller import camera_subentries
from .login import LoginFailed, LoginUnavailable, account_to_dict, load_app_keys

_PW = sel.TextSelector(sel.TextSelectorConfig(type=sel.TextSelectorType.PASSWORD))


async def _sign_in(data: dict, override: dict | None = None):
    try:
        keys = await asyncio.to_thread(load_app_keys, override)
        acc, resp = await sign_in(keys, data[CONF_ACCOUNT], data[CONF_ACCOUNT_PASSWORD], data[CONF_LOGIN_REGION])
    except LoginFailed:
        return "invalid_auth", None, None
    except LoginUnavailable:
        return "cannot_connect", None, None
    return None, account_to_dict(acc), device_names(resp)


def _label(name: str, dev: str) -> str:
    return f"{name} ({dev})" if name != dev else dev


def _camera_schema(names: dict[str, str], cur: dict) -> vol.Schema:
    options = [{"value": d, "label": _label(n, d)} for d, n in names.items()]
    return vol.Schema({
        vol.Required(CONF_DEVICE_ID, default=next(iter(names), None)):
            sel.SelectSelector(sel.SelectSelectorConfig(options=options)),
        **_settings_schema(cur),
    })


def _settings_schema(cur: dict) -> dict:
    return {
        vol.Required(CONF_SOURCE, default=cur.get(CONF_SOURCE, SOURCE_AUTO)): sel.SelectSelector(
            sel.SelectSelectorConfig(options=list(SOURCES), translation_key="live_source")),
        vol.Optional(CONF_RTSP_PASSWORD, description={"suggested_value": cur.get(CONF_RTSP_PASSWORD)}): _PW,
        vol.Optional(CONF_LIVE, description={"suggested_value": cur.get(CONF_LIVE)}):
            sel.EntitySelector(sel.EntitySelectorConfig(domain="camera")),
    }


def _settings_data(user_input: dict) -> dict:
    data = {CONF_SOURCE: user_input[CONF_SOURCE]}
    for k in (CONF_RTSP_PASSWORD, CONF_LIVE):
        if (v := user_input.get(k)) and str(v).strip():
            data[k] = v
    return data


class YooseeConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = ENTRY_VERSION

    @staticmethod
    @callback
    def async_get_options_flow(entry):
        return YooseeOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry):
        return {SUBENTRY_CAMERA: CameraSubentryFlow}

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_ACCOUNT].strip().lower())
            self._abort_if_unique_id_configured()
            err, p2p, names = await _sign_in(user_input)
            if err:
                errors["base"] = err
            elif not names:
                return self.async_abort(reason="no_devices")
            else:
                self._data = {**user_input, CONF_ACCOUNT: user_input[CONF_ACCOUNT].strip(), CONF_P2P: p2p}
                self._names = names
                return await self.async_step_devices()
        schema = vol.Schema({
            vol.Required(CONF_ACCOUNT): sel.TextSelector(sel.TextSelectorConfig(type=sel.TextSelectorType.EMAIL)),
            vol.Required(CONF_ACCOUNT_PASSWORD): _PW,
            vol.Required(CONF_LOGIN_REGION, default=self.hass.config.country or "US"): sel.CountrySelector(),
        })
        return self.async_show_form(step_id="user", data_schema=self.add_suggested_values_to_schema(schema, user_input),
                                    errors=errors)

    async def async_step_devices(self, user_input=None):
        if user_input is not None and user_input[CONF_DEVICES]:
            subentries = [{"data": {CONF_DEVICE_ID: d, CONF_SOURCE: SOURCE_AUTO}, "subentry_type": SUBENTRY_CAMERA,
                           "title": self._names[d], "unique_id": d} for d in user_input[CONF_DEVICES]]
            return self.async_create_entry(title=self._data[CONF_ACCOUNT], data=self._data,
                                           options={CONF_SIDEBAR: user_input[CONF_SIDEBAR]}, subentries=subentries)
        options = [{"value": d, "label": _label(n, d)} for d, n in self._names.items()]
        schema = vol.Schema({
            vol.Required(CONF_DEVICES, default=list(self._names)): sel.SelectSelector(
                sel.SelectSelectorConfig(options=options, multiple=True)),
            vol.Required(CONF_SIDEBAR, default=True): bool,
        })
        return self.async_show_form(step_id="devices", data_schema=schema,
                                    errors={"base": "no_selection"} if user_input else {})

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            data = {**entry.data, **user_input}
            err, p2p, _ = await _sign_in(data, entry.options)
            if not err:
                return self.async_update_reload_and_abort(entry, data={**data, CONF_P2P: p2p})
            errors["base"] = err
        return self.async_show_form(step_id="reauth_confirm", data_schema=vol.Schema({
            vol.Required(CONF_ACCOUNT_PASSWORD): _PW}), errors=errors,
            description_placeholders={"account": entry.data[CONF_ACCOUNT]})


class YooseeOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input=None):
        entry = self.config_entry
        errors = {}
        builtin = asdict(await asyncio.to_thread(load_app_keys))
        if user_input is not None:
            keys = {k: user_input.get(k, "").strip() for k in KEY_FIELDS}
            if keys == builtin:
                keys = dict.fromkeys(KEY_FIELDS, "")
            if any(keys.values()) and not all(keys.values()):
                errors["base"] = "invalid_keys"
            elif all(keys.values()) and any(keys[k] != entry.options.get(k) for k in KEY_FIELDS):
                err, p2p, _ = await _sign_in(entry.data, keys)
                if err:
                    errors["base"] = "invalid_keys"
                else:
                    self.hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_P2P: p2p})
            if not errors:
                return self.async_create_entry(data={**keys, CONF_SIDEBAR: user_input[CONF_SIDEBAR]})
        schema = vol.Schema({
            vol.Required(CONF_SIDEBAR, default=entry.options.get(CONF_SIDEBAR, True)): bool,
            **{vol.Optional(k, description={"suggested_value": entry.options.get(k) or builtin[k]}): str
               for k in KEY_FIELDS},
        })
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)


class CameraSubentryFlow(ConfigSubentryFlow):
    async def async_step_user(self, user_input=None):
        entry = self._get_entry()
        account = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
        taken = {str(s.data[CONF_DEVICE_ID]) for s in camera_subentries(entry).values()}
        try:
            names = {d: n for d, n in device_names(await account.device_list()).items() if d not in taken}
        except (AttributeError, LoginUnavailable):
            names = {}
        if not names:
            return self.async_abort(reason="no_cameras")
        if user_input is not None:
            d = user_input[CONF_DEVICE_ID]
            return self.async_create_entry(title=names.get(d, d),
                                           data={CONF_DEVICE_ID: d, **_settings_data(user_input)}, unique_id=d)
        return self.async_show_form(step_id="user", data_schema=_camera_schema(names, {}))

    async def async_step_reconfigure(self, user_input=None):
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            title = user_input.get("name", "").strip() or subentry.title
            data = {CONF_DEVICE_ID: subentry.data[CONF_DEVICE_ID], **_settings_data(user_input)}
            return self.async_update_and_abort(self._get_entry(), subentry, title=title, data=data)
        schema = vol.Schema({
            vol.Optional("name", description={"suggested_value": subentry.title}): str,
            **_settings_schema(dict(subentry.data)),
        })
        return self.async_show_form(step_id="reconfigure", data_schema=schema,
                                    description_placeholders={"camera": subentry.title})
