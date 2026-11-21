# Operations

The commands, the bootstrap, the teardown proof and the UI's moving parts, for whoever runs this
project. The [README](../README.md) has the claim, the architecture and the one-command start; this
page has everything after it. Every cron is UTC.

## Tests and CI

CI (`.github/workflows/ci.yml`, on push and pull requests to `main`) runs `ruff check src app tests`,
`mypy src` and the offline suite (`tests/unit tests/contract tests/spark tests/app tests/process`)
with no secret and no workspace. It installs without the `agent` extra, since the agent's layers need
the `agent` extra (`crewkit` is public), so they skip there; `tests/ui` needs the Mac's Chrome and runs locally.
`tests/` holds `unit`, `contract` (recorded payloads), `spark` (local Spark 4, which needs a JDK; CI
uses Temurin 17), `app`, `process`, `ui` and `integration` (the `dev` target over Databricks Connect
serverless).

## Bundle targets

`dev` prefixes every schema and job with `dev_` and runs the pipeline in development
(`presets.pipelines_development`), so a failed update stops after one attempt; `prod` keeps
production mode and its retries. `dev` is not in `mode: development`: that mode with a name prefix
fails validation unless the prefix holds the user name. Every schedule takes `var.schedule_pause`:
`UNPAUSED` on `dev`, so a hand deploy keeps it live, and `PAUSED` on `scratch` and `prod`; bootstrap
passes the variable itself. Every cron runs in UTC: ingest at :05, `ui_snapshot` at :15 and `score`
at :30 each hour, and `train` at 23:45 on 2 to 9 January. The champion's period ends 31 December,
so every call for a target after it holds `no_champion` until `train` registers the new year's refit,
which exists once the year's first session has its close and its 21:30 decision; nightly runs over
the first week catch it whichever day that session falls on. A run after it registers a new
version when the gate's figures have moved.

## Commands

    uv sync --locked                             # CI's install; add --extra agent (needs `crewkit`, public on GitHub) for the agent's layers
    uv run ruff check . && uv run ruff format --check . && uv run mypy
    uv run pytest -q -m "not integration and not live" tests/unit tests/contract tests/spark tests/app tests/process
    uv run pytest -q tests/ui                    # needs the Mac's Chrome; CI skips it
    databricks bundle validate -t dev --profile weather-signal --var warehouse_id=<id>
    uv run weather-signal bootstrap --profile weather-signal --target dev --only preflight
    UV_PROJECT_ENVIRONMENT=.venv-connect uv sync --no-default-groups --group dev --group connect --extra agent
    UV_PROJECT_ENVIRONMENT=.venv-connect uv run --no-sync pytest -q tests/integration
    uv run weather-signal --schema-prefix dev_ setup --runner <app-id> --agent <app-id>
    uv run weather-signal guard status

The raw history (ERA5 from 1961, Previous Runs from 2021-03-24, the Yahoo WMT, SPY and XLP
histories) is fetched outside this repository by a separate fetch tool in `<data folder>/fetch`,
which writes `<data folder>/raw` and its `manifest.json` with no Databricks; see its
README. Bootstrap seeds the raw volume from that folder. The ingest job here keeps fetching the
daily prices and weather.

On a fresh target, run the pipeline once before `setup`: the SQL functions `setup` creates read the
pipeline's tables.

Going live on `prod` (`bootstrap --target prod --live`) is the owner's act.

## Provisioning in detail

The inputs:

- `--profile`: the `~/.databrickscfg` profile `auth login` wrote. Bootstrap acts as that user, who
  must be a workspace admin to create the two service principals and the agent's secret.
- `--target`: `dev` (prefix `dev_`), `scratch` with `--schema-prefix tN_` (a throwaway copy that
  `--destroy --yes` removes), or `prod` (no prefix; it validates and has never been deployed).
- `--seed-from`: the data folder; bootstrap seeds the raw volume from it and fetches only the
  catch-up after it. A raw file already landed at an archive file's path, at another size, is
  kept with a `seed: kept` line when both are whole envelopes of the request the file's name
  encodes (one request fetched at two moments); anything else at that path stops the seed.
