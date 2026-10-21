"""Turn a chronological stream of parser.Event into a console-friendly
equipment-engagement summary: a timeline of transitions plus aggregated
totals (count + time engaged) per equipment channel.

Design notes (from log analysis):
  - Air Handler combos: compressor mode/stage comes from the AIRH_CMD tstat
    bitmask (Y1/Y2 = compressor call, B = reversing valve -> heat when set).
  - Furnace combos: compressor mode/stage comes from HCOMMAND's comp_cmd
    (stage number for staged ODU) + Rev_valve (heat when set).
  - For a communicating/variable ODU, the authoritative modulation % comes
    from OCOMMAND's HCMD/CCMD (confirmed over compPCT), with req_rpm != 0
    corroborating that the compressor is actually being commanded.
  - Secondary heat source (furnace gas valve / electric heat strip) comes
    from HCOMMAND's heat_cmd, cross-checked with the critical line's
    auxHPCT field.
  - deh/hum/vent fields are logged passively; their engagement semantics
    are not confirmed, so no on/off claim is made for them.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from parser import Event


@dataclass
class _Channel:
    value: str = "off"
    since: Optional[datetime] = None


@dataclass
class SummaryResult:
    timeline: list[tuple[Optional[datetime], str]] = field(default_factory=list)
    totals: dict[str, dict[str, timedelta]] = field(default_factory=dict)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    observed_fields: dict[str, Any] = field(default_factory=dict)


class Summarizer:
    def __init__(self) -> None:
        self._channels: dict[str, _Channel] = {}
        self._result = SummaryResult()
        self._last_ts: Optional[datetime] = None

        # Raw field memory used to (re)derive equipment snapshots.
        self._odu_variable: Optional[bool] = None  # None=unknown
        self._tstat_terminals: Optional[list[str]] = None
        self._comp_cmd: Optional[int] = None
        self._rev_valve: Optional[bool] = None
        self._comp_pct_critical: Optional[int] = None
        self._comp_mode_critical: Optional[str] = None
        self._heat_pct_ocommand: Optional[int] = None
        self._cool_pct_ocommand: Optional[int] = None
        self._req_rpm: Optional[int] = None
        self._curr_rpm: Optional[int] = None
        self._heat_cmd: Optional[int] = None
        self._aux_hpct: Optional[int] = None
        self._fan_stage: int = 0

    # -- public API ---------------------------------------------------
    def feed(self, event: Event) -> None:
        if event.ts is not None:
            self._last_ts = event.ts

        if event.kind == "critical_status":
            self._comp_mode_critical = event.data["comp"]
            self._comp_pct_critical = event.data["comp_pct"]
            self._aux_hpct = event.data["aux_hpct"]
            self._fan_stage = max(self._fan_stage, event.data["fan_stage"])
            self._fan_stage = event.data["fan_stage"]
            for key in ("deh", "hum", "vent"):
                self._note_observed(key, event.data[key])

        elif event.kind == "airh_cmd":
            self._tstat_terminals = event.data["terminals"]
            self._odu_variable = event.data["comm_odu"]
            self._fan_stage = event.data["fan_stage"]

        elif event.kind == "hcommand":
            self._comp_cmd = event.data["comp_cmd"]
            self._rev_valve = event.data["rev_valve"]
            self._heat_cmd = event.data["heat_cmd"]
            if event.data["defrost"]:
                self._note_observed("defrost", True)
            if event.data["humidifier_on"]:
                self._note_observed("humidifier_on", True)

        elif event.kind == "ocommand":
            self._odu_variable = True
            self._heat_pct_ocommand = event.data["heat_pct"]
            self._cool_pct_ocommand = event.data["cool_pct"]
            self._req_rpm = event.data["req_rpm"]
            self._curr_rpm = event.data["curr_rpm"]

        elif event.kind == "noncomm_odu":
            self._odu_variable = False

        elif event.kind == "fan_only":
            self._fan_stage = event.data["fan_stage"]

        elif event.kind == "summary":
            self._fan_stage = event.data["fan_stage"]
            if self._comp_mode_critical is None:
                self._comp_mode_critical = event.data["comp"]
                self._comp_pct_critical = event.data["comp_pct"]

        self._recompute(event.ts)

    def finalize(self) -> SummaryResult:
        for name, ch in self._channels.items():
            if ch.since is not None and self._last_ts is not None and ch.value != "off":
                self._accumulate(name, ch.value, self._last_ts - ch.since)
        return self._result

    # -- internals ------------------------------------------------------
    def _note_observed(self, key: str, value: Any) -> None:
        self._result.observed_fields[key] = value

    def _accumulate(self, channel: str, value: str, dur: timedelta) -> None:
        self._result.totals.setdefault(channel, {}).setdefault(value, timedelta())
        self._result.totals[channel][value] += dur

    def _set(self, channel: str, value: str, ts: Optional[datetime], detail: str = "") -> None:
        ch = self._channels.setdefault(channel, _Channel())
        if ch.value == value:
            return
        if ch.since is not None and ts is not None and ch.value != "off":
            self._accumulate(channel, ch.value, ts - ch.since)
        label = f"{channel}: {ch.value} -> {value}{detail}" if ch.since is not None else f"{channel}: {value}{detail}"
        self._result.timeline.append((ts, label))
        if value != "off":
            self._result.counts.setdefault(channel, {}).setdefault(value, 0)
            self._result.counts[channel][value] += 1
        ch.value = value
        ch.since = ts

    def _recompute(self, ts: Optional[datetime]) -> None:
        self._set("fan", self._fan_value(), ts)
        self._set("compressor", self._compressor_value(), ts, detail=self._compressor_detail())
        self._set("secondary_heat", self._secondary_heat_value(), ts)

    def _fan_value(self) -> str:
        return "off" if self._fan_stage <= 0 else f"stage{self._fan_stage}"

    def _compressor_value(self) -> str:
        # Prefer the Air Handler tstat bitmask when available (most precise).
        if self._tstat_terminals is not None:
            has_call = "Y1" in self._tstat_terminals or "Y2" in self._tstat_terminals
            if not has_call:
                return "off"
            mode = "heat" if "B" in self._tstat_terminals else "cool"
            if self._odu_variable:
                return f"{mode} {self._best_pct(mode)}"
            stage = 2 if "Y2" in self._tstat_terminals else 1
            return f"{mode} stage{stage}"

        # Otherwise use the Furnace HCOMMAND comp_cmd/Rev_valve pair.
        if self._comp_cmd is not None:
            if not self._comp_cmd:
                return "off"
            mode = "heat" if self._rev_valve else "cool"
            if self._odu_variable:
                return f"{mode} {self._best_pct(mode)}"
            return f"{mode} stage{self._comp_cmd}"

        # Fallback: critical-line comp/compPCT only.
        if self._comp_mode_critical and self._comp_mode_critical != "off":
            return f"{self._comp_mode_critical} {self._comp_pct_critical}%"
        return "off"

    def _best_pct(self, mode: str) -> str:
        """Prefer OCOMMAND's HCMD/CCMD (confirmed authoritative) over the
        critical line's compPCT when available."""
        if mode == "heat" and self._heat_pct_ocommand is not None:
            return f"{self._heat_pct_ocommand}%"
        if mode == "cool" and self._cool_pct_ocommand is not None:
            return f"{self._cool_pct_ocommand}%"
        if self._comp_pct_critical is not None:
            return f"{self._comp_pct_critical}%"
        return "?%"

    def _compressor_detail(self) -> str:
        """OCOMMAND's req/curr RPM, shown alongside the compressor timeline
        entry as corroborating detail (not part of the state key, since RPM
        fluctuates independently of the commanded mode/%)."""
        if self._req_rpm is not None:
            return f" (OCOMMAND req={self._req_rpm},curr:{self._curr_rpm})"
        return ""

    def _secondary_heat_value(self) -> str:
        if self._heat_cmd:
            return f"{self._heat_cmd}%"
        if self._aux_hpct is not None and self._aux_hpct > 0:
            return f"{self._aux_hpct}%"
        return "off"


def summarize(events: Iterable[Event]) -> SummaryResult:
    s = Summarizer()
    for event in events:
        s.feed(event)
    return s.finalize()


def format_report(result: SummaryResult) -> str:
    lines: list[str] = []
    lines.append("=== Equipment Timeline ===")
    if not result.timeline:
        lines.append("(no equipment-state lines recognized)")
    for ts, text in result.timeline:
        ts_str = ts.strftime("%H:%M:%S") if ts else "??:??:??"
        lines.append(f"{ts_str}  {text}")

    lines.append("")
    lines.append("=== Totals ===")
    if not result.totals:
        lines.append("(nothing engaged)")
    for channel, values in result.totals.items():
        lines.append(f"{channel}:")
        for value, dur in values.items():
            count = result.counts.get(channel, {}).get(value, 0)
            lines.append(f"  {value}: engaged {count}x, total {dur}")

    if result.observed_fields:
        lines.append("")
        lines.append("=== Observed (unconfirmed semantics, logged as-is) ===")
        for key, value in result.observed_fields.items():
            lines.append(f"  {key}: last seen value = {value}")

    return "\n".join(lines)
