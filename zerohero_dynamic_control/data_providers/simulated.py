"""Simulated site: a physical model of battery, PV and load used by the harness.

This doubles as both a TelemetryProvider and the backing store for the simulated
BatteryController, so a simulation exercises exactly the same control loop, decision
engine and safety logic that runs against real hardware.
"""

from __future__ import annotations

from datetime import datetime

from ..config import AppConfig
from ..curves import KwAt
from ..models import BatteryMode, Telemetry, energy_to_soc, soc_to_energy
from .base import TelemetryProvider


class SimulatedSite(TelemetryProvider):
    """A tiny but honest model of the reference site.

    Honest about the things that actually bite a controller:
      * the inverter's shared AC limit (PV competes with the battery for the 10 kW),
      * charge/discharge efficiency, so planned kWh != delivered kWh,
      * a finite ramp rate, so a step change in setpoint is not instantaneous,
      * and the hard SOC floor, which the BMS enforces whatever we ask for.
    """

    name = "simulated"

    def __init__(
        self,
        config: AppConfig,
        *,
        solar_kw_at: KwAt,
        load_kw_at: KwAt,
        start_soc_pct: float = 85.0,
    ) -> None:
        self.cfg = config
        self.solar_kw_at = solar_kw_at
        self.load_kw_at = load_kw_at
        self.capacity = config.battery.usable_capacity_kwh
        self.energy_kwh = soc_to_energy(start_soc_pct, self.capacity)

        self.mode: BatteryMode = BatteryMode.SELF_CONSUMPTION
        self.commanded_kw: float = 0.0
        self.actual_battery_kw: float = 0.0

        self.cumulative_import_kwh = 0.0
        self.cumulative_export_kwh = 0.0
        self.last_step: datetime | None = None
        self.history: list[Telemetry] = []

    # ------------------------------------------------------------------ control
    def apply_command(self, mode: BatteryMode, power_kw: float) -> None:
        self.mode = mode
        self.commanded_kw = power_kw

    # -------------------------------------------------------------------- model
    def _target_battery_kw(self, solar_kw: float, load_kw: float) -> float:
        """What the inverter will try to do, before limits."""
        if self.mode is BatteryMode.HOLD:
            return 0.0
        if self.mode is BatteryMode.FORCE_EXPORT:
            return self.commanded_kw
        if self.mode is BatteryMode.FORCE_CHARGE:
            return -abs(self.commanded_kw)
        # SELF_CONSUMPTION / BACKUP: chase zero grid flow.
        return load_kw - solar_kw

    def _clamp(self, battery_kw: float, solar_kw: float, load_kw: float, dt_h: float) -> float:
        cfg = self.cfg
        if battery_kw >= 0:
            battery_kw = min(battery_kw, cfg.battery.max_discharge_kw)
            if cfg.inverter.solar_shares_ac_limit:
                # PV and battery share the single AC port — see decision_engine's
                # equation (4). This is the limit that surprises people.
                battery_kw = min(battery_kw, max(0.0, cfg.inverter.ac_limit_kw - solar_kw))
            # Do not exceed the DNSP export limit.
            battery_kw = min(battery_kw, max(0.0, cfg.inverter.grid_export_limit_kw + load_kw - solar_kw))
            # The BMS will not go below the hard floor regardless of what we ask.
            floor = soc_to_energy(cfg.battery.emergency_floor_soc_pct, self.capacity)
            max_dc = max(0.0, self.energy_kwh - floor) / max(dt_h, 1e-9)
            battery_kw = min(battery_kw, max_dc * cfg.battery.discharge_efficiency)
        else:
            battery_kw = max(battery_kw, -cfg.battery.max_charge_kw)
            headroom = max(0.0, self.capacity - self.energy_kwh) / max(dt_h, 1e-9)
            battery_kw = max(battery_kw, -headroom / cfg.battery.charge_efficiency)
        return battery_kw

    def _ramp(self, target_kw: float, dt_minutes: float) -> float:
        limit = self.cfg.battery.ramp_kw_per_minute * dt_minutes
        delta = target_kw - self.actual_battery_kw
        if abs(delta) > limit:
            target_kw = self.actual_battery_kw + limit * (1 if delta > 0 else -1)
        return target_kw

    async def read(self, now: datetime) -> Telemetry:
        dt_h = 0.0
        if self.last_step is not None:
            dt_h = max(0.0, (now - self.last_step).total_seconds() / 3600.0)
        self.last_step = now

        solar = max(0.0, self.solar_kw_at(now))
        load = max(0.0, self.load_kw_at(now))

        target = self._target_battery_kw(solar, load)
        target = self._clamp(target, solar, load, dt_h if dt_h > 0 else 1 / 60)
        target = self._ramp(target, dt_h * 60 if dt_h > 0 else 1.0)
        self.actual_battery_kw = target

        # Integrate the energy flows over the elapsed interval.
        if dt_h > 0:
            if target >= 0:
                self.energy_kwh -= target * dt_h / self.cfg.battery.discharge_efficiency
            else:
                self.energy_kwh += -target * dt_h * self.cfg.battery.charge_efficiency
            self.energy_kwh = max(0.0, min(self.capacity, self.energy_kwh))

        grid = load - solar - target  # equation (2)
        if dt_h > 0:
            if grid > 0:
                self.cumulative_import_kwh += grid * dt_h
            else:
                self.cumulative_export_kwh += -grid * dt_h

        reading = Telemetry(
            timestamp=now,
            soc_pct=energy_to_soc(self.energy_kwh, self.capacity),
            battery_energy_kwh=self.energy_kwh,
            solar_kw=solar,
            load_kw=load,
            battery_kw=target,
            grid_kw=grid,
        )
        self.history.append(reading)
        return reading
