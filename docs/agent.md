# The hourly agent

A deterministic model makes every call. Claude reads the same governed inputs through three narrow
tools, explains each call, and may soften one to hold when an input is stale, implausible or
inconsistent. It has no way to name an action. The agent's code binds no action either, because
the database writes the signal's own action from the signal table. The agent runs hourly on the
owner's Mac under launchd, isolated from his Claude Code setup, and a run that overruns dumps where it
was stuck and ends with every process it started, whatever threads are still alive. It reaches Claude
through the Databricks AI Gateway on a provider service of its own, so each call is logged in the
workspace and matched to its hour, and goes to the subscription directly only when a gateway check
fails before anything is sent. Databricks documents the subscription provider service for a
developer running `ug claude`; this unattended use works, as measured, and is not documented.
Either way crewkit has first checked which account the CLI will bill: the owner's subscription here,
or an API key anywhere else.

Until the agent principal has an OAuth secret, the agent reads and writes as the profile `install`
is given (`weather-signal`, the owner's), not as its own principal; the setup is below and is the
owner's to run.

## Install

The agent's dependencies are the `agent` extra, so the jobs' wheel environment never carries them.
The Mac's venv takes Databricks Connect, not the `spark` group:

    uv sync --no-default-groups --group connect --extra agent
    uv run python -m weather_signal.agent install --profile weather-signal         # prints only
    uv run python -m weather_signal.agent install --profile weather-signal --yes   # writes and bootstraps

`install` writes `~/Library/LaunchAgents/dev.weather-signal.agent.plist`: minute 40, six environment
names (`CREW_LLM_BACKEND=plan`, `CREW_LLM_ROUTE=databricks-gateway`,
`CREW_DATABRICKS_PROFILE=weather-signal`,
`CREW_DATABRICKS_SERVICE=workspace.default.weather_signal_agent`, `WEATHER_SIGNAL_PROFILE`, and a
`PATH` holding the `databricks` CLI), and no `AbandonProcessGroup`, so launchd ends the CLI child
with the worker. It refuses when no `databricks` CLI is on `PATH`, since the gateway's token fetch
runs it. Remove with `launchctl bootout gui/$(id -u)/dev.weather-signal.agent` and delete the plist.

A one-off hour that writes nothing:

    uv run python -m weather_signal.agent run --as-of 2026-11-18T14:30Z --dry-run

`run` with no `--as-of` refuses outside its launchd label (exit 2) and names the `launchctl kickstart`
line to use instead.

## The agent principal (owner step)

`weather-signal-agent` is created and granted by `setup.grants` (`USE SCHEMA`, `SELECT`, `EXECUTE` on
signals; `MODIFY` on `recommendations` and `agent_runs`). It has no OAuth secret, and creating one is an
access-key act the owner names. When he does:

1. In the account console, generate an OAuth secret for the principal; it is shown once.
2. Add a profile to `~/.databrickscfg` by hand, never through argv:
   `[weather-signal-agent]` with `host`, `client_id` (the application id) and `client_secret`.
3. `uv run pytest -q tests/integration/agent/test_agent_identity.py` with
   `WEATHER_SIGNAL_PROFILE=weather-signal-agent`; it checks `current_user()`, the three functions, the
   two `MERGE`s and three refused writes.
4. Rerun `install --profile weather-signal-agent --yes`.

## One hour

1. Launch check, then a `started` line in `~/Library/Caches/weather-signal/runs.jsonl` (fsynced),
   then the deadline: `faulthandler.dump_traceback_later(1260, exit=True)` into
   `deadline-<run_id>.txt`, never cancelled, with a watchdog process that SIGKILLs the worker 15 s
   later in case the dump itself hangs (measured once, mid-dump, on 3.12). Once the worker is gone,
   the watchdog SIGKILLs the worker's process group: launchd's cleanup at job exit sends only
   SIGTERM, and a child that ignores it outlived the job (measured on this Mac).
2. A start past HH:30 + 20 min records `late_start` and exits.
3. Connect on the named profile, reconciliation of earlier run ids the table lacks (a full line wins
   over a `started` line; a `started` line with a thread dump becomes `deadline_killed`, without one
   `crashed`), inputs polled to :55, then `v_guard_hold` read until this as_of is post-verified.
   An open incident (or an unreadable one) holds at once; an unverified hour is read again every
   60 s, and holds as `guard_hold` once reads must stop: :55, or 900 s after the worker started,
   whichever comes first. The hour never runs on an as_of the guard has not verified.
4. crewkit `connect` on the gateway route. Only `DatabricksGatewayUnavailable` (a pre-check failure,
   before any prompt) goes direct, as `direct_fallback`; a refusal, a connect timeout, a status after
   the prompt and a turn-bound expiry all keep the hour's route and hold what is missing.
5. One turn bounded by `min(cap, 1200 - E - pre - 75)`, `None` under 30 s of the hour left.
6. One `MERGE` per date sourced from `signals.signal`; Python binds `downgrade`, the reason, the
   rationale and metadata, never an action.
7. The run row to `runs.jsonl`, then `agent_runs`; the trace flushed in a bounded thread; then
   `os._exit`.

## Departures from the plan, and what this does not fix

- `setup` gives `recommendations` an `llm_route` column with its CHECK, and `agent_runs` a column
  for every run field with its `outcome` and `route` CHECKs (foundation changes 10 and 12). Neither
  adds a column to a table an earlier `setup` made: there the agent binds `llm_route` only when the
  column exists, unknown run fields go into `detail` as JSON, and adding `llm_route_valid` to such a
  `recommendations` fails until the column is added by hand. `trace_id` is MLflow's trace id (`tr-`
  and 32 hex, which `mlflow.get_trace` resolves), never a span id.
