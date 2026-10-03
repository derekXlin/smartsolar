# zerohero_dynamic_control

Dynamic battery export control for a **GloBird ZEROHERO** customer in **an Australian site**
(Ausgrid, 47 kWh battery, 10 kW hybrid inverter).

It does two things every day:

1. **Guarantees the $1 ZeroHero credit** by holding grid import under **0.03 kWh in
   every clock hour** of 18:00–21:00.
2. **Maximises the financial outcome of the rest of the battery** — selling the energy
   that tomorrow's free charging window would otherwise strand, and keeping the energy
   that will displace expensive import.

```bash
pip install -e .
zerohero economics      # where the money is, on your actual rates
zerohero simulate       # run five evenings through the real control loop
```

**[Architecture](docs/ARCHITECTURE.md)** — design, the physics, safety layers, and how
to port it to another inverter or tariff.

---

## Why this is an arbitrage, not an SOC-maximisation problem

The rates below come from a real GloBird ZEROHERO invoice (15-Jul-2026 → 11-Aug-2026,
NSW/Ausgrid) and are GST-inclusive. **Check them against your own bill** — plans
change and rates differ by state and network.

| Line | Window | Rate |
|---|---|---|
| Daily supply charge | — | **$1.58400/day** |
| Import — offpeak | 11:00–14:00 | **$0.00000/kWh** |
| Import — shoulder | 14:00–16:00, 23:00–11:00 | $0.40700/kWh |
| Import — peak | 16:00–23:00 | $0.52800/kWh |
| Export — FiT | 16:00–23:00 | −$0.02000/kWh |
| Export — FiT | 23:00–16:00 | **$0.00000/kWh** |
| Export — Super Export top up (Step 1) | 18:00–21:00, first ~15 kWh | −$0.08000/kWh |
| ZeroHero credit | 18:00–21:00 compliance | −$1.00/day |

Three consequences drive the whole design:

1. **Import is free 11:00–14:00 and export is worth $0.10/kWh 18:00–21:00.** That is a
   ~$0.09/kWh round-trip margin, capped at 15 kWh/day by Super Export and 30 kWh by the
   3 h × 10 kW window. Filling that cap is worth **$1.50/day**.
2. **Export outside 16:00–23:00 pays literally nothing.** Generating more solar does not
   help; *moving* export into the window does.
3. **A kWh kept in the battery is worth up to $0.528** — but only while it actually
   displaces import. Once the house's overnight need is met and tomorrow's free window
   can refill the pack anyway, that kWh is *stranded* and is worth exactly its $0.10
   export value. Selling it is then strictly correct.

So the objective is neither "maximise SOC at 21:00" nor "export as much as possible".
It is: **sell precisely the stranded energy, keep the rest, and never import during the
window.** That comparison lives in
[`marginal_value_of_stored_energy`](zerohero_dynamic_control/tariff.py).

### Where this site stands today

From the same invoice (28-day averages):

| | Now | Target |
|---|---|---|
| ZeroHero credit | 25 of 28 days ($3 lost) | every day |
| Super Export | 4.50 kWh/day | 15.00 kWh/day |
| Paid import (peak + shoulder) | 1.98 kWh/day = $0.80 | ~0 |
| **Net** | **$1.06/day cost** | **$0.92/day earned** |

`zerohero economics` prints this from your config. The headline swing is about
**$700–900/year**, and the largest single controllable line is the **$20.88 shoulder
import** — energy bought at $0.407 that the battery should have been carrying.

---

## The physics: simultaneous solar + load + battery under a 10 kW limit

The full derivation is in the module docstring of
[`decision_engine.py`](zerohero_dynamic_control/decision_engine.py). The short version:

The site obeys one balance at every instant (battery positive = discharging):

```
solar + battery = load + export            (1)
grid = load - solar - battery              (2)      grid > 0 is import
```

**Holding the credit** means `grid ≤ 0`, i.e.

```
battery ≥ load - solar                     (3)      "net load"
```

**The hybrid inverter's 10 kW limit** applies to PV and battery *together*, because they
share one AC port:

