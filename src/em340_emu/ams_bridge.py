"""Translates the AMS-to-MQTT bridge firmware's JSON schema (as used by
gskjold/AmsToMqttBridge and compatible forks -- confirmed against a live
device publishing Home Assistant MQTT discovery configs for entity ids like
`ams-<id>_P1`, `ams-<id>_U1`, `ams-<id>_tPI`) onto the canonical
em340_emu.sources / parameters.py key vocabulary.

That firmware publishes three JSON messages under a base topic (by default
"energy-meter", matching this project's own default):

* `<base>/power`  -- instantaneous per-phase voltage/current/power, e.g.
  `{"P":146,"P1":47.0,"PO1":0.0,"Q":0,"QO":387,"I1":0.5,"U1":233.4,"PF1":1.0,...}`
* `<base>/energy` -- cumulative energy totals, e.g.
  `{"tPI":24208.34,"tPO":12099.35,"tQI":2874.71,"tQO":3775.11,"rtc":1778110110}`
* `<base>/realtime` and `<base>/state` -- app-level stats (cost, peaks,
  wifi RSSI, supply voltage) that have no EM340 register equivalent and are
  intentionally not translated.

The firmware only exposes reactive power (Q/QO) at the system level, not
per phase, unlike active power (P1..3/PO1..3). Per-phase reactive power is
therefore approximated by splitting the system value evenly across phases,
the same approximation already used elsewhere (see registers._phase_energy_share)
when finer-grained source data isn't available.
"""
from __future__ import annotations

POWER_FIELD_MAP = {
    "U1": "voltage_l1",
    "U2": "voltage_l2",
    "U3": "voltage_l3",
    "I1": "current_l1",
    "I2": "current_l2",
    "I3": "current_l3",
    "P1": "active_power_import_l1",
    "P2": "active_power_import_l2",
    "P3": "active_power_import_l3",
    "PO1": "active_power_export_l1",
    "PO2": "active_power_export_l2",
    "PO3": "active_power_export_l3",
}

ENERGY_FIELD_MAP = {
    "tPI": "energy_active_import",
    "tPO": "energy_active_export",
    "tQI": "energy_reactive_import",
    "tQO": "energy_reactive_export",
}


def translate_power(payload: dict) -> dict:
    values = {canonical: payload[src] for src, canonical in POWER_FIELD_MAP.items() if src in payload}
    if "Q" in payload or "QO" in payload:
        q_import = payload.get("Q", 0) / 3
        q_export = payload.get("QO", 0) / 3
        for phase in ("l1", "l2", "l3"):
            values[f"reactive_power_import_{phase}"] = q_import
            values[f"reactive_power_export_{phase}"] = q_export
    return values


def translate_energy(payload: dict) -> dict:
    return {canonical: payload[src] for src, canonical in ENERGY_FIELD_MAP.items() if src in payload}


def translate(topic: str, payload: dict) -> dict:
    """Route by the topic's final segment; returns {} for anything that
    isn't a recognized AMS-bridge power/energy message."""
    suffix = topic.rsplit("/", 1)[-1]
    if suffix == "power":
        return translate_power(payload)
    if suffix == "energy":
        return translate_energy(payload)
    return {}