- The Alpha Vantage key file: a free key from https://www.alphavantage.co/support/#api-key, read
  from the clipboard so it never reaches shell history (or saved into the file with an editor).
  It goes to the workspace's secret scope over stdin.
- Found, not passed: the SQL warehouse (the only one; `--warehouse-name` picks one of several) and
  the catalog (the metastore's default; `--catalog` overrides it).

The command builds `.venv-connect` on its first run: `setup` and `refresh` need Databricks Connect,
which cannot share a venv with local PySpark, and the agent's LaunchAgent runs that venv's Python.
Run it from the main checkout: on `dev`, and on `prod` with `--live`, preflight stops with exit 2
in a linked git worktree, since the LaunchAgent would keep that worktree's Python
after the worktree is removed. Each phase prints one line.

The wheel uploads below `artifacts/<wheel_tag>/`, a digest of `pyproject.toml`, `src/`, `app/`
and `aibi/` that bootstrap passes as `--var wheel_tag=`, so a code-only change is a changed
library path in the bundle plan and the `bundle` phase redeploys it; a plain
`databricks bundle deploy` uses the tag `manual`. The `app` phase redeploys the App when `app/`
differs from the digest in `~/.local/state/weather-signal/app-<target>-<prefix>.digest`, which
it writes after each good `bundle run ui_app`; a machine without that file redeploys once.

Schedules. When the bundle plan shows any change, the `bundle` phase deploys every schedule
`PAUSED`, so no job runs against a half-built set, and the last phase, `live`, redeploys with
`schedule_pause=UNPAUSED` on `dev` and on `prod` with `--live`. Before such a paused deploy of a
live set, bootstrap writes `~/.local/state/weather-signal/bootstrap-<target>-<prefix>.live`, and
`live` removes it. A run that stops between the two (a failed catch-up, Ctrl-C) leaves the file,
and the rerun treats the set as live and unpauses it at `live`: this holds for `prod` without
`--live` too, which otherwise keeps the state it has deployed, live or paused. `scratch` is never
unpaused.

The agent. On `dev`, and on `prod` with `--live`, the `agent` phase creates an OAuth secret for
`weather-signal-agent` when its profile has none, writes `[weather-signal-agent]` (host, client id,
secret) to `~/.databrickscfg` at mode 0600, and runs `python -m weather_signal.agent install --yes`
with the target's prefix. The secret goes from the create's output into a staged copy of the file
and never into argv or a printed line; a write that fails, or a Ctrl-C before the rename, deletes
the new secret, so a rerun leaves exactly one. A kill by `SIGKILL` in that window is not covered: the
secret stays on the principal, to delete under Settings > Identity and access > Service principals.
When the workspace refuses to make the secret, `agent` stops with exit 2 and the one step to take
by hand. The LaunchAgent fires at minute :40 of every hour on the Mac's clock, ten minutes after
`score`; a new secret that is not yet signing in stops the phase as a probe that did not answer,
and a rerun makes no second secret.

Times. Every table stores UTC; `known_at`, `as_of` and the cron above are UTC. Databricks Connect
collects timestamps as naive local time, which the code converts back, so a Mac in MST reads the
same instants. The agent calls an input stale when the forecast it read is more than 30 hours old:
the forecast steps once a day, so a healthy decision day stays under 29.5 hours.

Reruns. Rerunning the command on a finished workspace writes nothing but the catch-up run and
`setup`'s grants. A transient Open-Meteo or Yahoo throttle fails the catch-up phase (exit 1); rerun.
The catch-up run can also collide with the scheduled ingest at :05 on a live set
(`max_concurrent_runs: 1`); rerun after a few minutes. A Genie space created inside the list's lag
is found again by the id its create returned (kept for 15 minutes in the same state folder), so a
rerun makes no second space.

On `prod`, `train` runs as `var.owner`, which defaults to whoever deploys: a deploy by a service
principal puts an application id there and fails, and the job stops working if that user is
deactivated.

## Bootstrap

`weather-signal bootstrap --profile <p> --target dev --seed-from <data folder>` builds
the whole project on a workspace from this checkout and the data folder: principals (by name,
created when missing), the bundle, the Alpha Vantage secret (read from
`~/.config/alphavantage/API_KEY`, sent over stdin), the raw volume from the folder's `raw/`, one
catch-up run of the ingest job from each source's newest landed slot to now, the pipeline, `setup`
and its grants, the App, the train job's model (the walk's latest yearly refit, `@champion`
only when its gate passes), the backtest and the snapshot. A partial folder seeds what it holds; preflight prints
each source's files and date spans, and a rerun seeds what has landed since. Each phase asks the
workspace whether it is done first and prints one line; rerunning it on a finished workspace writes
nothing but the catch-up run and `setup`'s grants. Without `--seed-from` it refuses: the history
comes from the folder, never from bootstrap. `--target scratch --schema-prefix tN_`
builds a throwaway copy, and `--destroy --yes` removes one. Run it as the provisioning section
shows: `setup` and `refresh` open a Spark session, and preflight stops with exit 2 and that one
command when `databricks.connect` is missing. `--write-baseline <file> --cutoff
<date>` and `--compare <file>` record and check row counts and digests. Exit codes: 0 done, 1 a
phase failed or a probe did not answer, 2 an owner step is missing or the scope is refused, 75 a
lock is held or Apps hit the free daily limit. The last phase, `gitfolder`, creates or updates
a workspace Git folder of `config.GIT_FOLDER_URL` at `/Workspace/Users/<you>/<repo name>` and
checks it out to `config.GIT_FOLDER_BRANCH` (`main`) at the remote's head (`git ls-remote`); no
GitHub Git credential for the deploying user, a credential whose GitHub link has no access to the
repository (`repos create` or `repos update` saying so), or a remote that does not answer, prints
`gitfolder: skipped: <reason>` and bootstrap goes on, while anything else at that path, or a Git folder of
another URL, stops the phase and is left as it is. The folder is user content, not a bundle
resource: `--destroy` and the proof leave it alone. 

