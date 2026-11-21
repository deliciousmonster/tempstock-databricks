# weather-signal

An hourly buy, sell or hold call on the WMT ticker from a population-weighted US temperature index,
benchmarked against SPY and XLP, on Databricks. The backtest finds no edge that survives from 2007 on.

[![CI](https://github.com/deliciousmonster/tempstock-databricks/actions/workflows/ci.yml/badge.svg)](https://github.com/deliciousmonster/tempstock-databricks/actions/workflows/ci.yml)

Personal demo, not investment advice. Weather data by Open-Meteo.com (CC BY 4.0).
Prices from Yahoo are for personal display only; Yahoo's terms forbid redistribution, so no
Yahoo response is committed here and the tests use synthetic bodies.

## Result

The calls did no better than buying every day. Over run `h0:threshold_wf:2`, the hit rate sits inside
the 95% band of random placement and 0.2 points under always-buy. The champion gate scores the walk
on net profit and loss at a 0.001 cost per position day, and it fails: the walk's net is below both
zero and the 95th percentile of year-block shuffles of its own positions. Measured on the dev
workspace on 2026-11-21, against ERA5 normals for 1961 to 1990. The figures, the file behind each
and what it does not show are in [presentation/claims.md](presentation/claims.md), rows 3 to 7 and 6i.

The demo is the machinery around a model that says so. Every value carries the moment it became
knowable, and one function decides what a decision may see. An agent can downgrade a call to hold but
can't flip it. A proof run on 2026-11-19 destroyed the dev set and rebuilt it with one bootstrap
command, from this repository plus a local data folder, reusing the principals; the proof has its own
command.

## Architecture

    Open-Meteo, Alpha Vantage, Yahoo -> raw -> daily -> signals -> app

The pipeline builds bronze through the index; jobs write `signal` and what follows it.

| Tier | Schema | Holds |
|---|---|---|
| Bronze | `raw` | raw API responses as received (`raw_envelopes`), plus the volumes, the ledger among them |
| Silver | `daily` | observed weather, forecasts, prices, sessions and metros |
| Gold | `signals`, `app` | normals, index, signal, backtest, recommendations and agent runs, plus the `v_ui_*` views and `fn_ui_*` functions the App, dashboard and Genie read |
| Platform | `ops` | the pipeline's event log and the fill alerts; not a tier |

- Sources: Open-Meteo for weather. Alpha Vantage, the official keyed API, is polled hourly for
  the live daily bars. Yahoo is a one-time full-history backfill per ticker at catch-up, archived as received in
  bronze.
- Intraday: hourly WMT bars from Yahoo's 60-minute chart land in `daily.prices_intraday` during
  XNYS sessions for the App's and dashboard's Intraday chart; they are display only and never feed the model.
- A Lakeflow Declarative Pipeline builds `raw_envelopes`, silver and the normals and index in gold,
  with expectations on sha, HTTP status, bounds and price order. Jobs write `signal`; a guard checks
  each job-owned table's id, state and history before and after every score.
- `signals.signal` is deterministic and versioned per row; it and `recommendations` are append-only.
  The trained model is a registered MLflow model; the train job sets `signals.stock_signal@champion`
  only when the champion gate passes, and score holds every call with `no_champion` otherwise.
- Claude reads three narrow tools and explains the call; it may downgrade it to hold, and the database
  writes the action. The agent runs hourly on a Mac under launchd, through the workspace AI Gateway,
  on a Claude subscription rather than per-token billing.
- Governance is Unity Catalog: a runner and an agent service principal, the agent holding `SELECT`,
  `EXECUTE` and `USE SCHEMA` and `MODIFY` only on `recommendations` and `agent_runs`.
- It runs on a Databricks Free Edition workspace: serverless jobs and pipeline. Deployment is a Databricks Asset Bundle; the front end is an AI/BI dashboard, a Genie
  space and a Databricks App.
- Schedules: ingest at :05, App snapshot at :15 and score at :30 (UTC cron); the agent at :40 on the
  Mac's clock.

Bootstrap's last phase keeps a workspace Git folder of the repository at
`/Workspace/Users/<you>/<repo>` on `main`, cloned with your own GitHub credential; the URL and
branch are `GIT_FOLDER_URL` and `GIT_FOLDER_BRANCH` in `src/weather_signal/config.py`. It is for
reading the code in the workspace: deploys stay with bootstrap, and no destroy removes it.

The design and what it doesn't cover: [docs/design.md](docs/design.md). The agent:
[docs/agent.md](docs/agent.md). One page on the parts: [presentation/architecture.md](presentation/architecture.md).

## Layout

| Path | Holds |
|---|---|
| `src/weather_signal/` | the wheel, by seam: `ingest/`, `pipeline/` (the pipeline's PySpark), `signal/` (index, features, model, score, backtest), `catalog/` and `guard/`, `serving/` (views, snapshot, export, MCP server), `jobs/` (one module per bundle job), `agent/`, `deploy/` (bootstrap and the proof), `research/`; the seed CSVs as package data. [docs/design.md](docs/design.md#module-map) has the map |
| `src/weather_signal_etl/` | the declarative pipeline, `transformations/pipeline.py`, wiring only |
| `databricks.yml`, `resources/` | the bundle: variables and targets, then one file per job, pipeline, dashboard and App, and `catalog.yml` for the schemas, volumes and model |
| `aibi/` | the AI/BI dashboard and the Genie space definitions |
| `app/` | the Databricks App that serves the hourly snapshot |
| `scripts/` | `build_metros.py` and `build_sessions.py`, which build the seed CSVs |
| `tests/` | `unit`, `contract` (recorded payloads), `spark` (local Spark 4), `app`, `process`, `ui` (Chrome on the Mac), `integration` (the `dev` target), `fixtures` |
| `docs/` | design, agent, operations, known issues |
| `presentation/` | the deck outline, demo script and the sourced claims |
| `.github/`, `LICENSE` | the CI workflow; MIT |

## Quickstart

Offline, with no workspace and no secret. It needs `uv` and Java 17 (the local Spark tests):

    uv sync
    uv run pytest -q -m "not integration and not live" tests/unit tests/contract tests/spark tests/app tests/process

CI runs this after ruff and mypy. The agent's layer needs the `agent` extra, which CI does not install, so its tests skip.

## Provisioning from a fresh clone

Provisioning needs the agent layer, so it needs `crewkit` (public, tags v0.16.2 to v0.17.1) and runs only for its author today.
Prerequisites:

- macOS: the `agent` phase installs a LaunchAgent and stops on any other system.
- The `databricks` CLI 1.26.0 or later (`databricks version`; preflight refuses an older one).
- `uv`; it fetches Python 3.12, which `requires-python` pins, when the machine lacks it.
- A profile signed in as a workspace admin on a workspace with serverless, one SQL warehouse (or
  `--warehouse-name`) and the AI Gateway: `databricks auth login` below writes it.
- The Claude CLI signed in on the Mac; the agent runs on that login.
- An Alpha Vantage key (free, at alphavantage.co) on the clipboard.
- A data folder, `<data folder>`, holding `manifest.json` and `raw/`. It is an archive of raw API
  envelopes that a fetcher kept outside this repository fills. Bootstrap refuses without `--seed-from`.

Then:

    databricks auth login --host https://<workspace-host> --profile weather-signal
    mkdir -p ~/.config/alphavantage && (umask 077 && pbpaste > ~/.config/alphavantage/API_KEY)
    UV_PROJECT_ENVIRONMENT=.venv-connect uv run --no-default-groups --group connect --extra agent \
      weather-signal bootstrap --profile weather-signal --target dev --seed-from <data folder>

One command builds the principals, the bundle, the secret, the raw volume, the catch-up, the pipeline,
the model, the App, the Genie space and the LaunchAgent; each phase prints a line, and a rerun writes
only the catch-up run and the grants. Run it from the main checkout, not a linked worktree. The inputs,
the schedules, the teardown proof and the UI are in [docs/operations.md](docs/operations.md); what is
known to be rough is in [docs/known-issues.md](docs/known-issues.md).
