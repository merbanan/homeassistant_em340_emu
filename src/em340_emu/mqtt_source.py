"""MQTT-sourced live readings.

Some P1/HAN bridges publish parsed readings straight to an MQTT broker
instead of (or as well as) exposing them as Home Assistant entities. This
subscribes to a topic (which may be a wildcard filter, e.g. "energy-meter/#")
and accepts two payload shapes per message, tried in this order:

1. A flat JSON object already using the canonical key vocabulary from
   sources.apply_values() / parameters.py, e.g.
   `{"voltage_l1": 231.4, "current_l1": 6.2, ...}` -- any such keys are
   passed straight through.
2. The AMS-to-MQTT bridge firmware's compact schema (see ams_bridge.py),
   e.g. `{"P1": 47.0, "U1": 233.4, ...}` under a `<base>/power` or
   `<base>/energy` topic -- translated onto the same canonical keys.

Both can be present in the same message; whatever keys are found (after
translation) are merged and applied. Messages that yield nothing recognized
(app-level stats topics like `<base>/realtime`, unrelated topics caught by a
wildcard, etc.) are silently ignored.

paho-mqtt runs its network loop on its own background thread, so incoming
messages are handed back to the asyncio loop that called start() via
call_soon_threadsafe rather than touching MeterState directly from that
thread.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

import paho.mqtt.client as mqtt

from . import ams_bridge
from .parameters import PARAMETERS_BY_KEY

log = logging.getLogger("em340_emu.mqtt_source")

DEFAULT_PORT = 1883


class MqttSource:
    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        topic: str = "energy-meter/#",
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.topic = topic
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if username:
            self._client.username_pw_set(username, password)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_values: Callable[[dict], None] | None = None

    def start(self, on_values: Callable[[dict], None]) -> None:
        self._loop = asyncio.get_running_loop()
        self._on_values = on_values
        self._client.connect_async(self.host, self.port)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code == 0:
            log.info("connected to mqtt broker %s:%s, subscribing to %r", self.host, self.port, self.topic)
            client.subscribe(self.topic)
        else:
            log.warning("mqtt connection to %s:%s failed: %s", self.host, self.port, reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        log.warning("mqtt broker %s:%s disconnected (%s); paho will keep retrying", self.host, self.port, reason_code)

    def _on_message(self, client, userdata, msg) -> None:
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            log.warning("ignoring non-JSON message on %s: %s", msg.topic, exc)
            return
        if not isinstance(payload, dict):
            log.warning("ignoring non-object message on %s", msg.topic)
            return

        values = ams_bridge.translate(msg.topic, payload)
        values.update({key: value for key, value in payload.items() if key in PARAMETERS_BY_KEY})

        if not values:
            return
        if self._loop is not None and self._on_values is not None:
            self._loop.call_soon_threadsafe(self._on_values, values)
