#!/usr/bin/env python3
"""
cyclone_bms.py - read Cyclone CYG-B48V-100A LiFePO4 batteries through a Moxa NPort.

The battery's BMS is a PACE BMS (BmsTools.exe is a rebadged PACE PbmsTools). It talks PACE
protocol v2.5, an ASCII YD/T 1363.3 dialect (VER 0x25), at 9600 8N1 on its RS232 (RJ11)
and RS485 (RJ45) ports. The NPort must be in TCP Server mode; this script opens a raw TCP
socket to it (port 4001 by default).

Paralleled packs (linked on their RS485 ports) are all reachable through the master's RS232
port (PACE: master DIP address 1, the others 2, 3, ...; 0 means standalone). Firmware
differs in how it answers, so there are three modes; --probe shows which ones work:
  -a 1 --bank --packs 3   one INFO=0xFF request to the master returns every pack
  -a 1,2,3                one request per DIP address (what BmsTools "Multi Monitoring" does)
  -a 1,2,3 --relay        every request to the master, INFO selects the pack

LibreNMS runs it with --librenms (LibreNMS/Device/CycloneBattery.php) to turn each battery
into native sensors on the NPort's device: charge, voltage, current, temperature, cycle
count and BMS state sensors, with limits that the default alert rules act on.

Modes:
  (default)   one-line summary; exit 0 OK, 1 WARNING, 2 CRITICAL, 3 UNKNOWN
  --json      everything decoded, as JSON
  --librenms  the sensor set LibreNMS discovers and polls, as JSON
  --probe     try address / bank variants and print the raw frames (first bring-up)

Python 3.8+, standard library only. Read-only: it only sends "get" commands.

Protocol references:
  https://github.com/nkinnan/esphome-pace-bms (pace_bms_protocol_v25.cpp/.h, protocol PDFs)
  https://github.com/syssi/esphome-pace-bms/tree/main/docs
  https://github.com/Tertiush/bmspace (multi-pack INFO=0xFF parsing)
"""

import argparse
import json
import socket
import sys
import time

VERSION = "2.0.0"

OK, WARNING, CRITICAL, UNKNOWN = 0, 1, 2, 3
STATUS_NAMES = {OK: "OK", WARNING: "WARNING", CRITICAL: "CRITICAL", UNKNOWN: "UNKNOWN"}

VER_PACE = 0x25
CID1_BATTERY = 0x46

CID2_ANALOG = 0x42
CID2_ALARM = 0x44
CID2_VERSION = 0xC1
CID2_SERIAL = 0xC2
CID2_PACK_COUNT = 0x90

INFO_ALL_PACKS = 0xFF
CELLS_PER_PACK = 16

RTN_CODES = {
    0x00: "normal",
    0x01: "VER / undefined error",
    0x02: "CHKSUM error",
    0x03: "LCHKSUM error",
    0x04: "CID2 not supported",
    0x05: "command format error",
    0x06: "invalid data",
    0x09: "operation error",
    0x90: "ADR error",
    0x91: "internal communication error",
}

# 0x44 status bytes. Bit meanings follow nkinnan/esphome-pace-bms, which matches real
# PACE traffic (the PACE PDF disagrees on a few "system state" bits).
PROTECT1_BITS = {
    0: "cell over-voltage",
    1: "cell under-voltage",
    2: "pack over-voltage",
    3: "pack under-voltage",
    4: "charge over-current",
    5: "discharge over-current",
    6: "short circuit",
    7: "charger over-voltage",
}
PROTECT2_BITS = {  # bit 7 is the "fully charged" status flag, not a protection
    0: "charge over-temp",
    1: "discharge over-temp",
    2: "charge under-temp",
    3: "discharge under-temp",
    4: "MOSFET over-temp",
    5: "ambient over-temp",
    6: "ambient under-temp",
}
PROTECT2_FULLY_CHARGED = 7
SYSTEM_BITS = {
    0: "charge current limiter off",
    1: "charge MOSFET on",
    2: "discharge MOSFET on",
    3: "discharging",
    4: "terminals reversed",
    5: "charging",
    6: "AC in",
    7: "heater on",
}
CONFIG_BITS = {
    0: "buzzer enabled",
    1: "charge MOSFET forced off",
    2: "discharge MOSFET forced off",
    3: "current limit low gear",
    4: "charge current limiter enabled",
    5: "LED alarm enabled",
    6: "static balance",
}
FAULT_BITS = {
    0: "charge MOSFET fault",
    1: "discharge MOSFET fault",
    2: "NTC (temp sensor) fault",
    3: "communication fault",
    4: "cell fault",
    5: "sampling fault",
    6: "CCB fault",
    7: "heater fault",
}
WARN1_BITS = {
    0: "cell over-voltage",
    1: "cell under-voltage",
    2: "pack over-voltage",
    3: "pack under-voltage",
    4: "charge over-current",
    5: "discharge over-current",
}
WARN2_BITS = {
    0: "charge over-temp",
    1: "discharge over-temp",
    2: "charge under-temp",
    3: "discharge under-temp",
    4: "ambient over-temp",
    5: "ambient under-temp",
    6: "MOSFET over-temp",
    7: "low SOC",
}
LEVEL_WARNINGS = {0x01: "below lower limit", 0x02: "above upper limit", 0xF0: "other fault"}

# --librenms: sensor_type for numeric sensors, and the state names (LibreNMS state indexes are
# global, so they carry the same prefix; CycloneBattery.php recognises its sensors by it).
SENSOR_TYPE = "cyclone-bms"

