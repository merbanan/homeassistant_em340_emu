import asyncio
import json
from types import SimpleNamespace

from em340_emu.mqtt_source import MqttSource


class _StubClient:
    """Stands in for paho.mqtt.client.Client so tests don't touch a real
    broker; only the handful of methods MqttSource actually calls."""

    def __init__(self, *_args, **_kwargs):
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.subscribed = None
        self.connected_to = None
        self.username = None
        self.stopped = False
        self.disconnected = False

    def username_pw_set(self, username, password):
        self.username = (username, password)

    def connect_async(self, host, port):
        self.connected_to = (host, port)

    def loop_start(self):
        pass

    def loop_stop(self):
        self.stopped = True

    def disconnect(self):
        self.disconnected = True

    def subscribe(self, topic):
        self.subscribed = topic


def _make_source(monkeypatch, **kwargs) -> tuple[MqttSource, _StubClient]:
    created: list[_StubClient] = []

    def _factory(*args, **kw):
        client = _StubClient(*args, **kw)
        created.append(client)
        return client

    monkeypatch.setattr("em340_emu.mqtt_source.mqtt.Client", _factory)
    source = MqttSource(host="127.0.0.1", **kwargs)
    return source, created[0]


async def test_start_connects_and_subscribes(monkeypatch):
    source, client = _make_source(monkeypatch, port=1883, topic="energy-meter")
    received = []
    source.start(received.append)
    assert client.connected_to == ("127.0.0.1", 1883)

    client.on_connect(client, None, None, 0)
    assert client.subscribed == "energy-meter"


async def test_username_password_applied(monkeypatch):
    source, client = _make_source(monkeypatch, username="alice", password="secret")
    source.start(lambda values: None)
    assert client.username == ("alice", "secret")


async def test_valid_json_message_dispatched(monkeypatch):
    source, client = _make_source(monkeypatch)
    received = []
    source.start(received.append)

    msg = SimpleNamespace(topic="energy-meter", payload=json.dumps({"voltage_l1": 231.4}).encode())
    client.on_message(client, None, msg)

    # call_soon_threadsafe defers to the next loop iteration
    await asyncio.sleep(0)
    assert received == [{"voltage_l1": 231.4}]


async def test_ams_bridge_payload_translated_by_topic(monkeypatch):
    source, client = _make_source(monkeypatch, topic="energy-meter/#")
    received = []
    source.start(received.append)

    msg = SimpleNamespace(topic="energy-meter/power", payload=json.dumps({"U1": 233.2, "P1": 47.0}).encode())
    client.on_message(client, None, msg)
    await asyncio.sleep(0)

    assert received == [{"voltage_l1": 233.2, "active_power_import_l1": 47.0}]


async def test_unrecognized_topic_yields_no_dispatch(monkeypatch):
    source, client = _make_source(monkeypatch, topic="energy-meter/#")
    received = []
    source.start(received.append)

    msg = SimpleNamespace(topic="energy-meter/realtime", payload=json.dumps({"max": 3.6}).encode())
    client.on_message(client, None, msg)
    await asyncio.sleep(0)

    assert received == []


async def test_invalid_json_is_ignored_not_raised(monkeypatch):
    source, client = _make_source(monkeypatch)
    received = []
    source.start(received.append)

    msg = SimpleNamespace(topic="energy-meter", payload=b"not json")
    client.on_message(client, None, msg)  # must not raise
    await asyncio.sleep(0)
    assert received == []


async def test_non_object_json_is_ignored(monkeypatch):
    source, client = _make_source(monkeypatch)
    received = []
    source.start(received.append)

    msg = SimpleNamespace(topic="energy-meter", payload=json.dumps([1, 2, 3]).encode())
    client.on_message(client, None, msg)
    await asyncio.sleep(0)
    assert received == []


async def test_stop_stops_loop_and_disconnects(monkeypatch):
    source, client = _make_source(monkeypatch)
    source.start(lambda values: None)
    source.stop()
    assert client.stopped is True
    assert client.disconnected is True
