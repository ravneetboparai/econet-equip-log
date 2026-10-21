"""Parse ecobee cpss journalctl lines into normalized equipment-state events.

Covers the signal sources confirmed against real device logs:
  - AIRH_CMD tstat bitmask (Air Handler IDU, communicating or staged ODU)
  - HCOMMAND heat_cmd/comp_cmd/Rev_valve (Furnace IDU, communicating or staged ODU)
  - OCOMMAND req/curr/HCMD/CCMD (communicating/variable ODU detail)
  - [Critical] AirHandler:/Furnace:/VariableCompressor: status snapshots
  - hvacCommand Comp:/CompPCT=/Aux:/Fan: concise summary line
  - getIDUFanCFM ... "Non Communicating ODU FAN" explicit detector
  - getIDUFanCFM FAN cfm=.. fanstage=.. (fan-only, no ODU involved)
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

# ---------------------------------------------------------------------------
# tstat bitmask -> terminal names (LSB first)
# ---------------------------------------------------------------------------
TSTAT_BITS = [
    (0, "G"),    # fan
    (1, "W1"),   # heat stage1 (furnace/strip)
    (2, "W2"),   # heat stage2
    (3, "Y1"),   # compressor/cool stage1
    (4, "Y2"),   # compressor/cool stage2
    (5, "B"),    # reversing valve (configurable energize-on-heat/cool)
    (6, "ODD"),  # spare / outdoor dehumidify
]


def decode_tstat(hex_str: str) -> list[str]:
    value = int(hex_str, 16)
    return [name for bit, name in TSTAT_BITS if value & (1 << bit)]


# ---------------------------------------------------------------------------
# Line prefix: "<ts> <host> <proc>[<pid>]: <msg>"
# <ts> is either syslog style ("Oct 20 20:44:20") or a raw epoch float
# ("1792525879.851550").
# ---------------------------------------------------------------------------
LINE_RE = re.compile(r"^(?P<ts>.+?)\s+\S+\s+\S+\[\d+\]:\s*(?P<msg>.*)$")

_SYSLOG_TS_RE = re.compile(r"^[A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}$")


def parse_timestamp(ts: str) -> Optional[datetime]:
    ts = ts.strip()
    try:
        return datetime.fromtimestamp(float(ts))
    except ValueError:
        pass
    if _SYSLOG_TS_RE.match(ts):
        try:
            dt = datetime.strptime(ts, "%b %d %H:%M:%S")
            return dt.replace(year=datetime.now().year)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Field-level regexes
# ---------------------------------------------------------------------------
AIRH_CMD_RE = re.compile(
    r"hvacCommand AIRH_CMD tstat=0x([0-9A-Fa-f]+),cfm=\d+\((\d+)\),.*?"
    r"CommODU=(\d+),fanStage=(\d+),"
)

HCOMMAND_RE = re.compile(
    r"hvacCommand HCOMMAND heat_cmd=(\d+) comp_cmd=(\d+) Rev_valve=(\d+) "
    r"CFM_B16=\d+ CFM_B8=\d+ \((\d+)\) Def_mode=(\d+) Humid_On_Off=(\d+) "
    r"Test_Mode=(\d+)"
)

OCOMMAND_RE = re.compile(
    r"hvacCommand OCOMMAND\[([^\]]+)\],req=(\d+),curr:(\d+),HCMD=(\d+),"
    r"CCMD=(\d+),tarRPM=(\d+)"
)

CRITICAL_STATUS_RE = re.compile(
    r"\[Critical\]\s+(AirHandler|Furnace|VariableCompressor):"
    r"T:(-?\d+),H:(-?\d+),C:(-?\d+),comp:(\w+),compPCT:(-?\d+),"
    r"auxHPCT:(-?\d+),fan:(\d+),dehFanRed:(\d+),deh:(\d+),hum:(\d+),"
    r"vent:(\d+),ton:(-?\d+)"
)

SUMMARY_RE = re.compile(
    r"hvacCommand Comp:(\w+),\s*CompPCT=(-?\d+),\s*Aux:(-?\d+),\s*Fan:(\d+)"
)

NONCOMM_ODU_RE = re.compile(r"getIDUFanCFM Non Communicating ODU FAN(?:\s+CFM=(\d+))?")

FAN_ONLY_RE = re.compile(r"getIDUFanCFM FAN cfm=(\d+) fanstage=(\d+)")


@dataclass
class Event:
    ts: Optional[datetime]
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    raw: str = ""


def parse_line(raw_line: str) -> Optional[Event]:
    """Parse a single journalctl line into an Event, or None if not relevant."""
    m = LINE_RE.match(raw_line)
    if not m:
        return None
    ts = parse_timestamp(m.group("ts"))
    msg = m.group("msg")

    m2 = CRITICAL_STATUS_RE.search(msg)
    if m2:
        label, t, h, c, comp, comp_pct, aux_hpct, fan, deh_fan_red, deh, hum, vent, ton = m2.groups()
        return Event(ts, "critical_status", {
            "label": label,
            "comp": comp,
            "comp_pct": int(comp_pct),
            "aux_hpct": int(aux_hpct),
            "fan_stage": int(fan),
            "deh": int(deh),
            "hum": int(hum),
            "vent": int(vent),
        }, raw_line)

    m2 = AIRH_CMD_RE.search(msg)
    if m2:
        tstat_hex, cfm, comm_odu, fan_stage = m2.groups()
        return Event(ts, "airh_cmd", {
            "terminals": decode_tstat(tstat_hex),
            "cfm": int(cfm),
            "comm_odu": comm_odu == "1",
            "fan_stage": int(fan_stage),
        }, raw_line)

    m2 = HCOMMAND_RE.search(msg)
    if m2:
        heat_cmd, comp_cmd, rev_valve, cfm, def_mode, humid_on_off, test_mode = m2.groups()
        return Event(ts, "hcommand", {
            "heat_cmd": int(heat_cmd),
            "comp_cmd": int(comp_cmd),
            "rev_valve": rev_valve == "1",
            "cfm": int(cfm),
            "defrost": def_mode == "1",
            "humidifier_on": humid_on_off == "1",
        }, raw_line)

    m2 = OCOMMAND_RE.search(msg)
    if m2:
        state, req, curr, hcmd, ccmd, tar_rpm = m2.groups()
        return Event(ts, "ocommand", {
            "state": state,
            "req_rpm": int(req),
            "curr_rpm": int(curr),
            "heat_pct": int(hcmd),
            "cool_pct": int(ccmd),
            "target_rpm": int(tar_rpm),
        }, raw_line)

    m2 = SUMMARY_RE.search(msg)
    if m2:
        comp, comp_pct, aux, fan = m2.groups()
        return Event(ts, "summary", {
            "comp": comp,
            "comp_pct": int(comp_pct),
            "aux": int(aux),
            "fan_stage": int(fan),
        }, raw_line)

    m2 = NONCOMM_ODU_RE.search(msg)
    if m2:
        cfm = m2.group(1)
        return Event(ts, "noncomm_odu", {"cfm": int(cfm) if cfm else None}, raw_line)

    m2 = FAN_ONLY_RE.search(msg)
    if m2:
        cfm, fan_stage = m2.groups()
        return Event(ts, "fan_only", {"cfm": int(cfm), "fan_stage": int(fan_stage)}, raw_line)

    return None