# Fixed limits as (low, low_warn, warn, high). LibreNMS guesses limits for voltage and
# temperature sensors created without them (temperature low = reading - 10 C, voltage +-15%),
# so every voltage and temperature sensor gets explicit limits on both sides.
PACK_VOLTAGE_LIMITS = (46.0, 48.0, 56.8, 57.6)  # BMS protects at 43.2 / 58.4 V
CELL_VOLTAGE_LIMITS = (2.90, 3.00, 3.55, 3.60)  # BMS protects at 2.5 / 3.7 V
CURRENT_LIMITS = (-95.0, -80.0, 80.0, 95.0)     # rated 100 A charge and discharge
MOSFET_TEMP_LIMITS = (-20.0, None, 70.0, 85.0)
CYCLE_LIMITS = (None, None, 5000, 6000)         # rated 6000 cycles

GENERIC_OK, GENERIC_WARNING, GENERIC_CRITICAL = 0, 1, 2
MULTIPLE = 99
LEVEL_WARNING_CODE = 50


def _state_table(normal, names, generic, extra=()):
    rows = [{"value": 0, "descr": normal, "generic": GENERIC_OK}]
    rows += [{"value": i, "descr": n[0].upper() + n[1:], "generic": generic} for i, n in enumerate(names, 1)]
    rows += list(extra)
    rows.append({"value": MULTIPLE, "descr": "Multiple", "generic": generic})
    return rows


# Codes are positions in these lists: append only, never reorder.
PROTECTION_NAMES = list(PROTECT1_BITS.values()) + list(PROTECT2_BITS.values())
FAULT_NAMES = list(FAULT_BITS.values())
WARNING_NAMES = list(WARN1_BITS.values()) + list(WARN2_BITS.values())

STATE_TABLES = {
    "cyclone-bms-status": [
        {"value": 0, "descr": "Idle", "generic": GENERIC_OK},
        {"value": 1, "descr": "Charging", "generic": GENERIC_OK},
        {"value": 2, "descr": "Discharging", "generic": GENERIC_WARNING},
        {"value": 3, "descr": "Not responding", "generic": GENERIC_CRITICAL},
    ],
    "cyclone-bms-protection": _state_table("Normal", PROTECTION_NAMES, GENERIC_CRITICAL),
    "cyclone-bms-fault": _state_table("Normal", FAULT_NAMES, GENERIC_CRITICAL),
    "cyclone-bms-warning": _state_table("Normal", WARNING_NAMES, GENERIC_WARNING, extra=[
        {"value": LEVEL_WARNING_CODE, "descr": "Cell or temperature level warning", "generic": GENERIC_WARNING}]),
    "cyclone-bms-mosfet": [
        {"value": 0, "descr": "Both on", "generic": GENERIC_OK},
        {"value": 1, "descr": "Charge off (full)", "generic": GENERIC_OK},
        {"value": 2, "descr": "Charge off", "generic": GENERIC_WARNING},
        {"value": 3, "descr": "Discharge off", "generic": GENERIC_CRITICAL},
        {"value": 4, "descr": "Both off", "generic": GENERIC_CRITICAL},
    ],
}


class BmsError(Exception):
    pass


def bits(value, names):
    return [name for bit, name in sorted(names.items()) if value & (1 << bit)]


# --------------------------------------------------------------------------------------
# Framing:  ~ VER ADR CID1 CID2 LENGTH INFO CHKSUM \r   (every byte sent as 2 hex chars)
# --------------------------------------------------------------------------------------

def length_field(info_chars):
    """LENGTH = 4-bit LCHKSUM + 12-bit LENID (number of ASCII chars in INFO)."""
    if info_chars == 0:
        return 0
    lsum = (info_chars & 0xF) + ((info_chars >> 4) & 0xF) + ((info_chars >> 8) & 0xF)
    return (((~lsum + 1) & 0xF) << 12) | info_chars


def checksum(body):
    """Sum of the ASCII chars between SOI and CHKSUM, mod 65536, two's complement."""
    return (~sum(body) + 1) & 0xFFFF


def encode_frame(adr, cid2, info=b"", ver=VER_PACE, cid1=CID1_BATTERY):
    info_hex = info.hex().upper()
    body = f"{ver:02X}{adr:02X}{cid1:02X}{cid2:02X}{length_field(len(info_hex)):04X}{info_hex}"
    return f"~{body}{checksum(body.encode()):04X}\r".encode()


def decode_frame(raw):
    """Validate a response frame. Returns dict(ver, adr, cid1, rtn, info: bytes)."""
    raw = raw.strip(b"\r\n\x00 ")
    start = raw.rfind(b"~")
    if start < 0:
        raise BmsError(f"no SOI '~' in response: {raw[:60]!r}")
    raw = raw[start + 1:]
    if len(raw) < 16:
        raise BmsError(f"response too short: {raw!r}")
    body, chk = raw[:-4], raw[-4:]
    try:
        want = int(chk, 16)
    except ValueError:
        raise BmsError(f"bad checksum field: {chk!r}")
    if checksum(body) != want:
        raise BmsError(f"checksum mismatch (frame says {chk.decode()}, computed {checksum(body):04X})")
    try:
        ver, adr, cid1, rtn = (int(body[i:i + 2], 16) for i in range(0, 8, 2))
        length = int(body[8:12], 16)
        info = bytes.fromhex(body[12:].decode())
    except ValueError as e:
        raise BmsError(f"malformed frame: {e}")
    lenid = length & 0x0FFF
    if length_field(lenid) != length:
        raise BmsError(f"LENGTH checksum mismatch ({length:04X})")
    if lenid != len(body) - 12:
        raise BmsError(f"LENID says {lenid} chars of INFO, frame has {len(body) - 12}")
    return {"ver": ver, "adr": adr, "cid1": cid1, "rtn": rtn, "info": info}