## Teardown and reprovision (dev)

`weather_signal.deploy.proof` tears `dev` down and builds it again from empty, then writes a
pass/fail report. It runs from the main checkout under `.venv-connect`, and refuses otherwise:

    UV_PROJECT_ENVIRONMENT=.venv-connect uv run --no-default-groups --group connect --extra agent \
      python -m weather_signal.deploy.proof plan --profile weather-signal \
      --seed-from <data folder> --out <report folder>/proof

then the same command with `run --yes` in place of `plan`, and after the next :40 with
`verify-agent`.

- `plan` only reads: the dev bundle's deployed state (`.bundle/weather-signal/dev/state/resources.json`,
  exactly what `bundle destroy` deletes), the `dev_` objects present, the files and tables to
  archive and the bootstrap command. It changes nothing in the workspace or in
  `~/.local/state/weather-signal`, writes `plan.md`, and exits 2 when anything would be refused.
- `run --yes` takes bootstrap's dev lock (`~/.local/state/weather-signal/bootstrap-dev-dev_.lock`)
  for the whole run and hands it to the bootstrap it starts; a held lock exits 75 before anything.
  A `--out` that holds an earlier run (`archive/`, `before.txt`, `bootstrap.json`) is refused unless
  `--resume` is given; a fresh run needs a new folder and a deployed dev. Then (1) pause: every
  `UNPAUSED` dev schedule is paused, the run waits up to 30 minutes for every dev job's active runs
  and any agent run to end, and the agent's LaunchAgent is unloaded with `launchctl bootout` (its
  plist is kept; bootstrap's agent phase loads it again); then it waits up to 2 minutes until
  `pgrep` finds no process running the installed agent's command line, so a run that started
  after the drain has ended before anything is exported. Run it from a Terminal in the Mac's
  logged-in session: one that cannot see `gui/<uid>` (ssh) is refused before anything is paused.
  (2) archive: every raw-volume file the data folder lacks or holds with other bytes (compared by SHA-256), every ledger-volume file, and the
  `signals` tables `signal`, `recommendations`, `agent_runs`, `recommendations_eval` and
  `signal_backtest` as JSON lines, all under `$OUT/archive/`, never overwriting a file. A volume the
  dev state deploys that does not exist stops it. (3) list again: a file that landed or changed, or
  a table whose count moved, since the archive stops it; the counts are taken once more as the last
  read before `bundle destroy`. (4) destroy:
  refused unless every deployed resource is a `dev_` object (schemas, jobs, pipeline and dashboard
  `dev_`, the App `dev-`, volumes and the model in a `dev_` schema, the scope `weather-signal-dev`,
  catalog `workspace`); then `bundle destroy -t dev --var dev_holds=false`, the Genie space `dev_weather-signal` trashed,
  and a wait of up to 15 minutes until no `dev_` schema, job, pipeline, App or space is left. The
  principals, their secrets and `[weather-signal-agent]` stay. dev's raw schema and its `raw` and
  `ledger` volumes carry `prevent_destroy: ${var.dev_holds}` (default `true`), so a plain `bundle
  destroy -t dev` or a deploy that would delete them is refused; only this step passes `false`,
  after the archive and the recount. prod's holds are literal `true` and take no variable. A
  destroy that fails while every resource is still deployed and the recount still matches the
  archive deleted nothing: the run clears its pointer, records `destroy_failed_at` in `run.json`
  (so `--resume` refuses that folder) and prints the bootstrap with `--ledger-from`, which
  unpauses dev and loads the agent; run the proof again with a new `--out` after. (5) `weather-signal bootstrap
  --profile weather-signal --target dev --seed-from <data> --ledger-from $OUT/archive/ledger`: the
  seed phase restores the archived ledger before catch-up, so a slot claimed today is not claimed
  again on the shared Alpha Vantage key; its lines go to `$OUT/bootstrap.log`. (6) verify, below.
  Any stop after the pause, Ctrl-C, SIGTERM, SIGHUP and a probe that does not answer included,
  prints the one command that finishes. Before the archive is complete it is
  `<main>/.venv-connect/bin/weather-signal bootstrap --profile weather-signal --target dev`, whose
  live phase unpauses every schedule and whose agent phase loads the agent. Once the archive is
  complete it is that bootstrap with `--ledger-from $OUT/archive/ledger` (restore is put-if-absent,
  so a rerun claims nothing twice). While the destroy is under way it is `proof run --yes --resume
  ... --out <the same folder>`. Once the bootstrap has started it is `proof finish --profile
  weather-signal --seed-from <data> --out <the same folder>`, which takes the lock, runs that same
  bootstrap again, keeps the earlier attempt as `bootstrap-<n>.json` and `.log`, writes this
  attempt's as `bootstrap.json` and `.log`, then verifies; `verify` judges the phases and the score
  run window by `bootstrap.json`, the last attempt. A bootstrap run by hand writes no record, so
  verify still reads the attempt before it. `finish` refuses a folder whose bootstrap never began
  and the same folders `--resume` refuses. The archive step writes
  `$OUT/run.json` (profile, prefix, data folder, every ledger file archived with its size); before
  `bundle destroy` it records the resource keys the destroy is for, and points at the folder from
  `~/.local/state/weather-signal/proof-dev-dev_.json` until a bootstrap exits 0. While that pointer
  names another folder, a fresh run is refused and given that run's command. `--resume` refuses a
  folder without `run.json`, one for another profile, prefix or data folder, one whose
  `archive/ledger` lacks a listed file, and one whose `verify.json` passed. With bundle state
  present it runs `bundle destroy` again only when this run's destroy began, its bootstrap did not,
  and every resource still deployed is one that destroy was for; otherwise it stops and prints the
  bootstrap line. With no state it finishes the Genie trash and residue wait, or skips them when
  `run.json` records the destroy finished and no `dev_` residue is left, then bootstraps with the
  folder's ledger. A state file with no resources counts as not deployed. `--ledger-from` is
  refused on any target but dev.
  The exported tables are a record, not restored: bootstrap rebuilds `signal_backtest`, and the
  score job and the agent write the live tables again from the next hour.