```
solar + battery ≤ 10 kW                    (4)
⇔ load + export ≤ 10 kW                    (5)
```

Equation (5) is the trap. **Solar does not give you export headroom on a hybrid — it
competes for the same 10 kW.** At 18:00 in January with 4 kW of PV still coming in, the
battery can contribute at most 6 kW no matter how full it is. What solar *does* buy you
is battery *energy*: every kW of PV is a kW the battery does not have to supply.

Add the network export limit and the admissible battery power in any slot is:

```
max(0, net_load + margin)  ≤  battery  ≤  B_max

B_max = min( battery_max_discharge,
             10 kW - solar,                        [hybrid only]
             grid_export_limit + load - solar )     (7)
```

Plus the energy constraint, which is where conversion losses bite:

```
dc_drawn = ac_delivered / discharge_efficiency                    (8)
dc_drawn ≤ (soc - reserve)/100 × usable_capacity_kwh              (9)
```

### The control law

The 17:50 plan is a forecast; reality diverges. The loop therefore closes on the **grid
meter**, which is the number the retailer actually bills:

```
error    = grid_kw - (-planned_export_kw)
setpoint = current_battery_kw + error            # gain of exactly 1
```

The gain of 1 is not a tuning parameter — adding 1 kW of discharge removes exactly 1 kW
of import. The loop is deadbeat in one step. On top sit three protections: a standing
export margin so the meter never sits at exactly zero, **escalation of that margin as the
hour's 0.03 kWh allowance is consumed**, and an energy guard that drops opportunistic
export the moment the remaining charge stops comfortably covering the window.

### Why the hourly rule changes the control problem

0.03 kWh is **1.8 kW for one minute**, or 180 W for ten. And the rule is **per clock
hour**: a spike at 18:05 burns the 18:00 hour outright and cannot be "made up" later.
[`credit_monitor.py`](zerohero_dynamic_control/credit_monitor.py) integrates import into
per-hour buckets and reports headroom as a fraction, so the loop escalates long before
the limit.

---

## Project layout

```
zerohero_dynamic_control/
├── decision_engine.py      the optimiser + the physics derivation
├── tariff.py               ZEROHERO rates; marginal value of stored energy
├── economics.py            daily P&L in dollars
├── credit_monitor.py       per-clock-hour 0.03 kWh compliance tracking
├── free_window.py          11:00-14:00 config audit + behaviour assurance
├── runtime.py              live control loop + free-charge-window runner
├── scheduler.py            APScheduler: 17:50 decision job, 11:00 charge job
├── solar_geometry.py       NOAA sunrise/sunset, no network dependency
├── curves.py               forecast interpolation
├── clock.py                RealClock / SimClock (same loop in prod and sim)
├── ledger.py               JSONL decisions, samples, daily outcomes
├── cli.py  api.py  config.py  models.py  logging_setup.py
├── Dockerfile  docker-compose.yml  docker-compose.nas.yml
├── .github/workflows/     docker-publish (GHCR, multi-arch) · ci
├── foxess_client.py        signed FoxESS transport, rate limits, 1440/day budget
├── data_providers/         base · static_forecast · open_meteo · foxess · simulated
├── controllers/            base (+SafetyWrapper) · printing · foxess
│                           · homeassistant · tesla_fleet · simulated
└── simulation/             profiles (5 the reference site scenarios) · harness
tests/                      150 tests
config.yaml
```

---

## Running it

### Simulation (works immediately, no hardware)

```bash
zerohero simulate                    # all five scenarios
zerohero simulate -s summer          # one
zerohero simulate --forecast-bias 0.7 --load-bias 1.3    # stress a bad forecast
```

The harness runs the **real** `DecisionEngine`, `EveningRunner`, `CreditMonitor` and
`SafetyWrapper` against a physical model of the battery and inverter — including the
shared AC port, conversion losses, a finite ramp rate and the BMS floor. It is not a
mock of the control logic.

Scenarios, calibrated against your plant reports and invoice:

| Scenario | Date | Sunset | What it tests |
|---|---|---|---|
| `summer` | 15 Jan | 20:09 | Residual PV competing for the inverter port |
| `winter` | 21 Jun | 16:55 | Zero PV, heating load — battery carries all 3 h |
| `cloudy` | 27 Sep | 17:55 | Pack never filled; the energy guard |
| `high_load` | 10 Mar | 19:21 | 8.5 kW load collapses export headroom to 1.5 kW |
| `low_soc` | 21 Jun | 16:55 | Unwinnable window — must abandon cleanly |

### Live

```bash
zerohero foxess-discover   # verify the API key, list inverters, do a live read
zerohero plan       # build tonight's decision, command nothing
zerohero run        # scheduler daemon: 17:50 decision + 11:00 free charge
zerohero serve      # the above plus the HTTP API
zerohero ledger     # recent daily outcomes and credits secured
zerohero bill 2026-10-02 --paid --total 0.49 --topup -0.24
                    # record GloBird's figures; they override the estimates
```

API (`pip install -e '.[api]'`):

```
GET  /status              current decision, telemetry, per-hour credit headroom
GET  /plan                dry-run the engine now
GET  /ledger?limit=14     recent outcomes
GET  /economics           the tariff model and the daily best case
POST /override            {"power_kw": 4.5}
POST /override/clear
POST /mode                {"mode": "self_consumption"}
```

---

## FoxESS setup

```bash
export FOXESS_API_KEY=...          # FoxESS Cloud -> User Profile -> API Management
zerohero foxess-discover           # lists inverters, does a live read, prints your config
```

### Where the API key lives

Not in `config.yaml`. That file holds `api_key: ${FOXESS_API_KEY}`, which is expanded
from the **process environment** — so the key only ever exists in `.env`, which is
gitignored, and in the running container's environment.

`.env` uses plain `KEY=VALUE` with **no `export` prefix**:

```
FOXESS_API_KEY=your-key-here
```

* **Docker:** Compose reads `.env` from the directory you run it in and substitutes it
  into the compose file automatically. Nothing else to do — just put `.env` beside
  `docker-compose.yml`. The key is passed to the container as an env var and is never
  baked into the image (`.dockerignore` excludes `.env`).
* **Local shell:** `set -a; source .env; set +a`. Plain `source .env` sets a shell
  variable that child processes cannot see, so `zerohero` would not find it.

If the variable is missing, config loading fails immediately naming it, rather than
surfacing later as a FoxESS `40256 illegal signature` — which reads like a wrong key
rather than an absent one and sends you debugging the signature code.

Then set in `config.yaml`:

```yaml
providers:
  battery: foxess
  foxess:
    api_key: ${FOXESS_API_KEY}
    serial_number: "YOUR-SN"
controller:
  type: foxess
```

### How control actually works on FoxESS

FoxESS has a plain `WorkMode` setting, but it takes no power argument. The **scheduler**
is the only surface that accepts a real discharge power, and it gives us two things:

| Field | Meaning |
|---|---|
| `fdPwr` | force-discharge power, in **watts** → our kW setpoint |
| `fdSoc` | force-discharge **stop SOC**, in % → a hardware-enforced floor |

`fdSoc` is the valuable one. **The floor lives in the inverter, not in our loop.** If this
process crashes at 19:30, the Wi-Fi drops, or the cloud API goes down mid-window, the
battery still will not run itself flat. The controller sets it on every command.

### The free window is verified, not driven

Your 11:00–14:00 charging is a `ForceCharge` group in the FoxESS app, so this
controller does not touch it. It **checks** it instead, because that window is the
most valuable three hours of the day — ~30 kWh at $0.00 that would otherwise cost
$0.407–$0.528 — and it fails silently: the battery just quietly arrives at 18:00 half
full and the evening plan shrinks.

Two checks, because they fail independently:

| | When | Cost | Catches |
|---|---|---|---|
| **Configuration audit** | 10:50, once | 1 call | Group deleted, `enable` cleared, times edited, only partial coverage |
| **Behaviour checks** | 11:00–14:00, every 10 min | 18 calls | Battery idle with headroom, charging from PV rather than free import, SOC short at 14:00 |