# --------------------------------------------------------------------------------------
# Transport: raw TCP to the NPort (TCP Server mode)
# --------------------------------------------------------------------------------------

class Link:
    def __init__(self, host, port, timeout, verbose=False, connect_attempts=3):
        self.host, self.port, self.timeout, self.verbose = host, port, timeout, verbose
        self.connect_attempts = max(1, connect_attempts)
        self.sock = None

    def __enter__(self):
        # The NPort takes one client at a time; a few retries ride over someone's quick manual
        # session instead of reporting every battery as not responding.
        last = None
        for attempt in range(self.connect_attempts):
            if attempt:
                time.sleep(2)
            try:
                self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
                return self
            except OSError as e:
                last = e
        raise BmsError(f"cannot connect to NPort {self.host}:{self.port} ({last}); "
                       "is Port 1 in TCP Server mode, and is nothing else connected?")

    def __exit__(self, *exc):
        if self.sock:
            self.sock.close()

    def _drain(self):
        """Throw away anything stale sitting in the NPort's buffer."""
        self.sock.settimeout(0.05)
        try:
            while self.sock.recv(4096):
                pass
        except OSError:
            pass

    def transact(self, frame):
        """Send one frame, return the raw reply bytes (possibly empty)."""
        self._drain()
        if self.verbose:
            print(f"TX {frame!r}", file=sys.stderr)
        self.sock.sendall(frame)
        buf = b""
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self.sock.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                raise BmsError("NPort closed the connection")
            buf += chunk
            if b"~" in buf and buf.endswith(b"\r"):
                break
        if self.verbose:
            print(f"RX {buf!r}", file=sys.stderr)
        return buf

    def query(self, adr, cid2, info=b"", retries=1):
        frame = encode_frame(adr, cid2, info)
        last = None
        for attempt in range(retries + 1):
            if attempt:
                time.sleep(0.3)
            raw = self.transact(frame)
            if not raw:
                last = BmsError("no response from BMS (check the serial cable, NPort baud rate "
                                "and interface, and the battery's DIP address)")
                continue
            try:
                resp = decode_frame(raw)
            except BmsError as e:
                last = e
                continue
            if resp["rtn"] != 0:
                raise BmsError(f"BMS returned RTN 0x{resp['rtn']:02X} "
                               f"({RTN_CODES.get(resp['rtn'], 'unknown')}) for CID2 0x{cid2:02X}")
            time.sleep(0.2)  # PACE wants a short gap between requests
            return resp
        raise last


# --------------------------------------------------------------------------------------
# Payload decoders
# --------------------------------------------------------------------------------------

class Reader:
    def __init__(self, data, pos=0):
        self.data, self.pos = data, pos

    def u8(self):
        if self.pos >= len(self.data):
            raise BmsError(f"payload truncated at byte {self.pos}")
        v = self.data[self.pos]
        self.pos += 1
        return v

    def u16(self):
        return (self.u8() << 8) | self.u8()

    def s16(self):
        v = self.u16()
        return v - 0x10000 if v & 0x8000 else v

    def rest(self):
        return self.data[self.pos:]


def _looks_like_analog(data, i=0):
    if i >= len(data) or not 1 <= data[i] <= 32:
        return False
    t = i + 1 + 2 * data[i]
    if t >= len(data) or not 1 <= data[t] <= 16:
        return False
    return all(1500 <= (data[j] << 8 | data[j + 1]) <= 4500 for j in range(i + 1, t, 2))


def _looks_like_alarm(data, i=0):
    if i >= len(data) or not 1 <= data[i] <= 32:
        return False
    t = i + 1 + data[i]
    if t >= len(data) or not 1 <= data[t] <= 16:
        return False
    return all(v in (0, 1, 2, 0xF0) or 0x80 <= v <= 0xEF for v in data[i + 1:t])


def _split_packs(info, looks_like, parse_block):
    """Multi-pack (INFO=0xFF) payload: INFOFLAG, pack count, then the pack blocks.

    Every verified capture (Revov x7, Eenovance x3, Enersol x2, Greenrich x2) has equal-size
    blocks back to back, each ending in the same firmware extension, so split by length.
    An all-zero block is a slave that didn't answer the master. If the blocks don't line up
    (some firmware claims 3 packs but sends 1), walk the payload and keep what parses."""
    count = info[1] if len(info) > 1 else 0
    body = info[2:]
    if count and len(body) % count == 0:
        size = len(body) // count
        blocks = [body[i * size:(i + 1) * size] for i in range(count)]
        if all(looks_like(b) or not any(b) for b in blocks):
            return [parse_block(Reader(b), True) if any(b) else {"error": "pack did not answer the master"}
                    for b in blocks]

    r = Reader(info, 2)
    packs = []
    for _ in range(count):
        skip = next((s for s in range(17) if looks_like(r.data, r.pos + s)), None)
        if skip is None:
            break
        r.pos += skip
        packs.append(parse_block(r, False))
    return packs


def parse_analog(info, multi=False):
    """CID2 0x42 reply. Returns a list of pack dicts (one unless multi=True)."""
    if multi:
        return _split_packs(info, _looks_like_analog, _analog_block)
    r = Reader(info, 1)
    echo = r.u8()  # pack address echo (a standalone pack may echo 1 whatever it was asked)
    pack = _analog_block(r, True)
    pack["addr_echo"] = echo
    return [pack]