- `verify` (run again on its own after a stop): every phase printed `done` or `ran` and the exit was
  0; every pipeline and `signals` table answers a count, each with rows except the quarantine tables
  and the agent's `recommendations` and `agent_runs`, which `verify-agent` judges; `signal` is
  counted after the wait for the first scheduled score run and must hold rows; every job exists once and the scheduled
  ones are `UNPAUSED`; the App is RUNNING, ACTIVE and SUCCEEDED, and its log (`databricks apps logs
  --tail-lines 200`) shows the active deployment's `Application startup complete` with no shutdown
  after it. An HTTP probe can't stand in: the Apps front door answers `/healthz` itself. The agent
  profile signs in as `weather-signal-agent`; the LaunchAgent's Python is in this checkout's
  `.venv-connect`, with prefix `dev_` and profile `weather-signal-agent`, and is loaded; the first scheduled `score` run after bootstrap
  ended SUCCESS (it waits up to `--wait-min`, 75 by default). Report: `verify.md` and `verify.json`.
- `verify-agent`, after the next :40: the first `agent_runs` row since bootstrap ended whose `as_of`
  hour has `signal` rows has outcome `ok`, and earlier runs (a correct `signal_missing` before the
  first score) are listed as info; with no such run yet it fails. Every `signal` row of its `as_of` has a recommendation; each recommendation's
  `signal_action` is the model's action, and any other action is a `hold` with a `downgrade_reason`.
  Report: `verify-agent.md`.

