# Known issues

Each item below is known, does not block a deploy or the proof, and is recorded in the break
reviews of 2026-11-19 or the open items of the private build record.

## Deploy, teardown and the proof

- If a partial destroy leaves the bundle's `resources.json` stale, the proof can close its run with
  the pointer cleared although something was deleted. The archive in `$OUT` and the printed
  `bootstrap --ledger-from` command restore it; rerun with a new `--out`.
- Two faults together: a partial destroy and a missing pointer file. A fresh run then prints a bare
  `bootstrap` without `--ledger-from`; add `--ledger-from <first out>/archive/ledger` by hand.
- Two faults together: a `tables get` error read as not-found, then a destroy. That table's export
  is skipped and the recount never checks it. Expected from the code, not seen live.
- `BUNDLE_VAR_dev_holds=false` in the environment lifts dev's `prevent_destroy` holds for any bundle
  command, not only the proof's destroy. Never export it in a shell.
- `verify-agent` matches recommendations by hour and `written_at`, not by run id (the table has
  none), so a later run for the same hour can satisfy an earlier run's check.
- On dev the champion gate fails, so no `@champion` alias exists and every target holds
  `no_champion` until a train run passes the gate (`presentation/claims.md` rows 6i and 50c).
  `recommendations_eval` does not exist either, so `python -m weather_signal.agent.evaluate` refuses.

## UI, data and Genie

- Databricks can stop the App's compute on its own ("stopped due to workspace or account status",
  seen 2026-11-19 by 8:57 pm MST), and the restart comes up with no deployment. The jobs and the
  agent keep running. Recover with `databricks apps start dev-weather-signal-ui`, then
  `databricks apps deploy` from the bundle's `files/app` path if it reports nothing deployed.
- The newest outlook reads "awaiting guard check" by design until the next hour's guard check; the
  App's snapshot runs at :15, so it shows the previous hour's calls.
- A fill of 3 or 4 metros in a file fetched more than 7 days ago reads as weather, by design; at 5
  or more metros per lead it stays red. Check `daily.forecast_fill_summary` for older files.

## Repository

- CI installs without the `agent` extra, because the agent's layers need the Claude CLI signed in on
  the Mac (`crewkit` itself is public). The agent's layers skip there and run locally; see Tests and CI in `docs/operations.md`.