- The `MERGE` writes `s.confidence`; the agent binds no confidence (foundation change 11).
- The evaluation needs `dev_signals.recommendations_eval`, which the foundation doesn't create yet;
  `python -m weather_signal.agent.evaluate` refuses without it.
- `guard_hold` is an outcome the plan's list lacks: the foundation contract requires the guard read.
- The LaunchAgent stays at minute 40 and the hour waits for the guard instead of reading it once.
  The score job's `check` task, which writes `post_verified_as_of`, ended 12.3 and 16.1 min after a
  :30 start (dev, 2026-11-19 07:30 and 08:30), and 16.4 min into a one-off run; the :40 runs on
  both hours read the guard before it, and held. Reads stop at :55, which covers a score job of up
  to 25 min, and at 900 s elapsed the turn still has `1200 - 900 - 60 - 75 = 165` s. A later
  minute would only shorten that margin. A score job past :55 still holds the hour.
- Databricks Connect collects a `TIMESTAMP` as a naive datetime in the process's local zone and
  binds a naive one from it; `spark.sql.session.timeZone` doesn't reach either conversion (measured
  2026-11-19: session zone `Etc/UTC`, 06:30Z came back as 23:30 on an MST host, so every hour held).
  Every Mac session is opened through `weather_signal.spark.connect_session`, which sets the process zone
  to UTC first, and the store reads a collected instant back as UTC (`_instant`) for the guard
  check and the inputs it hands the model. This does not fix code that opens a Connect session
  another way.
- MLflow's usage telemetry is switched off in the worker (`MLFLOW_DISABLE_TELEMETRY`); it posted to
  MLflow's host from the first run.
- The env window is process-wide: any thread alive during `connect` sees the reduced `os.environ`.
  The worker logs the thread names when the window opens; it does not prevent the exposure.
- A `CREW_LLM_ROUTE` left unset makes the first route `direct`, recorded as `direct`; only a
  gateway pre-check failure is `direct_fallback`, and `agent_runs` refuses one with no reason.
