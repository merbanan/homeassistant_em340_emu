# em340-emu

A Modbus emulator of a **Carlo Gavazzi EM340** three-phase energy meter,
designed to sit behind an RS485-to-Ethernet gateway and answer a **Wallbox**
charging station's meter polling with live values sourced from a Swedish
P1/HAN-port electricity meter, instead of a real EM340.

> The task that produced this repo referred to the target device as
> "EM430". No such Carlo Gavazzi product exists; every source document
> (protocol spec, Wallbox meter compatibility table, community EM330/EM340
> emulator projects) points at the **EM340**, so that's what's implemented.
> If a different/newer model was actually meant, say so and the register
> map can be adjusted.

## Why this exists

Wallbox chargers with Power Boost / Eco-Smart / dynamic load balancing read
a physical energy meter over Modbus RTU to know how much of the site's
electrical capacity is already in use. In Sweden, the same information
(per-phase voltage, current, active/reactive power, cumulative energy) is
already available locally from the utility meter's **H1-port** (also called
the P1 or HAN port), in near real time, per the Energiforetagen
branschrekommendation. This project bridges the two: it presents itself to
the Wallbox (via the gateway) as a genuine EM340, and fills its registers
from whatever a P1/HAN reader already publishes -- no extra CT clamps or a
second physical meter required.

## Repository layout

```
src/em340_emu/            the library + CLI
  codec.py                 CRC16 and EM300-series word/byte packing
  model.py                 MeterState: the live electrical values
  registers.py              EM340 register map -> MeterState
  modbus.py                 Modbus PDU (FC03/04/06/08) handling
  framing.py                 RTU-over-TCP and Modbus-TCP (MBAP) framing
  server.py                  asyncio TCP client dialing out to the gateway (the "gateway-facing" side)
  sources.py                 flat-dict-of-readings -> MeterState
  parameters.py               canonical P1/HAN parameter list (key/unit/label/OBIS)
  mqtt_source.py               MQTT-subscribed live-value source
  ams_bridge.py                 AMS-to-MQTT bridge firmware schema -> canonical keys
  failsafe.py                   stale-source watchdog: ramps to a safe fallback
  cli.py                      `em340-emu` command line tool
tests/                     pytest suite (no Home Assistant dependency)
custom_components/em340_emu/  Home Assistant integration (own, separate test suite)
docs/                       (reserved for register map notes/extensions)
```

## How it talks to the gateway

`em340-emu serve` dials *out* to the RS485-to-Ethernet gateway sitting
between it and the Wallbox, rather than listening for the gateway to dial
in -- every gateway actually used with this project has turned out to be
a TCP server in its own right (check the gateway's own web UI for
something like "Work Mode: TCP Server"). `--host`/`--port` are the
gateway's own address. By default it **retries forever, every 15 seconds**
(`--retry-interval`) -- there's no good reason for a persistent service to
ever stop trying to reach a gateway that's meant to always be there, both
on first start and if the connection later drops (e.g. after a gateway
reboot). Pass `--connect-retry <seconds>` to give up and exit after that
many seconds instead, e.g. for a one-shot/scripted use. Two wire formats
are supported for whatever the gateway forwards from the Wallbox (the
RS485 master):

* **`rtu`** -- the gateway forwards raw Modbus RTU bytes (address, function
  code, data, 2-byte CRC) unmodified. This is the common "transparent
  serial bridge" mode.
* **`tcp`** -- the gateway itself speaks Modbus TCP (7-byte MBAP header, no
  CRC).
* **`auto`** (default) -- guessed per connection. A byte stream that parses
  as a valid CRC16-checked RTU frame is trusted over the (weaker, and for
  register 0x0000 genuinely ambiguous) MBAP structural heuristic; falls
  back to assuming RTU after ~32 bytes with no clear signal, since that's
  the more common gateway mode. Prefer setting `--framing` explicitly once
  you know your gateway's mode.

## Register map

Implements the Carlo Gavazzi "EM300 Series and ET300 Series Communication
Protocol" (v2 rev.17, 2021-07-05), physical (word) addressing:

* Table 2.4-1 -- instantaneous variables and meters, grouped by variable
  type (word addresses `0x0000`-`0x0053`).
* Table 2.6-1 -- the same data, grouped by phase (`0x00F6`-`0x01B5`).
* Table 2.7-1 -- firmware version/revision (`0x0302`/`0x0303`).
* `0x2004` -- max words readable per request (`50`), also confirmed at
  this address by the community reference below.
* `0x000B` -- the Carlo Gavazzi identification code (`341`, "EM340-DIN AV2
  3 X S1 X"), resolved *contextually* against its overlap with `V L3-L1`'s
  high word (see below).
* The measurement mode register (`0x1103`), fixed to `1` ("B"). The
  emulator is always the **PF.B (bidirectional)** variant: signed
  active/reactive power and separate import/export energy totals, needed
  for Eco-Smart / solar-aware load balancing. A write to this register
  (e.g. from a setup tool trying to force bidirectional mode) is accepted
  and echoed back rather than rejected, but never changes anything, since
  the emulator is already always PF.B.
* Table 2.9-9 (serial number, `0x5000`-`0x5006`, one ASCII letter per word)
  and table 2.9-10 (production year, `0x5010`). Added after a real Wallbox
  charger's own request log (via `sniff --emulate`) showed it reading
  `addr=0x5000 count=7` -- the serial number -- as part of its meter
  detection sequence, right after successfully reading the identification
  code; without this it got an illegal-address exception there and didn't
  proceed to reading live measurements.

Every address inside the two main tables' documented envelope is readable
(undocumented/"n.a." fields return `0`, matching real device behaviour).
Run `em340-emu registers` to print every specifically-implemented register.

**Courtesy mode (default) vs. strict mode.** Any address not specifically
implemented above -- not just the documented envelope gaps, genuinely any
address at all -- reads back as `0` by default rather than raising a
Modbus exception. This mirrors a proven-working real-world Carlo Gavazzi
EM1xx/EM3xx emulator (the sibling `wallbox-powerboost-emulator` project's
EM112 bridge), whose own README states plainly: *"Unknown registers
return 0 in courtesy mode. Strict mode returns exception `0x02` for
illegal address."* The
alternative -- exceptions for every address we haven't specifically
anticipated -- means discovering, one at a time via traffic captures,
every register a given charger's detection routine happens to probe
(exactly how `0x5000` and `0x1103` were found here). Pass `--strict` to
`serve` or `sniff --emulate` to restore exceptions for genuinely
unimplemented addresses (e.g. for protocol-conformance testing); the
underlying `RegisterMap(strict=True)` constructor argument does the same
for library use.

**Export energy totals: `0x004E`/`0x0050`, not `0x0050`/`0x0052`.** These
were originally placed at `0x0050` (kWh(-) TOT) / `0x0052` (kvarh(-) TOT)
based on
[docs.smart-stuff.nl's EM330/EM340 emulation reference](https://docs.smart-stuff.nl/p1-modbus-dongle/firmware-v1-legacy/register-mapping/em330-em340-emulation.md),
since the official protocol PDF's OCR seemed ambiguous there. A direct
re-read of the actual PDF table (not OCR) shows the real addresses are one
register-pair lower: `0x004E` (kWh(-) TOT) and `0x0050` (kvarh(-) TOT).
This was confirmed independently by a 15-minute live capture against a
real Wallbox: it polls exactly `addr=0x004E count=2` every single cycle,
without fail, right alongside `V L1-N`/per-phase `W`/`kWh(+) TOT` -- which
the earlier (wrong) mapping silently answered with `0` via courtesy mode,
meaning exported/solar energy was invisible to the Wallbox the whole time.
Table 2.6-1's equivalent addresses (`0x0116`/`0x0118`) were unaffected.

**The `0x000B` overlap.** The official protocol doc documents `0x000B` as
both the second word of `V L3-L1` (table 2.4-1) and, separately, the
identification code (table 2.8-1) -- these can't both be true of a single
physical register on real hardware. A real Wallbox charger's own request
log (captured via `serve --log-level DEBUG`, see below) showed it issuing
a standalone `FC03 read addr=0x000B count=1` -- i.e. it does probe the
identification code in isolation, contradicting the community reference's
choice to leave it out entirely. Rather than picking one meaning and
breaking the other, `RegisterMap.read()` resolves `0x000B` contextually:
a request that also covers `0x000A` (e.g. reading the full `V L3-L1`
field, or a wider block spanning both) gets `V L3-L1`'s real value; a
request for `0x000B` on its own gets the identification code. A real
EM340 chip can't do this (a physical register holds one fixed value
regardless of how it's read), but since this emulator isn't bound to
replicate that ambiguity, both access patterns get a correct answer
instead of one clobbering the other.

Known simplifications, all because a P1/HAN feed doesn't carry this level
of detail:

* Line-to-line voltages (`V L1-L2`, `V L2-L3`, `V L3-L1`) *are* implemented,
  at their correct table 2.4-1 addresses (`0x0006`/`0x0008`/`0x000A`) -- but
  all three currently return the same approximated value (phase-neutral
  average x sqrt(3), assuming a balanced 3-phase system), not independently
  measured line-to-line voltages, since a P1/HAN feed doesn't provide those.
* Per-phase cumulative energy (`kWh(+) L1/L2/L3`) is an even 3-way split of
  the system total, since Sweden's H1-port only exposes energy at the
  system level.
* Apparent power and power factor are derived from active + reactive power.
* Frequency defaults to 50.0 Hz unless a source provides it.
* Demand values (`kW dmd`, `kW dmd peak`) are not integrated over a real
  demand window; peak is simply the running max of instantaneous power
  since the process started.

## Installing the library + CLI

```bash
pip install -e .          # from a checkout of this repo
# or, once published:
pip install em340-emu
```

## CLI usage

By default, `serve` gets its live values from **MQTT**: it subscribes to a
broker/topic filter where a P1/HAN reader is expected to already be
publishing parsed readings as JSON (`192.168.200.142`, topic filter
`energy-meter/#`, by default -- override with
`--mqtt-host`/`--mqtt-port`/`--mqtt-topic`, or `--no-mqtt` to disable it).
`--demo` and `--values` both take priority over MQTT when given, for
testing without a broker.

```bash
# Default: live values from MQTT (192.168.200.142, topic "energy-meter/#"),
# with auth if the broker requires it. --host/--port are the gateway's own
# address (serve dials out to it -- see "How it talks to the gateway" above):
em340-emu serve --host 192.168.200.7 --port 12345 --mqtt-username ha_mqtt --mqtt-password ha_mqtt

# A different broker/topic:
em340-emu serve --host 192.168.200.7 --port 12345 --mqtt-host 10.0.0.5 --mqtt-topic han/readings \
                 --mqtt-username user --mqtt-password secret

# Try it with no external data source at all -- a small self-contained
# simulated EV-charging load ramp on L1:
em340-emu serve --host 192.168.200.7 --port 12345 --demo

# Point at a JSON file that something else keeps up to date instead of
# MQTT; reloaded automatically whenever its mtime changes:
em340-emu serve --host 192.168.200.7 --port 12345 --values /var/lib/em340-emu/values.json --framing rtu

# Watch every known P1/HAN parameter as it's broadcast over MQTT, without
# running the Modbus emulator at all -- handy for checking the payload
# format actually matches what em340-emu expects:
em340-emu view-readings

# Print the full implemented register map:
em340-emu registers

# Passively check whether an RS485-to-Ethernet gateway is forwarding any
# bus traffic at all -- connects and just listens, answers nothing, decodes
# any valid Modbus RTU frames it sees. Retries the connection itself for up
# to 5 minutes by default, e.g. for a gateway that's only powered up by (and
# thus only reachable once) the charger it's wired to has itself finished
# booting -- so you can start sniffing before that happens and still catch
# a brief startup probe instead of needing perfect timing:
em340-emu sniff --host 192.168.200.7 --port 12345
em340-emu sniff --host 192.168.200.7 --port 12345 --duration 30  # stop listening after 30s instead of Ctrl+C
em340-emu sniff --host 192.168.200.7 --port 12345 --connect-retry 600  # keep trying to connect for up to 10 minutes

# `sniff --emulate` is the same dial-out connection as plain sniffing, but
# actually answers as an EM340 over it instead of only logging -- useful
# for testing end-to-end against a charger without running the full `serve`
# command (no MQTT/fail-safe, just the register responses):
em340-emu sniff --host 192.168.200.7 --port 12345 --emulate --unit-id 2
em340-emu sniff --host 192.168.200.7 --port 12345 --emulate --unit-id 2 --demo  # with a changing demo load too
```

Two MQTT payload shapes are understood, and can be mixed freely across the
messages a broker delivers:

1. **The AMS-to-MQTT bridge firmware's schema** (see `em340_emu.ams_bridge`)
   -- confirmed against a real device publishing under `energy-meter/power`
   (`{"P1":47.0,"U1":233.2,"I1":0.5,"PO1":0.0,"Q":0,"QO":387,...}`) and
   `energy-meter/energy` (`{"tPI":24208.34,"tPO":...,"tQI":...,"tQO":...}`).
   This is why the default topic is a wildcard (`energy-meter/#`) rather
   than a single flat topic: the real data lives on subtopics. Per-phase
   reactive power isn't published by this firmware (only a system total),
   so it's approximated by splitting the total evenly across phases.
2. **A flat JSON object already using the canonical key vocabulary** from
   `em340_emu.sources` / `em340_emu.parameters` (mirrors the Swedish
   H1-port / OBIS parameter names -- see the table below), e.g.
   `{"voltage_l1": 231.4, "current_l1": 6.2}` on any topic. `--values`
   uses this same JSON shape:

```json
{
  "voltage_l1": 231.4, "voltage_l2": 229.8, "voltage_l3": 230.5,
  "current_l1": 6.2, "current_l2": 0.1, "current_l3": 0.1,
  "active_power_import_l1": 1400.0, "active_power_export_l1": 0.0,
  "energy_active_import": 12345.6, "energy_active_export": 3.2
}
```

## Running the tests

```bash
pip install -e ".[dev]"
pytest
```

The Home Assistant integration has its own, separate test suite under
`custom_components/em340_emu/tests/`, using the real Home Assistant test
harness rather than mocks:

```bash
pip install homeassistant pytest-homeassistant-custom-component
pytest -p homeassistant custom_components/em340_emu/tests
```

It's kept out of the default `pytest` run and its own `-p homeassistant`
flag is required because installing `pytest-homeassistant-custom-component`
registers a *global* pytest plugin (entry point name `homeassistant`) that
unconditionally blocks real sockets for every test in the environment --
which would otherwise break this package's own asyncio server tests. The
root `pyproject.toml` disables that plugin by default (`-p no:homeassistant`)
so a plain `pytest` keeps working after installing the HA test deps; the
command above re-enables it just for the HA suite.

## Home Assistant integration

`custom_components/em340_emu/` maps existing Home Assistant sensor
entities (from whatever already reads your P1/HAN port -- a DSMR reader, an
ESPHome HAN-to-MQTT bridge, etc.) onto the emulator, and runs it directly
inside Home Assistant's own event loop (no separate process). It always
dials *out* to your RS485-to-Ethernet gateway rather than listening for it
to dial in, since every gateway actually used with this project has
turned out to be a TCP server in its own right (check the gateway's own
web UI for something like "Work Mode: TCP Server").

| Field group | H1-port parameters (Bilaga 3) |
| --- | --- |
| Voltages & currents | Phase voltage L1/L2/L3, phase current L1/L2/L3 |
| Active power | Active power L1/L2/L3 import/export |
| Cumulative energy | Cumulative active energy import/export |

The setup flow only asks for these -- trimmed to exactly what two live
Wallbox captures showed getting read (see "Confirmed via live capture"
below); there's no reactive power/energy or frequency step, since neither
was ever polled. The underlying register map still implements them (in
case some other Wallbox firmware ever does read them), they just default
to `0`/`50Hz` without needing a P1/HAN entity mapped to them.

**Install via HACS:** add `https://github.com/merbanan/homeassistant_em340_emu`
as a custom repository (category: Integration), then install "EM340 Modbus
Emulator" and restart Home Assistant. HACS resolves `manifest.json`'s
`requirements` entry -- a git-tag-pinned URL, since the `em340-emu` library
isn't on PyPI -- automatically; see "Releasing an update" below for why
that tag matters.

**Manual install (no HACS):** copy `custom_components/em340_emu/` into
your Home Assistant config directory, `pip install -e /path/to/modbus-emu`
into the same Python environment yourself, then restart.

Either way, once installed: add the integration from Settings -> Devices
& Services. The config flow asks for:

* **Gateway address / port** -- the RS485-to-Ethernet gateway's own
  address, since this integration dials out to it.
* **Retry interval** -- how often to retry the connection (default 15s),
  both on first setup and if it later drops (e.g. to catch a gateway that
  only powers up when a charger starts). It always retries *forever* --
  there's no give-up option here, since a persistent integration should
  never stop trying to reach a gateway that's meant to always be there.
* **Unit id / framing** -- the Modbus slave id to answer as, and the wire
  framing (auto-detect works for most gateways).

Then it walks through mapping each parameter to a sensor entity (all
optional -- leave a field blank to leave that value at its default), then
the fail-safe settings (see below). All of these can be changed later
from the integration's "Configure" option.

The component has been exercised against a real Home Assistant instance
(via `pytest-homeassistant-custom-component`, see "Running the tests"
above) -- config flow, entity-mapping/unit-conversion, live state updates,
the embedded Modbus server actually answering a request over the wire, and
the fail-safe engaging/ramping/recovering are all covered by that suite,
not just syntax-checked.

### Observing it: entities and a dashboard card

The integration creates a device (named after the config entry) with a
sensor entity for every value it serves out over Modbus -- per-phase
voltage/current/active+reactive power/power factor, system totals,
frequency, and cumulative energy -- plus three diagnostic sensors:
"Modbus requests from gateway", "Modbus responses answered", and "Entity
value updates received", so you can confirm the Wallbox is actually
polling and your P1/HAN mapping is actually receiving updates without
digging through debug logs. All of these update live (no polling delay
worse than ~2 seconds).

There's also a **"Data flow healthy" light**: green while both (a) the
Wallbox has read a register from us within the last 10 seconds (steady-
state polling is every ~0.4-0.9s, see "Confirmed via live capture" below,
so 10s comfortably absorbs any brief hiccup) and (b) the fail-safe hasn't
had to engage, i.e. the P1/HAN entities are updating recently enough
(governed by the fail-safe's own configurable timeout, default 60s -- see
"Fail-safe" below). Red if either condition fails -- a single glance tells
you whether the whole chain (P1 meter -> Home Assistant -> emulator ->
Wallbox) is actually working, not just whether Home Assistant itself is up.

The quickest way to see all of these: go to the integration's device page
(Settings -> Devices & Services -> EM340 Modbus Emulator -> the device) --
every entity is listed there automatically, no dashboard setup needed.

To pin a summary to a dashboard instead, add a manual card with something
like:

```yaml
type: entities
title: EM340 Emulator
entities:
  - entity: light.em340_emulator_192_168_200_7_12345_data_flow_healthy
  - entity: sensor.em340_emulator_192_168_200_7_12345_energy_active_import
  - entity: sensor.em340_emulator_192_168_200_7_12345_energy_active_export
  - entity: sensor.em340_emulator_192_168_200_7_12345_current_l1
  - entity: sensor.em340_emulator_192_168_200_7_12345_current_l2
  - entity: sensor.em340_emulator_192_168_200_7_12345_current_l3
```

The exact entity ids depend on your config entry's title (gateway
host:port) -- check Settings -> Devices & Services -> Entities, filter by
"em340", and adjust the list above to match.

### Releasing an update

Since `em340-emu` isn't on PyPI, `manifest.json`'s `requirements` entry
installs it straight from this repo, pinned to a **git tag**:
```
em340-emu @ git+https://github.com/merbanan/homeassistant_em340_emu.git@v0.1.0#subdirectory=.
```
A plain `@main` reference would *not* reliably pick up new commits --
HACS/HA decide whether to reinstall a requirement by comparing the
requirement *string* in `manifest.json` against what's already installed,
not by checking whether the remote branch moved. So the tag is what
actually forces an update. When you change anything in `src/em340_emu/`
(the library) or want HACS to notice a new integration version at all,
bump the version **in both places, kept in sync**, then tag and push:

1. `pyproject.toml`: bump `version`.
2. `custom_components/em340_emu/manifest.json`: bump `version` to the
   same number, *and* update the `requirements` URL's `@vX.Y.Z` to match.
3. `git tag vX.Y.Z && git push origin main --tags`.

HACS shows an update is available once it sees the new `manifest.json`
`version`; installing that update is what actually triggers HA to
reinstall the pip requirement against the new tag.

## Fail-safe

Without it, the emulator would just keep reporting whatever values it was
last told, forever, if the P1/HAN source goes quiet (broker down, reader
offline, network blip) -- a charger could keep acting on now-stale
"everything's fine, lots of headroom" numbers. Both `serve` and the Home
Assistant integration arm a watchdog by default:

* If no live update arrives within the **timeout** (`--failsafe-timeout`,
  default 60s; or the "Fail-safe" config-flow step's "Timeout" field), the
  watchdog engages.
* Once engaged, active power import (per phase) ramps *linearly* towards a
  configured **limit** (`--failsafe-import-limit`, default 11000W total;
  should be set to your installation's actual main fuse rating) and active
  power export (assumed solar) ramps towards 0, both over 30 seconds --
  not an instant jump, so a charger backs off smoothly rather than seeing
  a sudden step change.
* The moment a fresh update arrives, it recovers immediately (no ramp back
  down needed, since the live source resumes dictating those values).
* Voltage, current, reactive power and energy totals are left exactly as
  they were; only active power import/export are touched.
* A timeout of `0` (or negative) disables the watchdog entirely.

See `src/em340_emu/failsafe.py` (`FailSafeMonitor`) for the implementation
-- it's transport-agnostic and used identically by the CLI and the Home
Assistant integration.

## Watching what a real master actually reads

Wallbox's meter model is selected manually in its own configuration (not
auto-detected), so whatever "test/detect meter" step it has almost
certainly just checks that expected registers respond without a Modbus
exception and look physically plausible -- not a device-identity register.
That's inference from documentation and community emulators, though, not
an observation of a specific Wallbox unit. To check directly, run with
debug logging and watch exactly which registers get read, in what order,
and whether any come back as an exception:

```bash
em340-emu serve --host 192.168.200.7 --port 12345 --log-level DEBUG
```

```
DEBUG em340_emu.server: unit=1 FC03 read addr=0x0000 count=2 -> ok
DEBUG em340_emu.server: unit=1 FC03 read addr=0x000B count=1 -> ok
DEBUG em340_emu.server: unit=1 FC03 read addr=0x9999 count=1 -> exception 0x02
```

This is deliberately left at DEBUG level (one line per request) rather than
always-on, since a charger polling every second or so would otherwise
flood normal operational logs.

That requires `serve` to already be answering requests, though. If you
just want to confirm the gateway itself is forwarding *any* bus traffic at
all -- e.g. before `em340-emu` is even configured correctly, or to catch a
charger's brief RS485 probe during its own startup -- use `em340-emu sniff`
instead (see above); it connects passively, answers nothing, and just logs
and decodes whatever arrives.

**Bytes that never form a valid Modbus RTU frame** (line noise, a gateway
or bus artifact, a corrupted response, etc.) are always silently ignored
by `RTUFramer` -- they can never crash or wedge the connection -- and are
additionally logged at DEBUG level (`em340_emu.framing`) when running
`serve --log-level DEBUG`, so they're visible for troubleshooting without
being treated as an error:

```
DEBUG em340_emu.framing: ignored 3 unrecognized byte(s) before a valid frame: 8c00fc
```

### Confirmed via live capture: exactly what this Wallbox reads

Two passive `sniff` captures against a real Wallbox+gateway (15 minutes of
steady-state traffic, then 20 minutes spanning a live Wallbox restart)
give a complete, empirical picture -- not inference -- of every register
this specific unit has ever been observed reading on the unit id it
answers on:

* **Steady-state polling**, repeating every ~0.4-0.9s for the entire
  session: `addr=0x0000 count=18` (voltages + currents), `addr=0x0012
  count=6` (per-phase active power), `addr=0x0034 count=2` (`kWh(+) TOT`),
  `addr=0x004E count=2` (`kWh(-) TOT`).
* **A one-time detection sequence**, seen only once per session -- right
  at connect, and again (still on the *same* TCP/RS485 session, which
  never dropped) the moment the Wallbox itself restarted mid-capture: an
  isolated `addr=0x000B count=1` (identification code) immediately
  followed by `addr=0x5000 count=7` (serial number) and `addr=0x1103
  count=1` (measurement mode), all within ~60ms of each other, then
  straight into the steady-state cycle above.
* **A slave-id scan on every restart**, targeting at least unit id 2 (this
  emulator only answers unit id 1): repeated bursts of `0x000B` and
  `0x4002` (table 2.9-8's "reset of run hour meter", read rather than
  written -- likely just probing for a response, not actually invoking the
  reset) over about 48 seconds, then abandoned. Since nothing answers unit
  id 2, this never interrupts the unit id 1 session running concurrently
  -- confirms the earlier decision to keep this emulator on unit id 1 and
  ignore other unit ids is correct and robust across a real restart, not
  just under normal operation.
* Each unanswered unit id 2 query is followed by a few stray 3-4 byte
  fragments that never form a valid CRC-checked frame -- consistent with
  RS485 line noise/reflection after a query nothing answers, already
  handled by `RTUFramer`'s garbage-skip (see above) without any special
  casing needed.

**Minimum P1/HAN entities actually required**, based strictly on the
above -- and, since this Wallbox's own confirmed behavior is the basis for
it, exactly what the HA integration's setup flow asks for (see "Home
Assistant integration" above; there's no reactive power/energy or
frequency step at all anymore):

* `voltage_l1`, `voltage_l2`, `voltage_l3`
* `current_l1`, `current_l2`, `current_l3`
* `active_power_import_l1/l2/l3` (and the matching `_export_l1/l2/l3`
  fields if the site ever exports/has solar, so net `W L1/L2/L3` comes out
  signed correctly)
* `energy_active_import`
* `energy_active_export` (this is exactly what was silently broken until
  the `0x004E` fix above)

The identification code, serial number, and measurement-mode registers
need no P1/HAN entity at all -- they're fixed constants the library
already answers correctly. Reactive power/energy and frequency are no
longer collected by the config flow, but the underlying register map
still implements them (table 2.4-1/2.6-1, in full) in case that assumption
about this Wallbox's firmware/configuration ever needs revisiting --
unmapped, they just default to `0`/`50Hz` rather than being unavailable.

## Wallbox meter profile

Per Wallbox's own Energy Meters Installation Guide, `EM340` (PF.A or PF.B)
is compatible with both Power Boost and Eco-Smart (PF.B only for
Eco-Smart). This emulator always presents itself as **PF.B**, since that's
the strictly more capable variant (bidirectional, required for Eco-Smart,
and a superset of what PF.A needs for Power Boost). Select the EM340 meter
profile in the Wallbox charger's configuration.

## License

MIT