Config can pass while behaviour fails (full battery, BMS limit, grid fault), and
behaviour can pass while config is wrong (sunny day charging from PV masks a disabled
ForceCharge group until the first dull one). Checking only one would miss real failures.

At 14:00 it scores the window and prices the gap at **what you will pay for that energy
instead** (~$0.42/kWh overnight blend), not at the export rate — so a window that ends
at 60% SOC is reported as roughly `$8 of free energy left on the table`, which is the
number that actually motivates a fix. Results go to `zerohero ledger` and `/status`.

`remediate` is **off** by default: the FoxESS app owns that window, and two things
writing the same scheduler is a good way to produce surprises. Turn it on once the audit
has been clean for a week or two.

### Three FoxESS-specific constraints the code handles

**1. The signature uses literal backslash characters.** The docs say
`md5(url + \r\n + token + \r\n + timestamp)`, and they mean the *literal four
characters* `\`,`r`,`\`,`n` — not a real CRLF. Writing `"\r\n"` in most languages emits
control bytes and every request fails auth with no useful error. `_signature()` uses a raw
string deliberately, and `test_signature_uses_literal_backslash_escapes` pins both forms
so it can never silently regress.

**2. 1440 API calls per day, per inverter.** Queries are capped at 1/sec and writes at
1/2sec. A naive 60-second poll running 24/7 needs exactly 1440 calls and would consume the
whole allowance. Because the scheduler only runs during the two windows, our budget is:

| | calls |
|---|---|
| free-window assurance (1 audit + 18 checks) | 19 |
| credit window 18:00–21:00 @ 60 s | 180 |
| control writes (deadbanded) | ~40 |
| baseline save + restore | 2 |
| **total** | **~240 of 1440** |

`CallBudget` tracks spend and refuses routine calls once only `call_reserve` (120) remain,
so close-out and schedule restore can *always* run. A quota-blocked setpoint nudge is
logged and skipped rather than aborting the window — a few cents against a $1 credit.

**3. `scheduler/enable` replaces the entire group list.** There is no per-group patch, so
a naive write would wipe your 11:00–14:00 ForceCharge group. Losing it costs a whole day
of $0.00 energy bought back later at $0.407–$0.528, so restoring at 21:00 is not enough
on its own — a failed restore would leave it missing until someone noticed.

The controller therefore **merges**: it keeps every existing group that does not overlap
18:00–21:00 and appends its own. Your free-charge group survives even if close-out never
runs at all. The baseline is still restored at 21:00, but as a tidy-up rather than the
only thing standing between a bug and a lost charging window.

## Other batteries

Control sits behind one abstraction:
[`controllers/base.py`](zerohero_dynamic_control/controllers/base.py). A concrete
controller answers three questions — `set_mode`, `set_power`, and `capabilities()`.

`capabilities()` matters. Some hardware cannot take a continuous power setpoint at all.
The loop reads the flags and **falls back from fine power modulation to coarse SOC-target
control automatically**, so a limited controller degrades in quality rather than failing.

| Target | Status | Notes |
|---|---|---|
| `foxess` | **working** | Scheduler `fdPwr`/`fdSoc`; true power setpoint + hardware floor |
| `printing` | **working** (default) | Prints actions; runnable out of the box |
| `homeassistant` | **working** | Map a mode `select` and a power `number` in config |
| `tesla_fleet` | documented skeleton | No continuous power setpoint; steer depth via `backup_reserve_percent` |
| `modbus` | telemetry only | Local FoxESS H3 Smart reads over Modbus TCP (`providers.foxess.modbus`), cloud as fallback; verify with `zerohero modbus-probe`. Control still goes through the cloud scheduler |

Every command passes through `SafetyWrapper`, which clamps to the inverter limit and
blocks discharge below the hard SOC floor regardless of what the loop asks for.

---

## Using this at your own site

This was built and proven against **one** system: a FoxESS H3-10.0-Smart with a 47 kWh
pack in NSW on GloBird ZEROHERO. Everything site-specific lives in `config.yaml`
and the environment — no personal data is committed — but four things need your input
before it will do anything sensible:

| What | Where | How to find it |
|---|---|---|
| Your config file | `cp config.example.yaml config.yaml` | `config.yaml` is gitignored — your site stays out of the repo |
| FoxESS API key | `FOXESS_API_KEY` env var | FoxESS Cloud → User Profile → API Management |
| Inverter serial | `FOXESS_SERIAL` env var | `zerohero foxess-discover` prints it |
| Your tariff rates | `plan.tariff` in `config.yaml` | Your own bill. **Do not trust the defaults** |
| Battery/inverter limits | `battery`, `inverter` | Nameplate, plus your DNSP export approval |

Then two site behaviours worth measuring rather than guessing — they decide how much
energy gets sold, and the defaults are calibrated to someone else's house:
`strategy.overnight_load_kw` and `strategy.morning_solar_to_battery_kwh`.

Run with `controller.type: printing` for a week first. You get real telemetry and a
full decision log with zero writes to your inverter, and `zerohero ledger` then tells
you what it *would* have done.

> **On a different plan?** `tariff.py` is a data model, not hard-coded rates. If yours
> has no ZeroHero-style credit, set `zerohero_credit_aud: 0` and the engine reduces to
> plain arbitrage — it will still refuse to sell energy that is worth more kept.

## Deploying to a NAS with Docker

```bash
# 1. Push this repo to a PRIVATE GitHub repo. The Action builds a multi-arch
#    image (amd64 + arm64) and publishes it to GHCR, where it inherits the
#    repo's visibility — private repo, private image.
git remote add origin git@github.com:YOU/smartsolar.git && git push -u origin main

