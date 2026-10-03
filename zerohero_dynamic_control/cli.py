"""Command line interface.

    zerohero simulate              run every scenario through the full control loop
    zerohero simulate -s winter    one scenario
    zerohero plan                  build tonight's decision from live data, command nothing
    zerohero economics             show the tariff model and where the money is
    zerohero run                   start the scheduler daemon (17:50 + 11:00 jobs)
    zerohero serve                 scheduler plus the FastAPI status/override endpoints
    zerohero ledger                recent daily outcomes
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .config import AppConfig
from .economics import best_case_daily_pnl, project_daily_pnl
from .logging_setup import setup_logging

app = typer.Typer(add_completion=False, help="GloBird ZEROHERO dynamic battery control")
console = Console()

ConfigOpt = typer.Option(None, "--config", "-c", help="Path to config.yaml")
ApiKeyOpt = typer.Option(None, "--api-key", envvar="FOXESS_API_KEY",
                         help="Overrides providers.foxess.api_key")
HostOpt = typer.Option(None, "--host", envvar="ZEROHERO_API_HOST",
                       help="Bind address for the API (use 0.0.0.0 in Docker)")
PortOpt = typer.Option(None, "--port", envvar="ZEROHERO_API_PORT", help="API port")


def _load(config: Path | None, level: str | None = None) -> AppConfig:
    cfg = AppConfig.load(config)
    setup_logging(cfg.logging, level=level)
    return cfg


@app.command()
def simulate(
    config: Path | None = ConfigOpt,
    scenario: str | None = typer.Option(None, "--scenario", "-s", help="summer|winter|cloudy|high_load|low_soc"),
    forecast_bias: float = typer.Option(0.85, help="Solar forecast error: 0.85 = forecast 15%% low"),
    load_bias: float = typer.Option(1.10, help="Load forecast error: 1.10 = forecast 10%% high"),
    log_level: str = typer.Option("WARNING", "--log-level"),
) -> None:
    """Replay synthetic evenings through the real decision engine and control loop."""
    from .simulation import SCENARIOS, render_result, run_scenario

    cfg = _load(config, log_level)
    keys = [scenario] if scenario else list(SCENARIOS)
    unknown = [k for k in keys if k not in SCENARIOS]
    if unknown:
        raise typer.BadParameter(f"unknown scenario(s) {unknown}; choose from {list(SCENARIOS)}")

    async def _run() -> list:
        return [
            await run_scenario(SCENARIOS[k], cfg, forecast_bias=forecast_bias, load_bias=load_bias)
            for k in keys
        ]

    results = asyncio.run(_run())
    for r in results:
        render_result(r)
    _summary_table(results)


def _summary_table(results: list) -> None:
    table = Table(title="Summary", header_style="bold")
    for col, just in (("scenario", "left"), ("start SOC", "right"), ("exported", "right"),
                      ("import", "right"), ("final SOC", "right"), ("$1 credit", "center"),
                      ("net $", "right")):
        table.add_column(col, justify=just)
    total = 0.0
    for r in results:
        o = r.outcome
        total += o.estimated_revenue_aud
        net = o.estimated_revenue_aud
        table.add_row(
            r.scenario.key,
            f"{r.decision.starting_soc_pct:.0f}%",
            f"{o.exported_kwh:.2f} kWh",
            f"{o.imported_kwh * 1000:.0f} Wh",
            f"{o.final_soc_pct:.0f}%",
            "[green]YES[/]" if o.credit_secured else "[red]NO[/]",
            f"[green]-{abs(net):.2f}[/]" if net < 0 else f"{net:.2f}",
        )
    table.add_section()
    table.add_row("[bold]mean[/]", "", "", "", "", "", f"[bold]{total / max(1, len(results)):.2f}[/]")
    console.print(table)
    console.print("[dim]net $ is the whole day including the $1.58 supply charge; "
                  "negative means the day earned money.[/]")


@app.command()
def plan(
    config: Path | None = ConfigOpt,
    log_level: str = typer.Option("INFO", "--log-level"),
) -> None:
    """Build tonight's decision from live providers. Issues no commands."""
    from .clock import RealClock
    from .controllers.printing import PrintingBatteryController
    from .data_providers import build_forecast_provider
    from .runtime import EveningRunner

    cfg = _load(config, log_level)
    if cfg.providers.battery == "simulated":
        console.print("[yellow]providers.battery is 'simulated' — this plan uses synthetic telemetry.[/]")

    from .data_providers.simulated import SimulatedSite
    from .simulation.harness import ScenarioForecastProvider  # noqa: F401  (kept for parity)
    from .simulation.profiles import SCENARIOS, load_curve, solar_curve

    scenario = SCENARIOS["summer"]
    site = SimulatedSite(
        cfg,
        solar_kw_at=solar_curve(scenario, cfg.site.tz),
        load_kw_at=load_curve(scenario),
        start_soc_pct=cfg.simulation.start_soc_pct,
    )

    async def _run():
        runner = EveningRunner(
            cfg,
            telemetry=site,
            forecast=build_forecast_provider(cfg),
            controller=PrintingBatteryController(quiet=True),
            ledger=None,
            clock=RealClock(cfg.site.tz),
        )
        return await runner.make_decision()

    decision = asyncio.run(_run())
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column(style="dim")
    t.add_column()
    t.add_row("window", f"{decision.window_start:%H:%M} - {decision.window_end:%H:%M}")
    t.add_row("target_export_kwh", f"{decision.target_export_kwh:.2f}")
    t.add_row("recommended_discharge_kw", f"{decision.recommended_discharge_kw:.2f}")
    t.add_row("expected_final_soc", f"{decision.expected_final_soc:.1f}%")
    t.add_row("mode", decision.recommended_mode.value)
    t.add_row("risk", decision.risk.value)
    t.add_row("projected P&L", decision.pnl_summary)
    console.print(t)
    for line in decision.rationale:
        console.print(f"  [dim]•[/] {line}")