Exit codes: 0 pass, 1 a check failed or a probe did not answer, 2 refused before any write.

`$OUT/archive/` is a copy of workspace data and stays on disk, out of git; each run's `archive.txt`, `run.json` and
`verify*.md` stay tracked. Keep the folder until the run's bootstrap has exited 0, since
`--ledger-from` and `--resume` read it.

## Exporting a deployment's landings

`weather-signal archive export --profile weather-signal --out <new folder>` copies dev's raw and
ledger volumes to a local folder in the data folder's layout, so what a workspace landed can seed a
later rebuild:

    <out>/raw/<source>/yyyy/mm/dd/hh/<key>.json.gz
    <out>/ledger/<file>.jsonl
    <out>/manifest.json   # files[]: path, bytes, sha256 (and source for raw/)

It uses the proof's archive read: every listed file is read back at its listed size or the export
stops, a missing raw or ledger volume stops it, and no file is overwritten. The manifest is written
last, so a stopped export leaves no manifest. It only reads the workspace. `bootstrap --seed-from
<out>` verifies and seeds from that folder, and `--ledger-from <out>/ledger` restores its ledger.

- The scope is `dev_` unless `--target prod` is given (`--target prod` reads `workspace.raw`).
  Scratch, any other target and any prefix that is not the target's own, including a top-level
  `--schema-prefix`, exit 2 before anything is read.
- `--out` must be a new or empty folder, and its `raw/` may not be
  `<data folder>/raw`, lie inside it or contain it, after symlinks are resolved: the
  local fetcher is the only writer there (REQ-018). A different folder inside the data folder, such
  as `<data folder>/exports/dev`, is allowed. Both refusals exit 2 with nothing read.
- It copies every file, including those the data folder already holds; the export is a copy of
  workspace data and stays out of git.

Exit codes: 0 exported, 1 a read failed or a volume is absent, 2 refused before any read.

## The UI, the App and the MCP server

`setup` also creates the `app.v_ui_*` views and `app.fn_ui_*` functions every UI surface reads
(`src/weather_signal/serving/views.py`). `resources/catalog.yml` holds the `ui` volume and `resources/weather_signal.dashboard.yml` the AI/BI
dashboard `ui_dashboard` (`aibi/weather_signal.lvdash.json`), whose datasets read only `v_ui_*`, unqualified, with catalog and schema from the
target.

