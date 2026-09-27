"""Runs a whole evening through the real control loop against a simulated site.

Nothing here is a mock of the controller logic: the same DecisionEngine, the same
EveningRunner, the same CreditMonitor and the same SafetyWrapper run against a
physical model of the battery and inverter. That is the point — if the simulation
secures the credit, the production path has actually been exercised.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from rich.console import Console
from rich.table import Table

from ..clock import SimClock
from ..config import AppConfig
from ..controllers.base import SafetyWrapper
from ..controllers.simulated import SimulatedBatteryController
from ..data_providers.base import ForecastProvider
from ..data_providers.simulated import SimulatedSite
from ..ledger import Ledger
from ..models import ControlCommand, DailyOutcome, Decision, ForecastPoint
from ..runtime import EveningRunner
from ..solar_geometry import sunset
from .profiles import SITE_LAT, SITE_LON, Scenario, load_curve, solar_curve

log = logging.getLogger(__name__)
console = Console()


class ScenarioForecastProvider(ForecastProvider):
    """Forecast with deliberate error injected, because perfect forecasts flatter.

    ``bias`` scales the solar forecast relative to truth and ``load_bias`` does the
    same for load. Running the suite with bias=0.8 / load_bias=1.15 is the honest
    test: does the controller still secure the credit when the forecast is wrong in
    the worst direction (less sun, more load than expected)?
    """

    name = "scenario"

    def __init__(self, scenario: Scenario, cfg: AppConfig, bias: float = 1.0, load_bias: float = 1.0):
        self.scenario = scenario
        self.cfg = cfg
        self.bias = bias
        self.load_bias = load_bias
        self._solar = solar_curve(scenario, cfg.site.tz)
        self._load = load_curve(scenario)

    async def solar(self, start: datetime, end: datetime) -> list[ForecastPoint]:
        pts, t = [], start
        while t <= end:
            pts.append(ForecastPoint(timestamp=t, solar_kw=self._solar(t) * self.bias))
            t += timedelta(minutes=15)
        return pts

    async def load(self, start: datetime, end: datetime) -> list[ForecastPoint]:
        pts, t = [], start
        while t <= end:
            pts.append(ForecastPoint(timestamp=t, load_kw=self._load(t) * self.load_bias))
            t += timedelta(minutes=15)
        return pts


@dataclass
class SimulationResult:
    scenario: Scenario
    decision: Decision
    outcome: DailyOutcome
    site: SimulatedSite
    sunset_local: datetime
    safety_violations: list[str]
    commands: list[ControlCommand]


async def run_scenario(
    scenario: Scenario,
    cfg: AppConfig | None = None,
    *,
    forecast_bias: float = 0.85,
    load_bias: float = 1.10,
    ledger: Ledger | None = None,
) -> SimulationResult:
    cfg = cfg or AppConfig()
    tz = cfg.site.tz
    day = scenario.day
    start = datetime(day.year, day.month, day.day, tzinfo=tz).replace(
        hour=cfg.plan.credit_window_start.hour, minute=0
    ) - timedelta(minutes=cfg.strategy.decision_lead_minutes)

    site = SimulatedSite(
        cfg,
        solar_kw_at=solar_curve(scenario, tz),
        load_kw_at=load_curve(scenario),
        start_soc_pct=scenario.start_soc_pct,
    )
    controller = SafetyWrapper(
        SimulatedBatteryController(site),
        max_power_kw=cfg.inverter.ac_limit_kw,
        min_soc_pct=cfg.battery.emergency_floor_soc_pct,
    )
    clock = SimClock(start, speed=0.0)
    runner = EveningRunner(
        cfg,
        telemetry=site,
        forecast=ScenarioForecastProvider(scenario, cfg, forecast_bias, load_bias),
        controller=controller,
        ledger=ledger,
        clock=clock,
    )

    decision = await runner.make_decision()
    outcome = await runner.run_window()

    return SimulationResult(
        scenario=scenario,
        decision=decision,
        outcome=outcome,
        site=site,
        sunset_local=sunset(day, SITE_LAT, SITE_LON, tz),
        safety_violations=list(controller.violations),
        commands=list(controller.command_log),
    )


# --------------------------------------------------------------------- display
def render_result(result: SimulationResult) -> None:
    s, d, o = result.scenario, result.decision, result.outcome
    verdict = "[bold green]$1 SECURED[/]" if o.credit_secured else "[bold red]$1 MISSED[/]"
    money = (
        f"[bold green]earned ${abs(o.estimated_revenue_aud):.2f}[/]"
        if o.estimated_revenue_aud < 0
        else f"[yellow]cost ${o.estimated_revenue_aud:.2f}[/]"
    )

    console.rule(f"[bold]{s.label}[/]  ({s.day}, sunset {result.sunset_local:%H:%M})")
    console.print(f"[dim]{s.description}[/]\n")

    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column(style="dim")
    t.add_column()
    t.add_row("Start SOC", f"{d.starting_soc_pct:.1f}%  ({d.starting_energy_kwh:.1f} kWh)")
    t.add_row("Plan", f"target_export_kwh={d.target_export_kwh:.2f}  "
                      f"recommended_discharge_kw={d.recommended_discharge_kw:.2f}  "
                      f"expected_final_soc={d.expected_final_soc:.1f}%")
    t.add_row("Mode", f"{d.recommended_mode.value}   peak setpoint {d.peak_discharge_kw:.2f} kW")
    t.add_row("", "")
    t.add_row("Actual export", f"{o.exported_kwh:.2f} kWh  (Super Export {o.super_export_kwh:.2f} kWh)")
    t.add_row("Actual import", f"{o.imported_kwh * 1000:.1f} Wh across the window")
    t.add_row("Final SOC", f"{o.final_soc_pct:.1f}%  ({o.final_energy_kwh:.1f} kWh)")
    t.add_row("Per-hour import", o.notes[0] if o.notes else "")
    t.add_row("", "")
    t.add_row("Verdict", f"{verdict}   {money}")
    console.print(t)

    if result.safety_violations:
        console.print("\n[yellow]Safety interventions:[/]")
        for v in result.safety_violations[:5]:
            console.print(f"  • {v}")

    console.print("\n[dim]Decision rationale:[/]")
    for line in d.rationale:
        console.print(f"  [dim]•[/] {line}")

    _render_profile(result)
    console.print()


def _render_profile(result: SimulationResult) -> None:
    """Half-hourly trace of what actually happened."""
    hist = [h for h in result.site.history if h.timestamp >= result.decision.window_start]
    if not hist:
        return
    table = Table(title="Half-hourly trace", title_style="dim", header_style="dim")
    for col in ("time", "solar kW", "load kW", "batt kW", "grid kW", "SOC %"):
        table.add_column(col, justify="right")
    step = max(1, len(hist) // 7)
    for h in hist[::step]:
        grid_style = "red" if h.grid_kw > 0.001 else "green"
        table.add_row(
            f"{h.timestamp:%H:%M}",
            f"{h.solar_kw:.2f}",
            f"{h.load_kw:.2f}",
            f"{h.battery_kw:.2f}",
            f"[{grid_style}]{h.grid_kw:+.2f}[/]",
            f"{h.soc_pct:.1f}",
        )
    console.print(table)