@app.command()
def economics(config: Path | None = ConfigOpt) -> None:
    """Show the tariff model and where the money actually is."""
    cfg = _load(config, "WARNING")
    tariff = cfg.plan.tariff

    t = Table(title="ZEROHERO tariff", header_style="bold")
    t.add_column("line")
    t.add_column("window")
    t.add_column("rate", justify="right")
    for p in tariff.import_periods:
        t.add_row(f"import / {p.name}", f"{p.start:%H:%M}-{p.end:%H:%M}", f"${p.rate_aud_per_kwh:.5f}")
    for p in tariff.export_periods:
        t.add_row(f"export / {p.name}", f"{p.start:%H:%M}-{p.end:%H:%M}", f"-${p.rate_aud_per_kwh:.5f}")
    t.add_row("Super Export top up",
              f"{tariff.super_export_start:%H:%M}-{tariff.super_export_end:%H:%M} "
              f"(first {tariff.super_export_cap_kwh:.0f} kWh)",
              f"-${tariff.super_export_topup_aud_per_kwh:.5f}")
    t.add_row("ZeroHero credit",
              f"{tariff.zerohero_start:%H:%M}-{tariff.zerohero_end:%H:%M} "
              f"(<{tariff.zerohero_import_limit_kwh_per_hour} kWh/h import)",
              f"-${tariff.zerohero_credit_aud:.2f}/day")
    t.add_row("daily supply charge", "—", f"${tariff.daily_supply_charge_aud:.5f}/day")
    console.print(t)

    best = best_case_daily_pnl(tariff)
    console.print(f"\n[bold]Best achievable day[/]: {best.format()}")
    nothing = project_daily_pnl(tariff, in_window_export_kwh=0.0, credit_secured=False)
    console.print(f"[bold]Do-nothing day[/]:      {nothing.format()}")
    delta = nothing.net_aud - best.net_aud
    console.print(f"\n[bold green]Daily swing: ${delta:.2f}  →  ${delta * 365:.0f}/year[/]")
    console.print("[dim]The whole strategy: import at $0.00 in 11:00-14:00, sell at $0.10 in "
                  "18:00-21:00, and never import during the credit window.[/]")


