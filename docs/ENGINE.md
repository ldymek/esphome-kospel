# Planning engine (v2): LLM, deterministic engine, or both

Since v2 the AppDaemon app has **two planners** and a switch that decides who writes the heater's
daily programs (program 8):

| `input_select.kospel_planer` | Who plans | Who checks |
|---|---|---|
| **LLM** (default) | the language model (Ollama), as in v1 | nobody — the app only validates the JSON |
| **Silnik** | the deterministic engine (`kospel_engine.py`) | nobody — pure math, no GPU needed |
| **Hybryda (silnik + weryfikacja LLM)** | the engine | the LLM audits the plan and may amend individual timetables |

Whatever the mode, the engine's plan is **always computed and published** to
`sensor.kospel_plan_silnika` (attributes `CO`, `CWU`, `Cyrkulacja`, `uzasadnienie`, `plan_mocy`,
`weryfikacja_llm`), so you can compare it with what the LLM did before you trust it with the heater.
Autonomy rules are unchanged: nothing is written to the live weekly maps unless the AI mode is
*Autonomiczny*; a human must enable that.

## What the engine knows

- **Prices** — the Pstryk hourly buy prices with cheap/expensive flags, plus absolute context.
- **Hot-water usage profile** — the meterless draw detector (tank-temperature drops while the heater
  is idle) keeps a per-weekday histogram of draws in `dhw_usage.json`. The engine clusters the draws
  and charges the tank in the cheapest non-expensive hour up to 5 h *before* each cluster (with a
  small penalty for distance, so it does not charge at 02:00 for a 07:00 shower if 05:00 is nearly as
  cheap). Circulation windows are placed on the clusters.
- **Thermal model of the building** — fitted daily (03:30) from 7 days of history:
  `dT_in/dt = a·(T_out − T_in) + b·P_CO + c`. From it the engine derives the time constant, how many
  hours the house can *coast* through an expensive block without dropping more than the preference
  allows, and how long a pre-heat before that block takes. Until it has ≥48 clean samples it reports
  `stan: uczenie` and the plan falls back to fixed comfort windows.
- **Tank model** — heating rate (K/h per kW) and standing loss, fitted from tank-temperature history;
  it also feeds the *degradation* diagnostic (scale/heater-element wear shows up as a slower rate).
- **Preference** — `input_select.kospel_preferencja`: *Oszczędność* (protect-only during expensive
  hours, allow 1.5 °C drop), *Balans* (Komfort− during peaks, 1.0 °C), *Komfort* (Komfort− idle,
  Komfort in windows, 0.5 °C). The same preference picks the power-cap tiers pushed to the ESP
  (expensive / normal / cheap hour → 12 / 20 / 24 kW or lower).
- **Presence** — when every `person.*` (or the `persons:` list in `apps.yaml`) has been away ≥30 min
  the plan switches to eco: CO on *Ochrona*, one DHW charge at 06:00, no circulation. Optional
  `calendar:` entity: events named *urlop*/*wakacje* force the same.
- **Comfort feedback** — the two scripts *Za zimno* / *Za ciepło* (put them on the wall panel) nudge a
  bias (±0.5 °C per press, decays ×0.9 per day) that shifts the engine's levels; the LLM prompt also
  receives them as context.

## Outputs

Each program is at most **5 time slots** (a Kospel timetable limit); gaps mean the economic level.
Levels are the heater's own: Ochrona (1), Komfort (2), Komfort− (3), Komfort+ (4). Example (Balans,
Tuesday, expensive block 17–22):

```
CO:   05:00-06:00 Komfort+ · 15:00-16:00 Komfort+ · 16:00-21:00 Komfort- · 21:00-22:00 Komfort
CWU:  02:00-03:00 Komfort · 12:00-13:00 Komfort · 15:00-16:00 Komfort
Cyrk: 06:00-08:00 · 17:00-21:00
```

`uzasadnienie` lists why each decision was made (which draw cluster, which price block, whether the
house is expected to coast through). Plans are validated (`validate_slots`) before any write: slots
must be ordered, non-overlapping, ≤5 per program and use valid levels.

## Hybrid verification

In *Hybryda* the LLM gets the engine plan plus prices, forecast, usage clusters and model state, and
must answer a strict JSON schema: `zatwierdzam` (bool), `uwagi` (list of remarks), `poprawki`
(optional corrected slot lists per timetable). Corrections are re-validated; if the JSON is invalid the
engine plan is used unchanged and the remark is logged. The schedule sensors carry a `zrodlo`
attribute so you can see who authored the program that is live (`Silnik`, `LLM`,
`Hybryda: silnik (LLM zatwierdził)`, `Hybryda: poprawka LLM`).

## Savings, diagnostics, backtest

- `sensor.kospel_oszczednosci` (daily 00:15) — yesterday's kWh and cost from the energy package vs
  two counterfactuals: the same kWh at the day's average price, and at a flat tariff
  (`input_number.kospel_taryfa_plaska`). Rolling 7-day totals are attributes.
- **Weekly digest** — Monday 08:00 persistent notification `kospel_tydzien` with the week's cost,
  savings, hot-water draws and model state.
- `sensor.kospel_diagnostyka` (daily 04:00) — system-pressure trend (< −0.03 bar/day or < 0.8 bar
  flags a leak/expansion vessel issue), tank heating-rate degradation (> 20 % slower than the
  baseline flags scale), and model-fit sanity.
- **Backtest** — toggle `input_boolean.kospel_backtest_run`: replays yesterday's real kWh against the
  engine's plan for that day and publishes `sensor.kospel_backtest` (real cost vs cost if the engine's
  power/CWU placement had been followed). Use it to decide whether to move from LLM to Silnik/Hybryda.

## Heat battery (mixing valve only)

`input_boolean.kospel_zawor_mieszajacy` + `input_number.kospel_cwu_magazyn_temp` (45–65 °C):
in the cheapest hour of the day the app raises the DHW comfort setpoint to the storage temperature
and restores it afterwards (also on autonomy disengage). **Only enable this if a thermostatic mixing
valve is installed** — 60 °C+ at the tap is a scalding hazard.

## Files & state

- `appdaemon/kospel_engine.py` — the engine (pure Python, no HA imports; unit-testable offline).
- `engine.json` in the apps directory — fitted models, bias, last plan.
- Helpers used: see `homeassistant/packages/kospel_helpers.yaml` (`kospel_planer`,
  `kospel_preferencja`, `kospel_zawor_mieszajacy`, `kospel_cwu_magazyn_temp`, `kospel_taryfa_plaska`,
  `kospel_backtest_run`).