# 2. On the NAS, log in once. A classic PAT with only `read:packages` is enough.
echo <PAT> | docker login ghcr.io -u YOU --password-stdin

# 3. Copy config.yaml, .env and docker-compose.nas.yml to a folder on the NAS,
#    edit the image line to your username, then:
docker compose -f docker-compose.nas.yml pull
docker compose -f docker-compose.nas.yml up -d

# 4. Check it
curl http://<nas-ip>:8787/status
docker compose -f docker-compose.nas.yml logs -f
```

`docker-compose.yml` builds locally; `docker-compose.nas.yml` pulls the published
image so the NAS needs no build tools.

### Synology NAS (Container Manager)

Synology's compose parser is older and stricter than the docker CLI's, and rejects
several things `docker-compose.yml` uses. Use `docker-compose.synology.yml.example`
instead — copy it to `docker-compose.synology.yml` and fill in your key.

Two things to get right first:

* **Use *Project*, not *Container*.** The Create Container wizard takes an image and
  will reject a compose file outright — that is what "The format of docker-compose.yml
  file(s) is invalid" means when you hit it from there.
* **Build the image over SSH first.** Container Manager Projects will not reliably
  build one for you:
  ```bash
  ssh your-nas && cd /path/to/smartsolar
  sudo docker build -t zerohero:1.0.0 .
  ```

What differs from the standard file, and why:

| Standard file | Synology file | Reason |
|---|---|---|
| `build: .` | `image: zerohero:1.0.0` | Projects want an existing image |
| `${FOXESS_API_KEY:?...}` | literal key inline | The parser supports neither `.env` lookups nor the `:?` required form |
| multi-line `healthcheck.test` | single-line `CMD-SHELL` | The list-plus-block-scalar mix fails to parse |
| (none) | `version: "3.8"` | Older DSM docker-compose requires it |
| relative `./var` | `/volume1/docker/zerohero/var` | Synology resolves relative paths differently |
| `# user:` commented | `user: "1026:100"` | DSM numbers real users from 1026, group 100; without this `/app/var` is not writable |

Because the key has to sit inline, `docker-compose.synology.yml` is gitignored and only
the `.example` template is committed. Keep it that way.

### Rebuilding: always tag the version