@app.command()
def ledger(
    config: Path | None = ConfigOpt,
    limit: int = typer.Option(14, help="How many days to show"),
) -> None:
    """Show recent daily outcomes. GloBird's figures replace estimates where recorded."""
    from .ledger import Ledger, verdict_of

    cfg = _load(config, "WARNING")
    book = Ledger(cfg.logging.ledger_path, cfg.logging.decision_log_path)
    rows = book.read_outcomes(limit)
    bills = book.read_bills()
    if not rows:
        console.print("[yellow]no ledger entries yet[/]")
        raise typer.Exit()
    t = Table(title=f"Last {len(rows)} days", header_style="bold")
    for col in ("date", "sold kWh", "import", "final SOC", "$1", "day $"):
        t.add_column(col, justify="right" if col != "date" else "left")
    shown = {"SECURED": "[green]YES[/]", "MISSED": "[red]NO[/]", "UNVERIFIED": "[yellow]?[/]",
             "CHECK BILL": "[yellow]check bill[/]"}
    counts = {v: 0 for v in shown}
    credit = cfg.plan.daily_credit_aud
    topup_rate = cfg.plan.tariff.super_export_topup_aud_per_kwh
    from_bill = 0
    for r in rows:
        bill = bills.get(r.date)
        verdict = verdict_of(r, bill)
        counts[verdict] += 1
        sold = f"{r.exported_kwh:.2f}"
        if bill is not None:
            from_bill += 1
            if bill.super_export_topup_aud is not None and topup_rate > 0:
                sold = f"{-bill.super_export_topup_aud / topup_rate:.2f} b"
            day = f"{bill.total_cost_aud:.2f} b" if bill.total_cost_aud is not None else "-"
            credit_cell = shown[verdict] + " b"
        else:
            # The stored net assumes no credit unless SECURED. Where the bill decides,
            # show it as if paid and flag it, rather than booking a loss nobody saw.
            unknown = verdict in ("CHECK BILL", "UNVERIFIED")
            net = r.estimated_revenue_aud - credit if unknown else r.estimated_revenue_aud
            day = f"{net:.2f}" + ("*" if unknown else "")
            credit_cell = shown[verdict]
        t.add_row(r.date, sold, f"{r.imported_kwh * 1000:.0f} Wh", f"{r.final_soc_pct:.0f}%", credit_cell, day)
    console.print(t)
    console.print(
        f"secured {counts['SECURED']}, check bill {counts['CHECK BILL']}, "
        f"unverified {counts['UNVERIFIED']}, missed {counts['MISSED']} "
        f"(${counts['MISSED'] * credit:.2f} lost on missed days)"
        + (f" — {from_bill} of {len(rows)} days from GloBird's bill" if from_bill else "")
    )
    console.print("[dim]b = GloBird's bill (`zerohero bill`); other figures are the controller's "
                  "estimates. * assumes the credit was paid.[/]")


@app.command()
def bill(
    date: str = typer.Argument(..., help="Day of the bill, YYYY-MM-DD"),
    paid: bool = typer.Option(..., "--paid/--not-paid", help="Was the ZEROHERO credit paid?"),
    total: float | None = typer.Option(None, help="Total cost for the day, $ (negative = earned)"),
    usage: float | None = typer.Option(None, help="USAGE line, $"),
    solar: float | None = typer.Option(None, help="SOLAR line, $ (negative)"),
    topup: float | None = typer.Option(None, help="Super Export top up line, $ (negative)"),
    config: Path | None = ConfigOpt,
) -> None:
    """Record GloBird's figures for a day. They override the controller's estimate."""
    from datetime import date as date_type
    from datetime import datetime

    from .ledger import Ledger
    from .models import BillRecord

    try:
        date_type.fromisoformat(date)
    except ValueError as exc:
        raise typer.BadParameter(f"{date!r} is not YYYY-MM-DD") from exc
    cfg = _load(config, "WARNING")
    record = BillRecord(date=date, credit_paid=paid, total_cost_aud=total, usage_aud=usage,
                        solar_aud=solar, super_export_topup_aud=topup, source="manual",
                        recorded_at=datetime.now(cfg.site.tz))
    Ledger(cfg.logging.ledger_path, cfg.logging.decision_log_path).record_bill(record)
    console.print(f"recorded {date}: credit {'[green]paid[/]' if paid else '[red]not paid[/]'}"
                  + (f", day ${total:.2f}" if total is not None else ""))