def _analog_block(r, own_block):
    cells = [r.u16() for _ in range(r.u8())]
    # 0 = sensor not fitted (some packs report 8 slots with 2 unused)
    temps = [None if raw == 0 else round((raw - 2730) / 10, 1) for raw in [r.u16() for _ in range(r.u8())]]
    current = r.s16() / 100
    voltage = r.u16() / 1000
    remaining_ah = r.u16() / 100
    p = r.u8()  # "user defined items": 02/03/04/09 seen, but full/cycles/design always follow
    full_ah = r.u16() / 100
    cycles = r.u16()
    design_ah = r.u16() / 100

    # Newer PACE firmware appends its own SOC/SOH to each block: 1 byte (SOC), 10 bytes
    # (SOC, 4 x u16, SOH) or 14 bytes (SOC, 4 x u16, SOH, u16, u16).
    extra = r.rest() if own_block else b""
    soc_bms = extra[0] if len(extra) in (1, 10, 14) and extra[0] <= 100 else None
    soh_bms = extra[9] if len(extra) in (10, 14) and 0 < extra[9] <= 100 else None

    soc = soc_bms
    if soc is None and full_ah:
        soc = round(min(100.0, remaining_ah / full_ah * 100), 1)
    soh = soh_bms
    if soh is None and full_ah and design_ah:
        soh = round(min(100.0, full_ah / design_ah * 100), 1)

    # The last two sensors are MOSFET and ambient; the rest are cell sensors.
    if len(temps) >= 6:
        cell_slots, mos_temp, env_temp = temps[:-2], temps[-2], temps[-1]
    else:
        cell_slots, mos_temp, env_temp = temps, None, None

    return {
        "cells_mv": cells,
        "cell_min_mv": min(cells) if cells else None,
        "cell_max_mv": max(cells) if cells else None,
        "cell_delta_mv": max(cells) - min(cells) if cells else None,
        "temps_c": temps,
        "cell_temps_c": [t for t in cell_slots if t is not None],
        "mos_temp_c": mos_temp,
        "env_temp_c": env_temp,
        "current_a": current,
        "voltage_v": voltage,
        "power_w": round(current * voltage, 1),
        "remaining_ah": remaining_ah,
        "full_ah": full_ah,
        "design_ah": design_ah,
        "cycles": cycles,
        "soc_pct": soc,
        "soh_pct": soh,
        "soc_source": "bms" if soc_bms is not None else "calculated",
        "soh_source": "bms" if soh_bms is not None else "calculated",
        "p_byte": p,
        "extra_hex": extra.hex(),
    }


def parse_alarm(info, multi=False):
    """CID2 0x44 reply. Returns a list of pack dicts (one unless multi=True)."""
    if multi:
        return _split_packs(info, _looks_like_alarm, _alarm_block)
    return [_alarm_block(Reader(info, 2), True)]


def _alarm_block(r, own_block):
    cell_levels = [r.u8() for _ in range(r.u8())]
    temp_levels = [r.u8() for _ in range(r.u8())]
    chg_current, pack_voltage, dsg_current = r.u8(), r.u8(), r.u8()
    protect1, protect2, system, config, fault, bal1, bal2, warn1, warn2 = (r.u8() for _ in range(9))

    level_warnings = []
    for i, v in enumerate(cell_levels, 1):
        if v:
            level_warnings.append(f"cell {i} voltage {LEVEL_WARNINGS.get(v, f'code 0x{v:02X}')}")
    for i, v in enumerate(temp_levels, 1):
        if v:
            level_warnings.append(f"temp sensor {i} {LEVEL_WARNINGS.get(v, f'code 0x{v:02X}')}")
    for name, v in (("charge current", chg_current), ("pack voltage", pack_voltage),
                    ("discharge current", dsg_current)):
        if v:
            level_warnings.append(f"{name} {LEVEL_WARNINGS.get(v, f'code 0x{v:02X}')}")

    # PACE doc: Balance1 = cells 1-8, Balance2 = cells 9-16 (bit 0 = lowest cell).
    # Unverified on this pack; some PACE firmware swaps the two bytes.
    balance = (bal2 << 8) | bal1
    balancing = [i + 1 for i in range(len(cell_levels)) if balance & (1 << i)]

    return {
        "level_warnings": level_warnings,
        "protections": bits(protect1, PROTECT1_BITS) + bits(protect2, PROTECT2_BITS),
        "faults": bits(fault, FAULT_BITS),
        "warnings": bits(warn1, WARN1_BITS) + bits(warn2, WARN2_BITS),
        "system": bits(system, SYSTEM_BITS),
        "config": bits(config, CONFIG_BITS),
        "fully_charged": bool(protect2 & (1 << PROTECT2_FULLY_CHARGED)),
        "charge_mosfet_on": bool(system & 0x02),
        "discharge_mosfet_on": bool(system & 0x04),
        "charging": bool(system & 0x20),
        "discharging": bool(system & 0x08),
        "balancing_cells": balancing,
        "raw": {"protect1": protect1, "protect2": protect2, "system": system, "config": config,
                "fault": fault, "balance1": bal1, "balance2": bal2, "warn1": warn1, "warn2": warn2},
        "extra_hex": r.rest().hex() if own_block else "",
    }


def parse_text(info):
    return info.split(b"\x00")[0].decode("ascii", "replace").strip()


def parse_serials(info):
    return {"bms_sn": parse_text(info[:20]), "pack_sn": parse_text(info[20:40])}