```bash
cd /path/to/repo && git pull
sudo docker build --build-arg ZEROHERO_BUILD=$(git rev-parse --short HEAD) \
                  -t zerohero:$(python3 -c 'import zerohero_dynamic_control as z;print(z.__version__)') \
                  -t zerohero:latest .
```

Then confirm what is actually running:

```bash
curl -s http://localhost:8787/status | python3 -m json.tool | head -4
#   "version": "1.1.0",
#   "build":   "a1b2c3d",
```

This matters more than it looks. `config.yaml` is **bind-mounted from the host**
while the code lives **inside the image**, so `git pull` applies config changes
instantly while the code stays on the old build until you rebuild. That skew once
sent a literal `${FOXESS_SERIAL}` to the API as a serial number; FoxESS replied
`errno 0` with an empty payload, and it surfaced four layers away as "telemetry
unavailable". Rebuilding into the same tag left nothing to reveal it.

Two guards now exist, but seeing the version is the one that saves you time:

- config referencing a `${VAR}` this build cannot expand fails **at startup**,
  naming the field and saying the config is newer than the image;
- an empty telemetry response names the serial it tried.

### Things that specifically bite in a container

**SIGTERM must reach Python.** `docker stop`, a compose restart, a NAS package
update and an image pull all send SIGTERM. If one lands at 19:30 the inverter is in
ForceDischarge, and a hard kill would leave it there — exporting the battery flat
overnight at $0.02/kWh to buy it back at $0.407 in the morning. The scheduler
installs SIGTERM/SIGINT handlers that close out the window and restore the owner's
schedule before exiting, the Dockerfile uses exec form so Python is PID 1, and
`stop_grace_period: 30s` leaves room for the restore call.

**The inverter frees itself anyway.** The safety model does not rely on any of the
above surviving. Every group the controller writes carries an explicit start *and*
end inside the credit window (18:00–20:59), so the inverter ends the forced mode on
its own and falls back to the all-day SelfUse group. A killed container, a crashed
NAS, a severed network and an exhausted API quota all end the same way: normal
operation at 21:00. `_assert_bounded` refuses to write an unbounded forced group,
and a test asserts it on every write.

**`fdSoc` is a deadman floor, not the hard floor.** If the process dies mid-window
the inverter keeps discharging at the last power it was given until the group
expires, so `fdSoc` is where an unattended battery stops. It is set to the
*planning* reserve (25%), not the 10% emergency minimum, so a crash costs some
charge rather than the whole pack. `minSocOnGrid` stays at 10% as the absolute
backstop, and the live loop only lowers `fdSoc` when the engine decides the credit
genuinely needs the depth.

**tzdata is mandatory.** Every window here is local wall-clock time and the AEST/AEDT
changeover moves them, but `python:slim` ships no zone database, so
`ZoneInfo("Australia/Sydney")` would raise at startup. The image installs the
`tzdata` package explicitly.

**Clock skew breaks auth.** The FoxESS signature embeds a millisecond timestamp. If
the NAS clock drifts, every call fails with `40256 illegal signature`, which reads
like a bad key. Make sure NTP is on.

**Do not expose port 8787.** `/override` and `/mode` have no authentication. Bind it
to the LAN and reach it over Tailscale or WireGuard if you need it remotely.

**Volume permissions.** `./var` holds the ledger and must outlive the container. The
image runs as UID 1000; Synology usually starts real users at 1026, so uncomment
`user:` in the compose file and set it to your `id` output if `./var` is not writable.

## Configuration

All tunables are in [`config.yaml`](config.yaml), validated by pydantic. The ones that
actually change behaviour:

| Key | Default | Effect |
|---|---|---|
| `strategy.objective` | `economic` | `economic` · `guarantee_credit_only` · `retain_overnight` · `maximise_export` |
| `strategy.allocation` | `front_loaded` | Export shape. Front-loading banks revenue early, before anything can go wrong |
| `strategy.import_safety_margin_kw` | 0.25 | Standing over-discharge. ~0.75 kWh across the window to insure $1 |
| `strategy.energy_safety_buffer_kwh` | 1.5 | Held back against load surprises |
| `strategy.overnight_load_kw` | 1.3 | **Tune from your data.** Higher → keep more, sell less |
| `strategy.morning_solar_to_battery_kwh` | 6.0 | **Tune seasonally.** Higher → more surplus is stranded → sell more |
| `battery.min_reserve_soc_pct` | 25 | Planning reserve |
| `battery.emergency_floor_soc_pct` | 10 | Hard floor, never crossed |
| `inverter.solar_shares_ac_limit` | true | **False only if PV is on a separate AC-coupled inverter** |
| `inverter.grid_export_limit_kw` | 10 | Set to your Ausgrid approval (often 5 kW/phase) |

`overnight_load_kw` and `morning_solar_to_battery_kwh` are the two knobs that decide how
much gets sold. The defaults are calibrated from your Jul–Aug invoice (42.09 kWh/day
total, only 1.98 kWh/day of it paid import), but your own interval data will beat them.
When a forecast provider is configured, morning solar is estimated from the forecast and
the static value is only a fallback.

---

## Safety and degradation

| Failure | Behaviour |
|---|---|
| One telemetry poll fails | `CachingTelemetryProvider` serves the last good reading, marked stale |
| Telemetry stale > 10 min | Blind fallback: force export at `fallback_discharge_kw` until 21:00 |
| Solar forecast fails | Assume **zero** residual solar — over-reserves battery, which cannot cost the credit |
| Load forecast fails | Static evening average |
| Controller API fails | Logged; the loop retries next tick; close-out still attempted in `finally` |
| Not enough charge to cover the window | Credit is unwinnable (the rule needs *every* hour), so stop spending on it and run self-consumption — evening peak $0.528 > overnight blend $0.424, so the charge is worth more spent tonight |
| Anything asks for > 10 kW or discharge below the floor | `SafetyWrapper` clamps and records the violation |

---

## Tests

```bash
python -m pytest -q        # 150 tests
```

Coverage worth knowing about:

- **`test_reproduces_the_reference_invoice`** — the tariff model must reproduce a real
  invoice total to the cent. If it fails, every dollar figure the engine reports is wrong.
- **`test_inverter_ac_limit_never_exceeded`** — parametrised over load, asserts both
  equation (4) and equation (5) hold in every planned slot.
- **`test_never_plans_below_emergency_floor`** — parametrised over SOC from 12% to 100%.
- **`test_inverter_limit_is_never_violated_in_practice`** — the same invariants checked
  against the *simulated hardware*, sample by sample, not just the plan.
- **`test_badly_wrong_forecast_still_secures_the_credit`** — forecast says sunny and
  quiet, reality is the opposite. The meter closed-loop, not the plan, has to save it.
- **`test_a_breach_in_one_hour_cannot_be_made_up_in_another`** — encodes the per-hour rule.
- **`test_short_high_spike_breaches_a_single_hour`** — 1.5 kW for 2 minutes = 0.05 kWh;
  one appliance start loses the day.

---

## Known gaps

- `tesla_fleet` is a documented skeleton that raises clearly rather than pretending to work.
- The FoxESS integration is written and unit-tested against a fake transport, but has
  **not yet been run against the real cloud API** — `zerohero foxess-discover` is the
  first thing to try, and it costs 2 of the 1440 daily calls.
- Local Modbus **telemetry** exists (`providers.foxess.modbus`) but has been tested only
  against a fake server built from the foxess_modbus H3 Smart register map, not yet
  against the real inverter. `zerohero modbus-probe` checks it live before you enable it.
  Control still goes through the cloud scheduler, whose own write latency is unmeasured.
- `solcast` is unimplemented.
- **Confirm the Super Export cap.** The invoice line reads "Super Export top up – **Step
  1**", which implies further steps at other rates. 15 kWh is an assumption; the billing
  data available here covers 126.06 kWh over 28 days (4.50 kWh/day), never approaching
  the cap, so it has not been observed directly.
- CSV replay of historical interval data is stubbed in config (`simulation.replay_csv`)
  but not implemented — the files in `data/` are monthly summaries, not interval data.
