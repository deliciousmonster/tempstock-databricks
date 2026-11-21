# Design note: weather-signal foundation

This note records the foundation design as built: where the build departs from the plan, what it
measured, and what it leaves open. The plan and its reviews are kept in the private build record.

## Module map

`src/weather_signal/` is the wheel. Each subpackage is one seam of the platform, and a module name
says what it holds. The bundle names console scripts, never module paths, so a move changes
`pyproject.toml` and not `resources/`.

- `ingest/`: fetch, validate and land each source once (`fetch`, `sources`, `envelope`,
  `validate`, `files`), the hourly steps (`hourly`), catch-up and its Open-Meteo chunks
  (`catchup`, `backfill`, `yahoo_history`), and the budgets (`av_ledger`, `om_budget`).
- `pipeline/`: `transforms`, the PySpark the declarative pipeline in `src/weather_signal_etl/` runs,
  with the price guards it declares as SQL (`SPLIT_OK`, `PRICE_KNOWN_AT_OK`).
- `signal/`: `temperature_index` (the weighted index and its normals), `features`, `model`,
  `backtest_fit`, `gate` (the champion gate's P&L and shuffles), `champion`, `estimate`, `train`,
  `score`, `backtest` and `checks` (the check task's findings).
- `catalog/`: `ddl` (the job-owned tables and their constraints) and `setup`, which reconciles them.
- `guard/`: the append-only guard over those tables.
- `serving/`: the `v_ui_*` views, the snapshot builder, the export and the MCP server.
  `app_bridge` imports the App's integrity rule from `app/ws_ui` in a checkout: the App runs Python
  3.11 and the wheel needs 3.12, so the App can't install the wheel.
- `jobs/`: one module per bundle job (`ingest`, `score` for guard, score and check, `backtest`,
  `train`, `snapshot`), each `main` a console script.
- `agent/`: the hourly recommender on the Mac.
- `deploy/`: `weather-signal bootstrap` and its phases, and the teardown-and-reprovision proof
  (`proof`, `proof_archive`, `proof_checks`, `proof_record`, `proof_verify`).
- `research/`: the metro study and its statistics; no job runs it.
- Top level: `config` (names shared by all of the above), `asof` (what a decision may see),
  `sessions` (XNYS sessions), `spark` (the job's session, or Databricks Connect from the Mac),
  `redaction` and `cli`.

## Schemas and medallion tiers

The schema names say what each layer holds, and the tier is the Databricks convention.

| Tier | Schema | Holds |
|---|---|---|
| Bronze | `raw` | raw API responses as received (`raw_envelopes`), plus the volumes, the ledger among them |
| Silver | `daily` | observed weather, forecasts, prices, sessions and metros |
| Gold | `signals`, `app` | normals, index, signal, backtest, recommendations and agent runs, plus the `v_ui_*` views and `fn_ui_*` functions the App, dashboard and Genie read |
| Platform | `ops` | the pipeline's event log and the fill alerts; not a tier |

## Invariant

A value reaches a decision only once it was knowable at that decision's `as_of`, and a written
signal or recommendation is never changed in place. The first is enforced by `asof.visible` and the
pipeline's `known_at` bounds; the second by `appendOnly`, CHECK constraints, and a guard that reads
each job-owned table's id, state and history before and after every score.

## Notes from the framing review, folded in

1. Plan line 317 says the agent principal could unset `appendOnly` "on its own table". Read it as
   "on a table it holds `MODIFY` on (owned by the owner)": the probe shows a capability that comes
   with `MODIFY`, not with ownership. `signals.v_guard_hold` is owned by the owner too.
2. `setup` creates `signals.agent_runs` as the owner (`catalog/ddl.agent_runs`), so the agent principal
   needs no `CREATE TABLE` on `signals`. It is not guarded: it is a run log, not a decision. Its
   `outcome` and `route` CHECKs are added by `ALTER` after the create, as on the guarded tables.
3. The wheel path. Measured at the first `dev` deploy (2026-11-17): both jobs' environment and the
   pipeline's environment resolve `../dist/*.whl` to
   `/Workspace/Users/<owner>/.bundle/weather-signal/dev/artifacts/.internal/weather_signal-0.1.0-py3-none-any.whl`,
   and the ingest job ran from it on serverless environment 4.

## Departures from the plan

- **Agent grants live in `setup.grants`, not the bundle.** The bundle cannot omit them under the
  one-principal fallback, and catalog-level and table-level grants on tables the bundle does not own
  cannot be expressed there. Schema and volume grants to the runner stay in the bundle.
- **`dev` is not `mode: development`.** Development mode requires the user name in the name prefix;
  the dev target instead sets `schema_prefix: dev_`, pauses triggers by preset and uses its own
  secret scope, `weather-signal-dev`.
- **Seeds come from the wheel.** The pipeline reads `metros.csv` and `sessions.csv` packaged in the
  wheel; the `raw.seed` volume is created and granted but holds nothing yet.
- **No `normals` row-count expectation yet.** The plan's "366 rows (fail)" would fail every update
  until the ERA5 backfill lands; it goes in with stage 4.
- **ERA5 from 1961, in four anchored segments (wmt-switch, 2026-11-18).** `ERA5_START` is
  1961-01-01, its own constant: calendar years 1961 to 1990, so a 1961-1990 normal never draws on a
  scored year; the 17 slots landed under the 1991 start (1991-01-01 to 2007-12-27) cut as they were;
  2007-12-28 to 2013-06-26 in 365-day chunks; and 2013-06-27 on, unchanged. A slot once landed is
  never re-cut, and a volume that holds it is not sent again. The tail is the exception by design:
  it keeps its start and grows on a weekly grid, so each week it is a new slot that re-requests the
  landed tail's days, about 150 weighted. Previous Runs is unchanged.
- **Price histories from 1991 (wmt-switch).** `config.HISTORY_START` is 1991-01-01, and a
  benchmark listed later starts at its first session (`BENCHMARK_FIRST`); fetch, check, slot and
  landing key all use that per-ticker start. `validate.history` requires every session from the first
  one on or after the start, each with a close, refuses a bar before the start or on a non-session
  day, and refuses a close-to-close move of 40% or more, the pipeline's own split guard, so a bad
  body is refused at landing. `src/weather_signal/seed/sessions.csv` starts 1990.
- **Normals are the 1961 to 1990 mean, every year present (wmt-switch).** `signals.normals` keeps
  `n_years`, the distinct years averaged per day of year, and leaves `normal_c` null unless every
  window year that has the day contributes: 30 for days 1 to 365, the 7 leap years 1964 to 1988 for
  day 366 (`index.expected_years`). A partial ERA5 fetch therefore leaves days null instead of
  averaging fewer years. A visible forecast whose day has no normal holds with the reason
  `no_normal`, not `stale_forecast`.
- **The price parser keeps every bar.** `price_bars.from_envelope` does not drop bars that were
  incomplete at fetch time, so a forged envelope reaches the `known_at` bound and fails the update
  instead of vanishing. Landing still validates first.
- **`score` without a champion holds every target with `no_champion`**, and says so in the log; the
  train job sets `signals.stock_signal@champion` only when the walk's gate passes (Champion and gate).
- **Every task of one score run uses the run's trigger time** for `as_of`, so a slow `refresh`
  cannot move `score` and `check` to another hour.
- **`setup` order:** on a fresh target the pipeline must run once before `setup`, since the
  functions read its tables.

- **Backfill chunks have their own validators.** A chunk body must be dated from the chunk's first
  to its last day, with every column as long as `time` and no lead column null at every location; a
  body that makes a check raise is invalid. ERA5 chunks then use the steady null rule; a Previous Runs
  chunk lands with `flag = interior_null` when it holds nulls, since a hole in history does not fill
  on refetch and daily drops null points. Measured: the 2021-03-24 chunk has no lead 2 at 00:00, runs
  before the archive start.
- **Backfilled ERA5 days are known at date + 8 d.** `daily.era5_first_seen` takes, per day, the
  earliest of a steady envelope's fetch and a backfill chunk's (slot `a..b`) date + 8 d, the
  midnight built in UTC whatever the session zone. Previous Runs history needs no rule of its own:
  its `known_at` comes from valid time and lead. Open-Meteo's zone-less hours, the session seed's
  open and close, and the ERA5 bound are read as UTC too (`transform.utc_wall`, `utc_midnight`).

## Discrepancies with the plan, measured

- After the 2025-07-03 half day at a 7.5 h lag, the primary lands at 03:05, not 02:05 like SPY's 02:05
  and the second benchmark's 01:05, so all three are visible from 03:30, not 02:30
  (`tests/unit/test_features_parity.py`).
- The 17:55 and 18:24 payloads the plan names were never recorded; `test_run_known_at` uses 18:07,
  18:35 and 18:41.
- The fill signature catches a mistyped `P` only well below 12 h. On the 18:07, 18:35 and 18:41
  payloads, `max_run` is 2 at `P` from 19 to 30 h, 4 at 12 h, 6 at 10 h and 10 at 6 h. The plan's
  "12 h gives `max_run >= 6`" does not reproduce; a mistype down to 11 h passes the check
  (`tests/spark/test_daily_known_at_bounds.py`).
- **The fill signature quarantines one metro at a time; it does not halt.** `lead_not_filled` is a
  counted expectation on `daily.forecast_fill_check`. Every hour in a run of 6 or more equal to
  lead 0 goes to `daily.forecast_fill_quarantine` (reason `lead_equals_lead0_6h`) and out of
  `forecast_daily` for that metro alone. `signals.forecast_index` averages over the metros present and
  keeps a cell when they carry at least 0.97 of the population weight (`MIN_FORECAST_WEIGHT`). A
  dropped metro biases the anomaly, since the normal is the 50-metro index's. Measured on dev's
  observed daily temperatures (11,090 days, `bias.py` in the fill-check evidence): over every single
  metro and 2,000 random drops of 2 to 5, the worst bias was 3.94 °C with no floor (New York alone
  2.07) and 0.87 °C (p95 0.51) at 0.97, against a daily anomaly sd of 2.11. By season the
  break r8 review's exhaustive pass over the 2,620 subsets the floor admits gives a worst bias of
  0.87 °C against an sd of 2.90 in winter (0.30 of it) and 0.47 against 1.14 in summer (0.41): the
  floor is relatively looser in summer. Anomalies taken per
  metro against their own normals halve that at the same floor (0.51, p95 0.22) but need per-metro
  normals; the floor carries most of the cut, so it is the one built. At 0.97 a lone coincidence at
  any of the 41 lighter metros (41860 at 0.0245 among them) keeps the cell, and one at a heavier
  metro holds it. A cell the quarantine alone put under the floor (present plus quarantined weight
  reaching it) is kept with no value and `quarantined = true`; a cell short for other reasons has no
  row, as before. The signal holds on a flagged cell as `forecast_quarantined`, not `stale_forecast`.
  Measured on dev,
  2026-11-18: the Previous Runs chunk of 2022-04-20 has one such run in 16,800 lead-7 hours, metro
  41860 (an offshore grid point near San Francisco), 2022-04-29 05:00 to 10:00 UTC at 10.0 to
  10.2 °C, with leads 1 to 6 off by 0.2 to 1.1 °C. That is the weather matching a 7-day forecast to
  0.1 °C, not a filled column; the parse reads the column the request names. The Sources strip
  shows `lead_not_filled` failures as runs quarantined, not as red violations, only while
  `fill_one_metro` passed in the same update and every file fetched in the last 7 days
  (`signal.checks.FILL_ALERT_WINDOW`) quarantined one metro at one lead (`v_ui_sources`'
  `fill_shape` row); older files rest on `fill_one_metro` alone; otherwise they stay red with the worst file's shape. A filled column or a
  mistyped `P` repeats across metros: `daily.forecast_fill_summary` counts metros per envelope and
  lead, its `fill_one_metro` expectation counts in the event log, and the score job's `check` task
  fails with `forecast_filled` when an envelope fetched in the last 7 days has a lead quarantined at
  5 or more metros, which mails the owner through the job's failure notification, once per
  envelope: the task records each envelope it reports in `ops.fill_alerts` (created by `setup`, or
  by the task itself, which holds CREATE TABLE on `ops`) and skips those on later runs. It reads
  `fetched_at` as epoch seconds, since `collect()` returns naive timestamps (a Connect client's in
  local time), and `signal.checks.as_utc` reads any naive value as UTC.

## Backtest (owner, 2026-11-18)

The unscheduled `backtest` job scores every primary session in `daily.prices_daily` from
`signal_backtest.START`, 1991-01-10 (the seventh session of 1991, the first with a five-day return),
at horizon 0 and writes `signals.signal_backtest`: the `signal` columns (`ticker` among them) plus `backtest_run_id`
(`h0:<model>:<version>`), `input_kind`, `created_at`, and the P&L and gate columns (Champion and
gate). A rerun replaces its run id's rows in one
commit and leaves other run ids; `signals.signal` is not touched. The live model is
`stock_signal@champion`, which the train job registers; the backtest scores an explicit model
instead, `threshold_wf` version 2 (`signal/backtest_fit`): a ThresholdModel refit at the
first session of each calendar year, choosing the sign (+1 or -1) and threshold (0.5, 1, 1.5, 2 or
3 °C) whose trades earned the most gross return from close t+1 to close t+2 over every earlier
session whose t+2 close was known at that refit, with at least 30 trades (version 1 used close t to
close t+1, which a 21:30 UTC decision on t cannot trade); with no such pair it holds (on WMT, 2013 had 179 buys, 49 sells and 24 holds, counted before the
staleness rule below, which holds 2013 to 07-03). Each
row's `features` records `model_coef` and `model_threshold_c`. The grid, the yearly cadence and the
objective were set before looking at results; they are not tuned. Each session decides at 21:30 UTC (`asof.backtest_as_of`)
and sees only rows whose `known_at` is at or before it, as the live frame does. The forecast feature
is the lead-2 cell for the session (`input_kind = 'forecast'`) when one is visible; otherwise it is
the latest observed ERA5 anomaly known by then (`'observed'`), which is the day about 8 days before
the session, since a backfilled ERA5 day is known at its date plus 8 days. An observed row is a
persistence input, not a forecast: it says what the weather was a week earlier. `'none'` means
neither was known, and the row holds as `no_input`; an observed day with no normal (Dec 31 of a
leap year is day 366, which the normals leave null) holds as `no_normal`. An observed day more than
14 days before the session (`asof.MAX_OBSERVED_AGE`, `asof.observed_stale`) holds as `stale_input`,
keeping its anomaly and `observed_date` in `features`, and takes no part in the yearly fit
(backtest-stale, 2026-11-20): before it, a gap in the ERA5 load left sessions buying on an
anomaly up to 2,015 days old. ERA5 is now complete on dev and no observed call is stale
(`presentation/claims.md` rows 51 and 6f). An observed
call normally lags 8 days and up to 14 over holidays, so the limit holds none of those. The live
frame reads only the fitted-lead forecast for its own target, never an observed day, so it has no
such input to age. The task logs the rows by kind and by those three reasons, and fails without
writing when `no_input` and `no_normal` pass 1% of sessions, so an empty forecast or normals table
cannot pass as a quiet run of holds. `stale_input` is logged but outside that share, so a past gap
in ERA5 shows as holds in History rather than stopping the run; when the newest session holds on
`stale_input`, the feed has stopped and the task fails without writing. Training reads the same
walk the backtest job runs (`backtest.walk`), so stale sessions leave its training set too. `features` is strict JSON (a NaN is
null), since Spark's `get_json_object` returns null for a whole document with a bare NaN.

`app.v_ui_backtest`, which `setup` creates over the table, shows only the newest run (by
`created_at`), so two run ids never double a session in the App's History. Only the App reads it:
its SELECT grant is in the App bundle with the other `v_ui_*` grants, and it is not in the UI's
`VIEWS`, so the agent SQL gate, Genie and the snapshot don't see it. On dev the backtest job runs
as the runner, as prod's jobs do, so the runner's MODIFY on the table is exercised there.

The normals are 1961-1990, which end before the first scored session, so no session compares
against a mean that used its own or later years (`presentation/claims.md` row 25). Not fixed, stated: the observed value is the
latest revision `weather_observed_daily` holds, not the value as first seen; ERA5 revisions are
small, unmeasured here. The fit pools observed and forecast sessions as one feature, though they
are different signals. Archived forecasts start 2021-03-24 on dev and cover only the cells the
backfill has landed so far.
The normals key on day of year, so after Feb 28 a leap year compares each day with the next
calendar day's normal, and Dec 31 of a leap year has none; the live forecast index shares this.

## Champion and gate (champion-gate, 2026-11-20)

Parity with the reference definitions (private build record),
which the owner ruled Databricks builds. Each backtest row carries `pos` (+1 buy, -1 sell, 0 hold),
`ret`, WMT's raw simple return from close t+1 to close t+2 over the primary's price rows, and
`pnl = pos * ret - 0.001 * [pos != 0]` (`signal/gate`). A session whose t+2 close is not in the table
yet has a null `ret` and takes no part. The gate shuffles the order of the calendar years' blocks of
positions 200 times from `random.Random(0)`, each shuffle paying the same costs over the same
returns, and passes only when the real net total is above the 95th percentile of the 200 totals
(linear interpolation) and above 0; the draws and percentile follow the reference's `pattern_stats`, so
equal daily inputs give equal numbers. The run's `gate_total`, `gate_p95`, `gate_pass`, `gate_n`
(sessions with a return) and `gate_trades` repeat on every row of the run, and `app.v_ui_status`
reads the newest run's, so the App, the dashboard and Genie read the gate from a view they already
have. The App's hit rate (`app/ws_ui/measure.py`) is unchanged and still scores each call against the
next session's close.

The train job runs the same walk and registers its latest refit (`train.deployed`): the model of the
current calendar year, fitted at that year's first session on labels known then. Its tags are
`params`, `fit`, `fit_as_of`, `coef`, `threshold`, `n_train` (labels known at the refit),
`period_through` (31 December of the refit year), `gate_pass`, `gate_total`, `gate_p95`, `gate_n`,
`gate_trades` and `est_basis`. A version is registered whatever the gate says, so the failed state is
recorded; `@champion` moves to it only when `gate_pass` is true, after the logged pyfunc's actions
match its params. On a failed gate train removes `@champion`. No alias is the one state the score
job already read as "no champion" (`RESOURCE_DOES_NOT_EXIST`); a second alias or a sentinel version
would be a state for the score, deploy and baseline to agree on, with nothing gained. A newest
version whose tags all equal the fit's is reused rather than registered again. The score trades the
champion only for targets on or before its `period_through` and holds every other target, and every
target when there is no alias, with the reason `no_champion` (`signal/champion`). The train job's
schedule (23:45 UTC, 2 to 9 January) registers the new year's refit, so a live set holds targets
from 1 January until the first run after the year's first session. Deploy's train
phase counts either a champion of `threshold_wf v2` or, with no alias, a newest version tagged
`gate_pass=false` as done. The deploy baseline compares `params`, `fit`, `fit_as_of` and
`period_through`, not `gate_pass`, which sessions landing after the baseline can flip, and digests
only the newest backtest run, since a migrated scope keeps v1's rows beside v2's.

Every signal row carries `est_price`, `est_reason` and `est_basis` (`signal/estimate`). For a buy or
sell, `move = mean_gross - pos * 0.001`, where `mean_gross` is the mean `ret` over the walk's days on
that side, each rounded to 8 dp as the reference rounds them, and `est_price = close * (1 + move) ** sessions`, from the last WMT close the features saw
(`last_close`, `last_close_date`) over the exchange sessions after that close's date through the
target day. `est_basis` is JSON with `close`, `close_date`, `side`, `sessions`, `mean_gross`, `cost`,
`move`, `n_trades` and `fit_as_of`. With no estimate, `est_price` and `est_basis` are null and
`est_reason` names why, in the reference's words: `no_champion`, `hold`, `no_basis` (the basis tag's
`fit_as_of` is not the champion's), `few_trades` (under 30 days on the side), `no_edge_after_costs`
(a buy's move at or below 0, a sell's at or above 0) or `no_close`. `v_ui_calls` and `v_ui_outlook`
expose the three columns. Targets stay calendar days; session targets are a separate parity gap.

The new columns go last in each table, since rows are written by position. `setup` adds the ones a
table lacks: on the guarded `signals.signal` as a guard act (`ADD COLUMNS`, planned with the
operation alone, since Delta records the columns as JSON), and on `signals.signal_backtest` as a plain
ALTER. The backtest job adds any still missing, but it runs as the runner, which does not own the
table and is expected to be refused an ALTER, so `setup` runs first.

Not done here: the reference's walk tests only sessions on or after its cut; this one walks from
`START`, so its gate covers more years. The label counts the primary's price rows as sessions, so a
missing close would join two sessions' returns.

## Stock and history (wmt-switch, 2026-11-18)

The traded stock is WMT, measured against SPY and XLP, from Yahoo's split-adjusted `quote.close`
from 1991 and Alpha Vantage's daily bars after. `config.PRIMARY` and `config.BENCHMARKS` name them
once, and the primary's features are `stock_ret_1d` and `stock_ret_5d` (`config.key`), so a change
of stock touches `config` and the static labels (README, `index.html`, the dashboard, the Genie
space) alone. Benchmark keys keep the ticker: `spy_ret_5d`, `xlp_ret_5d`. Signal and backtest rows
carry a `ticker` column, set from `config.PRIMARY` by `score.row`; the views read `close`, `ret` and
`ticker` from the rows, and the App and snapshot take their price labels from that `ticker`. The
registered model is `stock_signal`. A benchmark's five-day return is null and does not hold a call
while its five-back session precedes its first session (`<b>_unlisted = 1`; SPY 1993-01-29, XLP
1998-12-22); after that a missing return holds `stale_prices` as before. Alpha Vantage slot 22 is
SPY and 23 XLP, every other slot WMT. Normals are the 1961-1990 ERA5 mean per day of year with
every year present. The metro study searches 1998-12-22 to 2012-12-31 and holds out 2013-01-01 to
2026-11-17, with normals from before 1991-01-01; the windows were fixed before any WMT data was
read. The App measures the newest backtest run from its own rows (`app/ws_ui/measure.py`): each
buy or sell against the next session's close, always buying, and the same buys and sells on random
days (exact mean and variance, normal band), for the whole run and for forecast and observed inputs
apart. A reused run id cannot carry an earlier stock's figures.

Not done here: the agent's prompt and tools. Whether WMT has an edge is measured in the backtest
(`presentation/claims.md`, rows 6a to 6h).

## Intraday (display only, 2026-11-20)

The hourly ingest job has a second task, `intraday`, beside `ingest` and waiting on neither. Inside
an XNYS session from the seed calendar, up to an hour past its close, it fetches Yahoo's chart for
`config.PRIMARY` at `interval=60m&range=5d` and lands the response as a `yahoo-intraday` envelope
(`src/weather_signal/ingest/intraday.py`); outside that window it makes no call. A failed fetch logs
and exits 0, so it never fails `ingest`. The pipeline's `daily.prices_intraday` keeps the points on
the session's hour grid (bar starts at the open plus whole hours), drops the trailing quote stamped
at the fetch, marks a bar `final` once it has ended (at the next hour or the close), lets each
envelope replace the rows from its oldest bar on, and keeps the last five sessions. `app.v_ui_intraday`
reads it, and the App's and the dashboard's Intraday pages draw the hourly closes with the horizon-0
calls made in each session and each call's `est_price` from `v_ui_calls`.

Nothing that makes a call reads these bars: `prices_raw` keeps only the `alphavantage` and `yahoo`
sources, and `tests/unit/test_intraday_display_only.py` pins that no signal, guard, agent,
backtest, train or score module names them. `v_ui_intraday` is outside `views.VIEWS`, so Genie and
the MCP tools do not offer it. A bar fetched at :05 reaches the table with the score job's pipeline
refresh at :30 and the App with the next snapshot at :15, so the chart runs up to about 70 minutes
behind (expected from the schedules, not measured).

## Measured on the `dev` target

- The ingest job on serverless jobs compute reached Open-Meteo and Alpha Vantage, read the key from
  the secret scope and landed both envelopes. This settles the open question of egress from jobs compute.

## Agent hosting

The agent runs on the owner's Mac under launchd so that it uses the owner's Claude subscription,
whose login lives in the Claude CLI on that Mac. That trades away availability: the hour runs only
while the Mac is on and the owner logged in, and launchd folds the hours missed in sleep into one
run on wake (its documented behaviour, not measured here). There is one host,
so a lost disk or a moved folder stops the agent until `agent install` runs again. Each model call is
logged in the workspace by the AI Gateway (`system.ai_gateway.usage`), but the run itself is not:
its logs, run records and launchd state live on the Mac, outside workspace audit and permissions.

The hosted alternative runs the same worker as a Databricks job on the hourly schedule, or behind a
model-serving endpoint, as the agent's own service principal with an OAuth secret, and calls Claude
through the gateway on an API key or a provider service instead of the subscription. That costs per
token and needs the principal's OAuth secret, which bootstrap's `agent` phase creates; in return the hour runs with the
workspace's scheduler, retries and audit, and nothing depends on the Mac.

## Not covered

- The tamper tests run as the owner, not the runner principal, which has no OAuth secret. The agent
  principal's secret is created by bootstrap's `agent` phase, so `test_agent_contract.py` runs where
  that profile exists; the agent's refusal with `check` unable to read is not exercised.
- `weather-signal lead-boundary` and `weather-signal av-lag` (stage 3 measurements) are not built.
- The `check` task's metrics cover the calendar's end and price disagreements; the Yahoo, forecast
  timing, `stale_prices`, Alpha Vantage outage and lineage checks are not built.
- Nothing here measures `P`, ERA5 first-seen lags or the Alpha Vantage serving lag: stage 3.

- A filled first sighting is permanent. `forecast_hourly` keeps each key's first sighting, so when
  the first envelope for an hour is filled and quarantined, a later clean envelope never replaces
  it; only a full refresh of the streaming table recovers the cell.
- The app does not show `signal.reasons`, so `forecast_quarantined` (like every model reason) is
  visible in `signals.signal` and `v_ui_outlook` but not on screen; that is the UI's change to make.
- `ops.fill_alerts` is written before the task fails; if the failure mail is lost, the envelope is
  not reported again. A heavy metro (ranks 1 to 9, over 0.03) short for any reason other than the
  quarantine removes its cell with no row, and the signal holds as `stale_forecast`.

Follow-ups recorded from break rounds 2 and 3, not fixed here:

- The validators check `time`'s first and last value and its length, not each value, so swapped or
  duplicated dates pass, as do string or huge values in a column.