# --------------------------------------------------------------------------------------
# Polling
# --------------------------------------------------------------------------------------

def poll(link, addresses, bank=False, relay=False, expected_packs=None, with_ids=False, min_packs=None):
    """Returns (packs, notes). packs is in a fixed order; a pack that couldn't be read is
    {"address": ..., "error": "..."} so the caller can still emit a stable value set.
    notes are bank-level warnings."""
    notes = []
    if bank:
        adr = addresses[0]
        info = bytes([INFO_ALL_PACKS])
        analog_info = link.query(adr, CID2_ANALOG, info)["info"]
        analog = parse_analog(analog_info, multi=True)
        alarms = parse_alarm(link.query(adr, CID2_ALARM, info)["info"], multi=True)
        reported = analog_info[1] if len(analog_info) > 1 else 0
        expected = expected_packs or max(reported, len(analog), min_packs or 1)
        if len(analog) > expected:
            notes.append(f"bank reply has {len(analog)} packs, expected {expected} (update --packs)")
            analog = analog[:expected]
        answered = sum("error" not in a for a in analog)
        packs = []
        for i in range(expected):
            pack = analog[i] if i < len(analog) else {
                "error": f"missing from the bank reply ({answered} of {expected} packs answered)"}
            if "error" not in pack:
                alarm = alarms[i] if i < len(alarms) else {"error": ""}
                if "error" in alarm:
                    pack = {"error": "missing from the alarm reply"}
                else:
                    pack["alarm"] = alarm
            pack["address"] = None
            packs.append(pack)
        id_targets = [(adr, packs[0])]
    else:
        packs = []
        for adr in addresses:
            # relay: ask the master (first address) for each pack's data
            target, info = (addresses[0] if relay else adr), bytes([adr])
            try:
                pack = parse_analog(link.query(target, CID2_ANALOG, info)["info"])[0]
                pack["alarm"] = parse_alarm(link.query(target, CID2_ALARM, info)["info"])[0]
            except BmsError as e:
                pack = {"error": str(e)}
            pack["address"] = adr
            packs.append(pack)
        id_targets = [(p["address"], p) for p in packs if "error" not in p and not relay] or \
                     [(addresses[0], packs[0])]

    if with_ids:
        for adr, pack in id_targets:
            for cid2, key, parser in ((CID2_VERSION, "version", parse_text),
                                      (CID2_SERIAL, "serials", parse_serials)):
                try:
                    pack[key] = parser(link.query(adr, cid2)["info"])
                except BmsError as e:
                    pack[key] = f"unavailable: {e}"
    return packs, notes


# --------------------------------------------------------------------------------------
# Evaluation and output
# --------------------------------------------------------------------------------------

def evaluate_pack(s, t):
    a = s["alarm"]
    crit, warn = [], []

    crit += [f"PROTECTION: {p}" for p in a["protections"]]
    crit += [f"FAULT: {f}" for f in a["faults"]]
    if not a["discharge_mosfet_on"]:
        crit.append("discharge MOSFET off (pack cannot carry the load)")
    if not a["charge_mosfet_on"] and not a["fully_charged"]:
        warn.append("charge MOSFET off")
    warn += [f"BMS warning: {w}" for w in a["warnings"]]
    warn += [f"BMS warning: {w}" for w in a["level_warnings"]]

    def low(value, label, w, c, unit):
        if value is None:
            return
        if value <= c:
            crit.append(f"{label} {fmt(value)}{unit} <= {fmt(c)}{unit}")
        elif value <= w:
            warn.append(f"{label} {fmt(value)}{unit} <= {fmt(w)}{unit}")

    def high(value, label, w, c, unit):
        if value is None:
            return
        if value >= c:
            crit.append(f"{label} {fmt(value)}{unit} >= {fmt(c)}{unit}")
        elif value >= w:
            warn.append(f"{label} {fmt(value)}{unit} >= {fmt(w)}{unit}")

    low(s["soc_pct"], "SOC", t.soc_warn, t.soc_crit, "%")
    low(s["soh_pct"], "SOH", t.soh_warn, t.soh_crit, "%")
    high(s["cell_delta_mv"], "cell imbalance", t.delta_warn, t.delta_crit, "mV")
    if s["cell_temps_c"]:
        high(max(s["cell_temps_c"]), "cell temp", t.temp_high_warn, t.temp_high_crit, "C")
        low(min(s["cell_temps_c"]), "cell temp", t.temp_low_warn, t.temp_low_crit, "C")
    return crit, warn


def fingerprint(s):
    return (tuple(s["cells_mv"]), tuple(s["temps_c"]), s["current_a"], s["remaining_ah"], s["cycles"])


def pack_label(i, s, multi):
    if not multi:
        return ""
    return f"pack {i}" + (f" (addr {s['address']})" if s.get("address") is not None else "") + ": "


def evaluate(packs, t, notes=()):
    """Return (status, problems, summary) for the polled packs and thresholds."""
    multi = len(packs) > 1
    good = [s for s in packs if "error" not in s]
    crit, warn = [], list(notes)

    for i, s in enumerate(packs, 1):
        label = pack_label(i, s, multi)
        if "error" in s:
            crit.append(f"{label}{s['error']}")
            continue
        c, w = evaluate_pack(s, t)
        crit += [label + x for x in c]
        warn += [label + x for x in w]

    seen = {}
    for i, s in enumerate(packs, 1):
        if "error" in s:
            continue
        fp = fingerprint(s)
        if fp in seen:
            crit.append(f"pack {i} returned exactly the same data as pack {seen[fp]}: one pack is "
                        "answering for every address (check the DIP switches, or try --bank / --relay)")
        else:
            seen[fp] = i

    status = CRITICAL if crit else WARNING if warn else OK
    return status, crit + warn, summarize(packs, good)