@app.command("globird-fetch")
def globird_fetch(
    config: Path | None = ConfigOpt,
    days: int = typer.Option(7, help="How many days back to read"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what the portal says; record nothing"),
) -> None:
    """Read GloBird's daily costs from the customer portal and record new ones.

    Needs GLOBIRD_EMAIL and GLOBIRD_PASSWORD in the environment. Read-only.
    """
    from datetime import datetime

    from .globird import GloBirdError, GloBirdPortal, credentials_from_env, new_or_changed
    from .ledger import Ledger

    cfg = _load(config, "WARNING")
    creds = credentials_from_env()
    if creds is None:
        console.print("[red]GLOBIRD_EMAIL and GLOBIRD_PASSWORD are not set.[/] Put them in the .env "
                      "beside your compose file and restart the container.")
        raise typer.Exit(1)
    book = Ledger(cfg.logging.ledger_path, cfg.logging.decision_log_path)
    now = datetime.now(cfg.site.tz)
    portal = GloBirdPortal(*creds, session_path=cfg.logging.ledger_path.parent / "globird_session.json")

    async def _run():
        try:
            return await portal.fetch_bills(days=days, today=now.date())
        finally:
            await portal.aclose()

    try:
        fetched = asyncio.run(_run())
    except GloBirdError as exc:
        console.print(f"[bold red]GloBird:[/] {exc}")
        raise typer.Exit(1) from exc
    fresh = new_or_changed(fetched, book.read_bills())
    t = Table(title="GloBird portal", header_style="bold")
    for col in ("date", "credit", "day $", "usage", "solar", "top-up", ""):
        t.add_column(col, justify="right" if col != "date" else "left")
    money = lambda v: "-" if v is None else f"{v:.2f}"  # noqa: E731
    for b in fetched:
        t.add_row(b.date, "[green]paid[/]" if b.credit_paid else "[red]not paid[/]", money(b.total_cost_aud),
                  money(b.usage_aud), money(b.solar_aud), money(b.super_export_topup_aud),
                  "[cyan]new[/]" if b in fresh else "")
    console.print(t)
    if dry_run:
        console.print(f"dry run: {len(fresh)} day(s) would be recorded")
        return
    for b in fresh:
        book.record_bill(b.model_copy(update={"recorded_at": now}))
    console.print(f"recorded {len(fresh)} day(s)")


def _foxess_history_client(cfg: AppConfig):
    """A FoxESS cloud client for the overnight history, or None without credentials."""
    fox = cfg.providers.foxess
    if cfg.providers.battery != "foxess" or not fox.api_key:
        return None
    from .foxess_client import CallBudget, FoxESSClient

    return FoxESSClient(fox.api_key, base_url=fox.base_url, timezone=cfg.site.timezone,
                        budget=CallBudget(daily_limit=fox.daily_call_limit, reserve=fox.call_reserve))


@app.command("notify-test")
def notify_test(config: Path | None = ConfigOpt) -> None:
    """Send a test message to the ntfy topic in NTFY_TOPIC."""
    from .notify import ENV_TOPIC, ntfy_from_env

    cfg = _load(config, "WARNING")
    ntfy = ntfy_from_env(cfg.notify.server)
    if ntfy is None:
        console.print(f"[red]{ENV_TOPIC} is not set.[/] Put it in the .env beside your compose file.")
        raise typer.Exit(1)
    ok = asyncio.run(ntfy.send("ZeroHero test", "If you can read this, daily messages will arrive here.",
                               tags=["wave"]))
    console.print("[green]sent[/]" if ok else "[red]ntfy did not accept the message[/] (see the log)")
    raise typer.Exit(0 if ok else 1)


@app.command("notify-preview")
def notify_preview(
    config: Path | None = ConfigOpt,
    day: str | None = typer.Option(None, "--date", help="YYYY-MM-DD (default: yesterday)"),
    send: bool = typer.Option(False, "--send", help="Also send it (does not count as the daily message)"),
) -> None:
    """Show the morning summary for a day, as it would be sent."""
    from datetime import date as date_type
    from datetime import datetime, timedelta

    from . import __version__
    from .ledger import Ledger
    from .notify import morning_message, ntfy_from_env

    cfg = _load(config, "WARNING")
    now = datetime.now(cfg.site.tz)
    target = date_type.fromisoformat(day) if day else (now - timedelta(days=1)).date()
    ledger = Ledger(cfg.logging.ledger_path, cfg.logging.decision_log_path)
    client = _foxess_history_client(cfg)

    async def _run():
        try:
            return await morning_message(cfg, ledger, target, now, foxess_client=client,
                                         health=[f"Controller {__version__} (preview)"])
        finally:
            if client is not None:
                await client.aclose()

    title, body, tags, _ = asyncio.run(_run())
    console.print(f"[bold]{title}[/]\n{body}")
    if send:
        ntfy = ntfy_from_env(cfg.notify.server)
        if ntfy is None:
            console.print("[red]NTFY_TOPIC is not set[/]")
            raise typer.Exit(1)
        console.print("[green]sent[/]" if asyncio.run(ntfy.send(title, body, tags=tags)) else "[red]not sent[/]")


@app.command("overnight-backfill")
def overnight_backfill(
    config: Path | None = ConfigOpt,
    days: int = typer.Option(7, help="How many past nights to record"),
) -> None:
    """Record the last N nights' battery curves from FoxESS history (one call each)."""
    from datetime import datetime, timedelta

    from .ledger import Ledger
    from .overnight import fetch_night

    cfg = _load(config, "WARNING")
    client = _foxess_history_client(cfg)
    sn = cfg.providers.foxess.serial_number
    if client is None or not sn:
        console.print("[red]needs providers.battery=foxess with an API key and serial[/]")
        raise typer.Exit(1)
    ledger = Ledger(cfg.logging.ledger_path, cfg.logging.decision_log_path)
    today = datetime.now(cfg.site.tz).date()

    async def _run():
        out = []
        try:
            for k in range(days, 0, -1):
                rec = await fetch_night(client, sn, today - timedelta(days=k),
                                        capacity_kwh=cfg.battery.usable_capacity_kwh, tz=cfg.site.tz)
                if rec is not None:
                    ledger.record_overnight(rec)
                    out.append(rec)
        finally:
            await client.aclose()
        return out

    t = Table(title="Nights recorded", header_style="bold")
    for col in ("night", "21:00", "04:00", "drain kWh/h", "low", "11:00", "bought", "full by"):
        t.add_column(col, justify="right" if col != "night" else "left")
    for r in asyncio.run(_run()):
        t.add_row(r.date, f"{r.soc_21:.0f}%", f"{r.soc_04:.0f}%", f"{r.drain_kwh_per_h:.2f}",
                  f"{r.low_soc:.0f}% {r.low_at}", f"{r.soc_11:.0f}%", f"{r.import_kwh:.2f}", r.full_at or "-")
    console.print(t)


@app.command()
def run(config: Path | None = ConfigOpt, log_level: str = typer.Option("INFO", "--log-level")) -> None:
    """Start the scheduler daemon."""
    cfg = _load(config, log_level)
    scheduler = _build_scheduler(cfg)
    console.print(f"[bold green]ZEROHERO control running[/] — site {cfg.site.name}, "
                  f"tz {cfg.site.timezone}, controller {scheduler.controller.name}")
    asyncio.run(scheduler.start())


@app.command()
def serve(
    config: Path | None = ConfigOpt,
    log_level: str = typer.Option("INFO", "--log-level"),
    host: str | None = HostOpt,
    port: int | None = PortOpt,
) -> None:
    """Start the scheduler plus the HTTP status/override API."""
    import uvicorn

    from .api import build_app

    cfg = _load(config, log_level)
    # In a container the API must bind 0.0.0.0 to be reachable from the host,
    # while the default stays 127.0.0.1 so a bare `zerohero serve` is not
    # exposed on the LAN by accident.
    if host:
        cfg.api.host = host
    if port:
        cfg.api.port = port
    scheduler = _build_scheduler(cfg)
    http = build_app(scheduler)

    async def _main() -> None:
        server = uvicorn.Server(
            uvicorn.Config(http, host=cfg.api.host, port=cfg.api.port, log_level="warning")
        )
        await asyncio.gather(scheduler.start(), server.serve())

    console.print(f"[bold green]API on http://{cfg.api.host}:{cfg.api.port}/status[/]")
    asyncio.run(_main())


@app.command("verify-free-window")
def verify_free_window(
    config: Path | None = ConfigOpt,
    log_level: str = typer.Option("INFO", "--log-level"),
) -> None:
    """Audit and watch the 11:00-14:00 free charging window, right now.

    Read-only unless strategy.free_window_assurance.remediate is on. Costs about
    19 of the 1440 daily FoxESS calls. Run it standalone, or let `zerohero run`
    fire it on schedule every day.
    """
    from .clock import RealClock
    from .data_providers import build_telemetry_provider
    from .free_window import FreeChargeAssurance
    from .ledger import Ledger

    cfg = _load(config, log_level)
    # Read-only unless remediation is explicitly enabled: the audit and the
    # behaviour checks never need write access, so do not take any.
    if cfg.strategy.free_window_assurance.remediate and cfg.controller.type != "printing":
        from .controllers import build_controller

        controller = build_controller(cfg)
    else:
        controller = _shadow_controller(cfg)
    assurance = FreeChargeAssurance(
        cfg,
        telemetry=build_telemetry_provider(cfg),
        controller=controller,
        clock=RealClock(cfg.site.tz),
        ledger=Ledger(cfg.logging.ledger_path, cfg.logging.decision_log_path,
                      cfg.logging.samples_path),
    )
    outcome = asyncio.run(assurance.run_window())

    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column(style="dim")
    t.add_column()
    t.add_row("verdict", "[bold green]OK[/]" if outcome.ok else "[bold red]PROBLEM[/]")
    t.add_row("schedule audit", "pass" if outcome.schedule_ok else "[red]fail[/]")
    t.add_row("SOC", f"{outcome.start_soc_pct:.0f}% -> {outcome.final_soc_pct:.0f}% "
                     f"(reachable {outcome.achievable_soc_pct:.0f}%)")
    t.add_row("energy added", f"{outcome.energy_added_kwh:.1f} kWh at $0.00")
    t.add_row("checks charging", f"{outcome.charging_samples}/{outcome.samples}")
    if outcome.unclaimed_free_kwh > 0.1:
        t.add_row("unclaimed", f"{outcome.unclaimed_free_kwh:.1f} kWh "
                               f"(${outcome.missed_value_aud:.2f} to buy later)")
    console.print(t)
    for f in outcome.findings:
        console.print(f"  [dim]•[/] {f}")


def _shadow_controller(cfg: AppConfig):
    """Printing controller that can still READ the real FoxESS schedule."""
    from .controllers.printing import PrintingBatteryController
    from .foxess_client import CallBudget, FoxESSClient

    ctl = PrintingBatteryController()
    fox = cfg.providers.foxess
    if cfg.providers.battery == "foxess" and fox.api_key and fox.serial_number:
        client = FoxESSClient(
            fox.api_key, base_url=fox.base_url, timezone=cfg.site.timezone,
            budget=CallBudget(daily_limit=fox.daily_call_limit, reserve=fox.call_reserve),
        )

        async def _read():
            return (await client.scheduler_get(fox.serial_number)).get("groups") or []

        ctl.attach_schedule_reader(_read)
    return ctl


@app.command("foxess-discover")
def foxess_discover(
    config: Path | None = ConfigOpt,
    api_key: str | None = ApiKeyOpt,
) -> None:
    """List the inverters on your FoxESS account and check the API key works.

    Costs 2 of your 1440 daily API calls.
    """
    from .foxess_client import CallBudget, FoxESSClient, FoxESSError

    cfg = _load(config, "WARNING")
    key = api_key or cfg.providers.foxess.api_key
    if not key:
        raise typer.BadParameter(
            "no API key. Set providers.foxess.api_key in config.yaml, export FOXESS_API_KEY, "
            "or pass --api-key. Generate one in the FoxESS Cloud portal under "
            "User Profile -> API Management."
        )

    client = FoxESSClient(
        key, base_url=cfg.providers.foxess.base_url, timezone=cfg.site.timezone,
        budget=CallBudget(daily_limit=cfg.providers.foxess.daily_call_limit),
    )

    async def _run():
        devices = await client.device_list()
        rows = []
        for d in devices:
            sn = str(d.get("deviceSN") or d.get("sn") or "?")
            probe = {}
            if d.get("hasBattery"):
                from .data_providers.foxess import VARIABLES

                try:
                    probe = await client.real_query(sn, VARIABLES)
                except FoxESSError as exc:
                    probe = {"error": str(exc)}
            rows.append((d, sn, probe))
        await client.aclose()
        return rows

    try:
        rows = asyncio.run(_run())
    except FoxESSError as exc:
        console.print(f"[bold red]FoxESS API error:[/] {exc}")
        console.print("[dim]A signature error usually means the key is wrong or the "
                      "server clock is skewed — the signature embeds a millisecond timestamp.[/]")
        raise typer.Exit(1) from exc

    if not rows:
        console.print("[yellow]no devices on this account[/]")
        raise typer.Exit(1)

    t = Table(title="FoxESS devices", header_style="bold")
    for col in ("serial", "type", "battery", "PV", "status"):
        t.add_column(col)
    status_names = {1: "online", 2: "fault", 3: "offline"}
    for d, sn, _ in rows:
        t.add_row(sn, str(d.get("deviceType", "?")),
                  "yes" if d.get("hasBattery") else "no",
                  "yes" if d.get("hasPV") else "no",
                  status_names.get(d.get("status"), str(d.get("status"))))
    console.print(t)

    for _d, sn, probe in rows:
        if not probe:
            continue
        if "error" in probe:
            console.print(f"[yellow]{sn}: live read failed — {probe['error']}[/]")
            continue
        console.print(f"\n[bold]Live reading for {sn}[/]")
        for k, v in sorted(probe.items()):
            console.print(f"  [dim]{k:24s}[/] {v}")
        console.print(
            f"\n[green]Add this to config.yaml:[/]\n"
            f"  providers:\n    battery: foxess\n    foxess:\n"
            f"      api_key: ${{FOXESS_API_KEY}}\n      serial_number: \"{sn}\"\n"
            f"  controller:\n    type: foxess"
        )


@app.command("modbus-probe")
def modbus_probe(
    config: Path | None = ConfigOpt,
    host: str | None = typer.Option(None, "--host", help="Overrides providers.foxess.modbus.host"),
    port: int | None = typer.Option(None, "--port"),
    unit_id: int | None = typer.Option(None, "--unit-id"),
    samples: int = typer.Option(3, help="Modbus reads to take, to show the values are live"),
    interval: float = typer.Option(5.0, help="Seconds between Modbus reads"),
    cloud: bool = typer.Option(True, help="Also read the cloud once to compare (1 API call)"),
) -> None:
    """Read the inverter over local Modbus and check it before trusting it.

    Two checks. The power balance (solar + battery + grid ~= load) catches a
    wrong sign or scale on its own, because it only uses the local reading. The
    cloud comparison catches a wrong SOC or battery-energy register; its power
    figures can be minutes old, so they are shown but not judged.
    """
    from datetime import datetime

    from .data_providers.foxess_modbus import FoxESSModbusTelemetryProvider
    from .modbus_tcp import ModbusError, ModbusTcpClient

    cfg = _load(config, "WARNING")
    mb = cfg.providers.foxess.modbus
    target = host or mb.host
    if not target:
        raise typer.BadParameter("no host. Pass --host or set providers.foxess.modbus.host")
    client = ModbusTcpClient(target, port or mb.port, unit_id=mb.unit_id if unit_id is None else unit_id,
                             timeout=mb.timeout_seconds)
    provider = FoxESSModbusTelemetryProvider(cfg, client)

    async def _modbus() -> list:
        readings = []
        try:
            for i in range(samples):
                if i:
                    await asyncio.sleep(interval)
                raw = await provider.read_raw()
                readings.append((raw, provider.to_telemetry(raw, datetime.now(cfg.site.tz))))
        finally:
            await provider.aclose()
        return readings

    try:
        readings = asyncio.run(_modbus())
    except ModbusError as exc:
        console.print(f"[bold red]Modbus failed:[/] {exc}")
        console.print(
            "[dim]Connection refused or timed out usually means port 502 is closed: the "
            "H3 Smart's built-in logger only serves Modbus on recent firmware (ask FoxESS "
            "support for a remote update), or the IP is not the logger's. An exception "
            "reply means the server answered but not with this register map.[/]"
        )
        raise typer.Exit(1) from exc

    t = Table(title=f"Modbus {target}", header_style="bold")
    for col in ("read", "SOC %", "energy kWh", "solar kW", "load kW", "battery kW", "grid kW", "balance kW"):
        t.add_column(col, justify="right")
    worst = 0.0
    for i, (_raw, tel) in enumerate(readings, start=1):
        # Hybrid inverter: PV and battery feed the AC side, the grid tops up the rest.
        balance = tel.solar_kw + tel.battery_kw + tel.grid_kw - tel.load_kw
        allowed = 0.5 + 0.08 * (tel.solar_kw + abs(tel.battery_kw))
        worst = max(worst, abs(balance) - allowed)
        t.add_row(str(i), f"{tel.soc_pct:.0f}", f"{tel.battery_energy_kwh:.2f}", f"{tel.solar_kw:.2f}",
                  f"{tel.load_kw:.2f}", f"{tel.battery_kw:+.2f}", f"{tel.grid_kw:+.2f}",
                  f"[{'green' if abs(balance) <= allowed else 'red'}]{balance:+.2f}[/]")
    console.print(t)
    raw = readings[-1][0]
    console.print("[dim]raw: " + ", ".join(f"{k}={v:g}" for k, v in raw.items()) + "[/]")
    console.print("[dim]battery + = discharging, grid + = importing[/]")

    problems = []
    if worst > 0:
        problems.append("power does not balance: a sign or scale is wrong, so do NOT enable this yet")

    if cloud:
        from .data_providers import build_telemetry_provider
        from .data_providers.base import ProviderError
        from .data_providers.foxess import FoxESSTelemetryProvider

        cfg.providers.foxess.modbus.enabled = False
        try:
            cloud_provider = build_telemetry_provider(cfg)
            if not isinstance(cloud_provider, FoxESSTelemetryProvider):
                raise ProviderError("providers.battery is not foxess")

            async def _cloud():
                try:
                    return await cloud_provider.read(datetime.now(cfg.site.tz))
                finally:
                    await cloud_provider.aclose()

            c = asyncio.run(_cloud())
        except ProviderError as exc:
            console.print(f"[yellow]cloud comparison skipped: {exc}[/]")
        else:
            m = readings[-1][1]
            ct = Table(title="Modbus vs cloud (cloud power can be ~5 min old)", header_style="bold")
            for col in ("", "modbus", "cloud", "diff"):
                ct.add_column(col, justify="right")
            for label, a, b, tol in (
                ("SOC %", m.soc_pct, c.soc_pct, 2.0),
                ("energy kWh", m.battery_energy_kwh, c.battery_energy_kwh, 1.5),
                ("solar kW", m.solar_kw, c.solar_kw, None),
                ("load kW", m.load_kw, c.load_kw, None),
                ("battery kW", m.battery_kw, c.battery_kw, None),
                ("grid kW", m.grid_kw, c.grid_kw, None),
            ):
                bad = tol is not None and abs(a - b) > tol
                if bad:
                    problems.append(f"{label} disagrees with the cloud by {a - b:+.2f}")
                ct.add_row(label, f"{a:.2f}", f"{b:.2f}", f"[{'red' if bad else 'dim'}]{a - b:+.2f}[/]")
            console.print(ct)

    if problems:
        for p in problems:
            console.print(f"[bold red]✗[/] {p}")
        raise typer.Exit(1)
    console.print(
        f"[green]✓ Modbus readings are consistent.[/] To use them, set in config.yaml:\n"
        f"  providers:\n    foxess:\n      modbus:\n        enabled: true\n        host: {target}\n"
        f"  strategy:\n    control_interval_seconds: 10\n"
        f"  controller:\n    min_lower_interval_seconds: 30"
    )


def _build_scheduler(cfg: AppConfig):
    from .data_providers.simulated import SimulatedSite
    from .scheduler import ZeroHeroScheduler
    from .simulation.profiles import SCENARIOS, load_curve, solar_curve

    if cfg.providers.battery == "simulated":
        console.print("[yellow]providers.battery='simulated' — running against the synthetic "
                      "site. Set a real provider in config.yaml for production.[/]")
        scenario = SCENARIOS[cfg.simulation.scenario if cfg.simulation.scenario in SCENARIOS else "summer"]
        telemetry = SimulatedSite(
            cfg,
            solar_kw_at=solar_curve(scenario, cfg.site.tz),
            load_kw_at=load_curve(scenario),
            start_soc_pct=cfg.simulation.start_soc_pct,
        )
    else:
        from .data_providers import build_telemetry_provider

        telemetry = build_telemetry_provider(cfg)
    return ZeroHeroScheduler(cfg, telemetry)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