The `ui_snapshot` job writes `/Volumes/<catalog>/<prefix>signals/ui/snapshot.json` at :15 each hour
(paused until bootstrap's `live` phase); the App in `app/` serves it. The App is declared in the main bundle
(`resources/weather_signal_ui.app.yml`) for `dev`, `scratch` and `prod`; `run_as` is set per job and
pipeline, not on the target, because an App refuses a target-level `run_as`:

```sh
databricks bundle deploy -t dev --var warehouse_id=<id>
databricks bundle run -t dev --var warehouse_id=<id> ui_app
```

Grants on the main bundle's schemas and volumes replace whatever else is there, so the App
principal's `USE_SCHEMA` on `signals` and `app` and `READ_VOLUME` on `ui` sit in the complex variables
`signals_grants`, `app_grants` and `ui_volume_grants`, which `resources/weather_signal_ui.app.yml` sets per target by reference to
`${resources.apps.ui_app.service_principal_client_id}`; no principal id is pinned. Their default
in `databricks.yml` lists the runner alone. The App's `SELECT` on the
`v_ui_*` views is granted by `setup`, since the views do not exist at a fresh land's first deploy.
The Genie space is not a bundle resource: Genie refuses a space over views that do not exist,
and the first deploy comes before them. Bootstrap's `genie` phase creates it from
`aibi/weather_signal.genie.json` after `setup`, titled `<prefix>weather-signal`, and `--destroy` trashes it.

The Intraday page (`#/intraday`, `/api/intraday`) and the dashboard's Intraday page read
`app.v_ui_intraday` over `daily.prices_intraday`, filled by the ingest job's `intraday` task during
XNYS sessions (`docs/design.md`, Intraday). The bars are display only and never feed the model. The
table may be empty outside session hours, so the proof counts it without requiring rows; teardown
removes it with the `daily` schema and its envelopes with the `raw` volume. The snapshot job reads
the view apart from the rest, so a missing view leaves an empty chart, not a failed snapshot.

`weather-signal --schema-prefix dev_ ui-export --out <dir>` downloads that snapshot within 60 s,
refuses it unless its sha256 checks, and writes `index.html`, `static/` and `snapshot.js`; the
folder opens from `file://` with no workspace (`--from <file>` exports a local copy). It reads the
pages from this checkout's `app/static`. It writes only into an empty folder or one it made (it
leaves `.weather-signal-export` there, listing its files), refuses any folder overlapping `app/`,
a marker listing a path outside the folder, a symlink anywhere it would write and a file with a
second hard link; it writes each file to a temporary name and renames it into place, and removes
nothing. `--force` writes into a non-empty folder beside what is there, or over a marker that lists
files a newer checkout no longer has, which are left in place. It refuses a snapshot
built more than 75 min ago (the App's `MAX_AGE`) unless `--stale-ok`, and one whose `built_at`
has no offset or is more than 5 min ahead. A failure prints
`not exported: <why>` and exits 1.

`weather-signal-mcp --profile weather-signal --schema-prefix dev_ --warehouse-id <id>` (the id from
`databricks warehouses list`) is the Claude beat's local stdio MCP server: `outlook`,
`calls`, `runs`, `sources` and `status` over `fn_ui_*`, each call row with `integrity` from the
App's rule at call time. Every tool uses the credentials of `--profile`, which has no default:
the owner's `weather-signal`, or `weather-signal-reader` once the owner creates the reader.

`.claude/hooks/sql_gate.py` is the `PreToolUse` fallback for `execute_sql`: it allows only one plain
`SELECT` over `v_ui_*` and `fn_ui_*` in an app schema, with no other function, and never allows on
its own. The `|| exit 2` blocks the call when the gate cannot run at all. It is not wired in; to
wire it, add to `.claude/settings.json`:

```json
{"hooks": {"PreToolUse": [{"matcher": "mcp__databricks-sql__execute_sql",
  "hooks": [{"type": "command", "command": "uv run --quiet python .claude/hooks/sql_gate.py || { echo 'sql_gate did not run' >&2; exit 2; }"}]}]}}
```