def bank_totals(good):
    full = sum(s["full_ah"] or 0 for s in good)
    if len(good) == 1:
        soc = good[0]["soc_pct"]
    else:
        soc = round(min(100.0, sum(s["remaining_ah"] for s in good) / full * 100), 1) if full else None
    current = round(sum(s["current_a"] for s in good), 2)
    power = round(sum(s["power_w"] for s in good), 1)
    return soc, current, power


def summarize(packs, good):
    if not good:
        return "no pack data"
    soc, current, power = bank_totals(good)
    if any(s["alarm"]["discharging"] for s in good):
        mode = "DISCHARGING"
    elif any(s["alarm"]["charging"] for s in good):
        mode = "charging"
    else:
        mode = "idle"
    cell_min = min(s["cell_min_mv"] for s in good)
    cell_max = max(s["cell_max_mv"] for s in good)
    delta = max(s["cell_delta_mv"] for s in good)
    temps = [x for s in good for x in s["cell_temps_c"]]
    voltage = max(s["voltage_v"] for s in good)

    if len(packs) == 1:
        s = good[0]
        text = f"SOC {fmt(s['soc_pct'])}% SOH {fmt(s['soh_pct'])}%, {voltage:.2f}V {current:+.2f}A {mode}"
    else:
        per = lambda key: "/".join(fmt(s.get(key)) if "error" not in s else "-" for s in packs)
        text = (f"{len(good)}/{len(packs)} packs, bank SOC {fmt(soc)}% (packs {per('soc_pct')}%), "
                f"SOH {per('soh_pct')}%, {voltage:.2f}V {current:+.2f}A {mode}")
    text += f", cells {cell_min / 1000:.3f}-{cell_max / 1000:.3f}V (delta {delta}mV)"
    if temps:
        text += f", {min(temps)}-{max(temps)}C"
    cycles = [s["cycles"] for s in good if s["cycles"] is not None]
    if cycles:
        text += f", {max(cycles)} cycles" if len(packs) == 1 else f", cycles {'/'.join(map(str, cycles))}"
    return text


def fmt(v):
    return "?" if v is None else (f"{v:g}" if isinstance(v, float) else str(v))


def mv_to_v(mv):
    return None if mv is None else round(mv / 1000, 3)


def code_for(names, table):
    """State value for a list of active conditions: 0 none, its code if one, MULTIPLE if more."""
    if not names:
        return 0
    return table.index(names[0]) + 1 if len(names) == 1 else MULTIPLE


def mosfet_code(a):
    if a["charge_mosfet_on"] and a["discharge_mosfet_on"]:
        return 0
    if not a["charge_mosfet_on"] and not a["discharge_mosfet_on"]:
        return 4
    if not a["discharge_mosfet_on"]:
        return 3
    return 1 if a["fully_charged"] else 2


def librenms_doc(packs, t, notes=(), error=None):
    """The complete sensor set for LibreNMS, one entry per sensor, in a fixed order.

    A pack that didn't answer still gets every sensor (value None, status "Not responding")
    so LibreNMS discovery never deletes sensors, and their history, during an outage."""
    single = len(packs) == 1
    temp_limits = (t.temp_low_crit, t.temp_low_warn, t.temp_high_warn, t.temp_high_crit)
    sensors = []
    for i, s in enumerate(packs, 1):
        ok = "error" not in s
        d = s if ok else {}
        a = d.get("alarm") or {}
        name = "Battery" if single else f"Battery {i}"
        temps = d.get("cell_temps_c") or []

        def add(cls, key, descr, value, limits=(None, None, None, None), stype=SENSOR_TYPE):
            low, low_warn, warn, high = limits
            sensors.append({"class": cls, "index": f"cyclone.{i}.{key}", "type": stype,
                            "descr": f"{name} {descr}", "value": value,
                            "low": low, "low_warn": low_warn, "warn": warn, "high": high})

        add("charge", "soc", "state of charge", d.get("soc_pct"), (t.soc_crit, t.soc_warn, None, None))
        add("charge", "soh", "state of health", d.get("soh_pct"), (t.soh_crit, t.soh_warn, None, None))
        add("voltage", "voltage", "voltage", d.get("voltage_v"), PACK_VOLTAGE_LIMITS)
        add("voltage", "cell_min", "lowest cell", mv_to_v(d.get("cell_min_mv")), CELL_VOLTAGE_LIMITS)
        add("voltage", "cell_max", "highest cell", mv_to_v(d.get("cell_max_mv")), CELL_VOLTAGE_LIMITS)
        add("voltage", "cell_delta", "cell imbalance", mv_to_v(d.get("cell_delta_mv")),
            (0.0, None, t.delta_warn / 1000, t.delta_crit / 1000))
        add("current", "current", "current", d.get("current_a"), CURRENT_LIMITS)
        add("temperature", "cell_temp_max", "cells (highest)", max(temps) if temps else None, temp_limits)
        add("temperature", "cell_temp_min", "cells (lowest)", min(temps) if temps else None, temp_limits)
        add("temperature", "mos_temp", "MOSFET", d.get("mos_temp_c"), MOSFET_TEMP_LIMITS)
        add("temperature", "env_temp", "ambient", d.get("env_temp_c"), temp_limits)
        add("count", "cycles", "cycles", d.get("cycles"), CYCLE_LIMITS)
        if t.cells:
            cells = d.get("cells_mv") or [None] * CELLS_PER_PACK
            for c, mv in enumerate(cells, 1):
                add("voltage", f"cell{c:02d}", f"cell {c}", mv_to_v(mv), CELL_VOLTAGE_LIMITS)

        status = 3 if not ok else 2 if a["discharging"] else 1 if a["charging"] else 0
        warnings = a.get("warnings", []) + (["level"] if a.get("level_warnings") else [])
        warning = None
        if ok:
            warning = (MULTIPLE if len(warnings) > 1 else LEVEL_WARNING_CODE if warnings == ["level"]
                       else code_for(warnings, WARNING_NAMES))
        add("state", "status", "status", status, stype="cyclone-bms-status")
        add("state", "protection", "protection", code_for(a["protections"], PROTECTION_NAMES) if ok else None,
            stype="cyclone-bms-protection")
        add("state", "fault", "fault", code_for(a["faults"], FAULT_NAMES) if ok else None, stype="cyclone-bms-fault")
        add("state", "warning", "BMS warning", warning, stype="cyclone-bms-warning")
        add("state", "mosfet", "MOSFETs", mosfet_code(a) if ok else None, stype="cyclone-bms-mosfet")

    return {"error": error, "notes": list(notes), "packs_answered": sum("error" not in s for s in packs),
            "states": STATE_TABLES, "sensors": sensors}


# --------------------------------------------------------------------------------------
# Probe mode
# --------------------------------------------------------------------------------------

def probe_frame(link, adr, info_byte, cid2=CID2_ANALOG):
    frame = encode_frame(adr, cid2, bytes([info_byte]) if info_byte is not None else b"")
    raw = link.transact(frame)
    print(f"ADR=0x{adr:02X} " + (f"INFO=0x{info_byte:02X}" if info_byte is not None else "no INFO")
          + f"  TX {frame!r}")
    print(f"    RX {raw!r}" if raw else "    RX <nothing>")
    resp = None
    if raw:
        try:
            resp = decode_frame(raw)
            print(f"    frame OK, header ADR=0x{resp['adr']:02X}, RTN=0x{resp['rtn']:02X} "
                  f"({RTN_CODES.get(resp['rtn'], '?')})")
        except BmsError as e:
            print(f"    frame error: {e}")
    time.sleep(0.3)
    return resp if resp and resp["rtn"] == 0 else None


def probe_single(link, target, adr, found):
    resp = probe_frame(link, target, adr)
    if not resp:
        return
    try:
        pack = parse_analog(resp["info"])[0]
    except BmsError as e:
        print(f"    decode error: {e}")
        return
    print(f"    decoded: {len(pack['cells_mv'])} cells, {pack['voltage_v']}V, SOC {fmt(pack['soc_pct'])}%, "
          f"address echo {pack['addr_echo']}")
    found[adr] = pack


def distinct_addresses(found):
    """Addresses whose replies differ from every earlier address's reply."""
    seen, keep = set(), []
    for adr, pack in found.items():
        if fingerprint(pack) in seen:
            print(f"    address {adr} returned the same data as an earlier address (ignored)")
            continue
        seen.add(fingerprint(pack))
        keep.append(adr)
    return keep


def probe(args):
    addresses = args.addresses if args.addresses != [0] else [0, 1]
    master = addresses[0] if args.addresses != [0] else 1
    print(f"Connecting to NPort {args.host}:{args.port} ...")
    with Link(args.host, args.port, args.timeout, connect_attempts=args.connect_attempts) as link:
        print("TCP connection OK.\n")

        print("== Per-address requests (ADR=n, INFO=n)")
        direct = {}
        for adr in addresses:
            probe_single(link, adr, adr, direct)
        direct_ok = distinct_addresses(direct)

        relay_ok = []
        if len(addresses) > 1:
            print(f"\n== Relay requests through the master (ADR={master}, INFO=n)")
            relayed = {}
            for adr in addresses:
                probe_single(link, master, adr, relayed)
            relay_ok = distinct_addresses(relayed)

        print(f"\n== Bank request (ADR={master}, INFO=0xFF: every pack in one reply)")
        bank = []
        resp = probe_frame(link, master, INFO_ALL_PACKS)
        if resp:
            try:
                bank = [p for p in parse_analog(resp["info"], multi=True) if "error" not in p]
                print(f"    header says {resp['info'][1]} pack(s), decoded {len(bank)}: " + ", ".join(
                    f"{p['voltage_v']}V SOC {fmt(p['soc_pct'])}%" for p in bank))
            except BmsError as e:
                print(f"    decode error: {e}")

        print(f"\n== Pack count (CID2 0x90, ADR={master})")
        resp = probe_frame(link, master, None, CID2_PACK_COUNT)
        if resp and resp["info"]:
            print(f"    pack count: {resp['info'][-1]}")

        print()
        if not (direct_ok or relay_ok or bank):
            print("No valid replies. Check: cable (RJ11 pin3 TX -> DB9 pin2, pin4 RX -> DB9 pin3,\n"
                  "pin5 GND -> DB9 pin5), NPort Port 1 = 9600 8N1 / no flow control / RS-232,\n"
                  "battery awake (RUN LED), and the DIP addresses.")
            return UNKNOWN
        best = max(len(direct_ok), len(relay_ok), len(bank))
        if best == 1 and direct_ok:
            hint = f"-a {direct_ok[0]}"
        elif len(bank) == best:
            hint = f"-a {master} --bank --packs {len(bank)}"
        elif len(direct_ok) == best:
            hint = "-a " + ",".join(map(str, direct_ok))
        else:
            hint = "-a " + ",".join(map(str, relay_ok)) + " --relay"
        print(f"Suggested options: {hint}\n")

        opts = build_parser().parse_args(["-H", args.host] + hint.split())
        packs, notes = poll(link, opts.addresses, opts.bank, opts.relay, opts.packs, with_ids=True)
        print(json.dumps({"notes": notes, "packs": packs}, indent=2))
        return OK


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

def address_list(s):
    try:
        addrs = [int(x, 0) for x in s.split(",") if x.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad address list: {s!r}")
    if not addrs or any(not 0 <= a <= 0xFE for a in addrs):
        raise argparse.ArgumentTypeError(f"addresses must be 0-254: {s!r}")
    return addrs


def build_parser():
    p = argparse.ArgumentParser(
        description="Poll Cyclone CYG-B48V-100A (PACE BMS) batteries through a Moxa NPort in TCP Server mode.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("-H", "--host", required=True, help="NPort IP address (LibreNMS passes this)")
    p.add_argument("-p", "--port", type=int, default=4001, help="NPort TCP Server port")
    p.add_argument("-a", "--address", dest="addresses", type=address_list, default=[0],
                   help="battery DIP address, or a comma list for paralleled packs (e.g. 1,2,3)")
    p.add_argument("--bank", action="store_true",
                   help="read all paralleled packs with one INFO=0xFF request to the first address")
    p.add_argument("--relay", action="store_true",
                   help="send every request to the first address (the master) and select the pack with INFO")
    p.add_argument("--packs", type=int, default=None,
                   help="with --bank: number of packs expected (missing ones go CRITICAL)")
    p.add_argument("--min-packs", type=int, default=None,
                   help="with --bank: like --packs, but more packs are accepted when the bank reports them "
                        "(LibreNMS auto-detection passes the packs it has already seen)")
    p.add_argument("-t", "--timeout", type=float, default=2.0, help="per-request timeout, seconds")
    p.add_argument("--connect-attempts", type=int, default=3, help="tries to connect to the NPort, 2 s apart")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="print the full decoded state as JSON")
    mode.add_argument("--librenms", action="store_true", help="print the LibreNMS sensor set as JSON")
    mode.add_argument("--probe", action="store_true", help="bring-up helper: try address/bank variants, show raw frames")
    p.add_argument("-v", "--verbose", action="store_true", help="print raw frames to stderr")
    p.add_argument("--cells", action="store_true", help="--librenms: add a voltage sensor for every cell")
    th = p.add_argument_group("thresholds (applied to every pack)")
    th.add_argument("--soc-warn", type=float, default=80, help="WARNING when SOC <= this (%%)")
    th.add_argument("--soc-crit", type=float, default=50, help="CRITICAL when SOC <= this (%%)")
    th.add_argument("--soh-warn", type=float, default=85, help="WARNING when SOH <= this (%%)")
    th.add_argument("--soh-crit", type=float, default=80, help="CRITICAL when SOH <= this (%%)")
    th.add_argument("--delta-warn", type=int, default=100, help="WARNING when max-min cell voltage >= this (mV)")
    th.add_argument("--delta-crit", type=int, default=250, help="CRITICAL when max-min cell voltage >= this (mV)")
    th.add_argument("--temp-high-warn", type=float, default=45, help="WARNING when any cell temp >= this (C)")
    th.add_argument("--temp-high-crit", type=float, default=55, help="CRITICAL when any cell temp >= this (C)")
    th.add_argument("--temp-low-warn", type=float, default=5, help="WARNING when any cell temp <= this (C)")
    th.add_argument("--temp-low-crit", type=float, default=0, help="CRITICAL when any cell temp <= this (C)")
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {VERSION}")
    return p


def expected_packs(args):
    return len(args.addresses) if not args.bank else (args.packs or args.min_packs or 1)


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.probe:
        try:
            return probe(args)
        except BmsError as e:
            print(f"ERROR: {e}")
            return UNKNOWN

    notes = []
    try:
        with Link(args.host, args.port, args.timeout, args.verbose, args.connect_attempts) as link:
            packs, notes = poll(link, args.addresses, args.bank, args.relay, args.packs, with_ids=args.json,
                                min_packs=args.min_packs)
    except BmsError as e:
        if args.librenms:
            # Nothing answered: still report every sensor so LibreNMS keeps them.
            addrs = args.addresses if not args.bank else [None] * expected_packs(args)
            packs = [{"address": a, "error": str(e)} for a in addrs]
            print(json.dumps(librenms_doc(packs, args, error=str(e)), indent=2))
        elif args.json:
            print(json.dumps({"error": str(e)}))
        else:
            print(f"UNKNOWN - {e}")
        return UNKNOWN

    if args.librenms:
        print(json.dumps(librenms_doc(packs, args, notes), indent=2))
        return OK if any("error" not in s for s in packs) else UNKNOWN

    if all("error" in s for s in packs):
        problems = "; ".join(s["error"] for s in packs)
        print(json.dumps({"error": problems, "packs": packs}, indent=2) if args.json else f"UNKNOWN - {problems}")
        return UNKNOWN

    status, problems, summary = evaluate(packs, args, notes)
    if args.json:
        print(json.dumps({"status": STATUS_NAMES[status], "problems": problems, "packs": packs}, indent=2))
    else:
        head = "; ".join(problems) + " - " if problems else ""
        print(f"{STATUS_NAMES[status]} - {head}{summary}")
    return status


if __name__ == "__main__":
    sys.exit(main())
