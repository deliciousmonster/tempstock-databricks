# Nine-minute demo script

The demo shows a system that makes calls, explains them, and says what its backtest measured. It's planned for nine minutes in a ten-minute slot, with a minute of slack for the fallback or a slow answer. The deck is a separate slot; if the two share one, use deck slides 1, 5, 6 and 10 and start here.

Open five browser tabs before starting: the App, the AI/BI dashboard, the Genie space, and the private run record and `verify-agent.md` rendered from the repo. Check the App an hour before, not at the start; see the fallback at the end.

Cumulative marks are in the headings.

## 0:00 to 0:45. Frame

Say what it is: hourly buy, sell or hold calls on WMT from US temperature, built to find out whether anything is there. State the result before showing anything: the champion gate fails. After costs the walk's net P&L is below the 95th percentile of its own shuffled positions and below zero, so no model is promoted and every call holds (claims.md row 6i). On hit rate, no slice beats always buying, real forecasts included, and the whole run sits 0.2 points under it (rows 4 to 6a). Returns and costs are measured now (row 6b).

Talking point: a client would rather hear that from a consultant than find it in their P&L. The demo's job is rigor, so the result comes first.

## 0:45 to 2:15. App, Outlook

Click: the App, then **Outlook**.

- The seven rows: today and the next six days, each with the agent's call and the model's signal beside it.
- Open one call's page: the model signal, the hold reason, the forecast anomaly and when the forecast was known, the agent's rationale and a trace summary. The model's `signal_action` appears as the signal, never as a call.
- If a row reads "awaiting guard check", say it's by design: the call is shown only after the guard has checked the hour (`docs/known-issues.md`). The snapshot runs at :15, so the page can lag by one hour.
- If most rows are hold, name the reason shown: a stale input, the guard, or an anomaly under the model's threshold.

Talking point: the model decides and Claude explains. Claude has no way to name an action; the database writes the action from the signal table. There is no `@champion` on dev, because the gate failed, so the rows hold with the reasons shown (`no_champion`, `stale_prices` or `horizon_unavailable`; row 6e).

## 2:15 to 3:45. App, History

Click: **History**. Say first that this page measures a different model from the one on Outlook: `threshold_wf` v2, the same threshold model refit each January, with the grid fixed before looking (row 7). Don't present it as the live calls' track record. The App's hit rate scores close t to t+1 while v2 learns close t+1 to close t+2; on the v2 label the hit rates barely move (row 7).

- The WMT close from 1991 with the temperature anomaly, and the backtest's buy and sell dots.
- The caption is the App's measure of the whole run as stored: hit rate 50.0% against 50.2% always buying, inside the random band (row 4). The run is `h0:threshold_wf:2` (row 3).
- From 2007: 3,240 calls hit 51.4% against 52.4%, inside their band (row 6a). The calls from real forecasts hit 52.4% against 53.9% (row 5). The 1991 to 2006 slice shows no lift either (row 6h).
- The normals are 1961 to 1990, which end before the first scored session, so no scored year feeds them (row 6d). Every observed input is the ERA5 value 8 days old by design, and none holds `stale_input` (row 6f); ERA5 is complete on dev (row 51).
- After costs the walk loses against its own shuffles, and long every day does better gross and net (row 6i).

Talking point: the gate is the test, and it fails; WMT is the second stock tried, after a null on CDW (row 6c). It doesn't show that no model works.

## 3:45 to 4:30. App, Agent runs

Click: **Agent runs**.

- One row per hour: outcome, route, duration. Route reads `databricks-gateway`.
- Show an `ok` row and, if present, a `guard_hold` or `signal_missing` row.

Talking point: a hold row is the system working. The agent never runs on an hour the guard hasn't verified; if the guard is late, the hour is held and recorded. Each Claude call is also logged in the workspace by the AI Gateway.

## 4:30 to 5:30. AI/BI dashboard

Click: **Sources**, **How calls firmed**, then **Agent and integrity**.

- Sources: freshness and pipeline expectations. A forecast column that repeats lead 0 is quarantined for that metro alone, and the signal holds as `forecast_quarantined`.
- How calls firmed: dates whose call changed between horizon 6 and horizon 0.
- Agent and integrity: runs per day by outcome, route, p50 and max duration, audit problems.

Talking point: the dashboard says "guard inputs as read" rather than "verified", because a dashboard can't see when it's viewed. It claims what it can see.

## 5:30 to 6:45. Genie

Ask, in this order:

1. "What did the agent call for each of the next seven days?" Answered in the 2026-11-19 UI check and matched the outlook (the private run record, the 2026-11-19 9:10 pm line).
2. "What is the outlook?" To rehearse: backed by an example query in the space, not recorded as run.
3. "Which agent runs failed in the last 48 hours, and why?" To rehearse: a sample question in the space, not recorded as run.

Drop any question that hasn't answered in rehearsal on the day; two will do. Talking point: the dashboard and Genie read the same six governed views, and Genie's instructions say what a column isn't, for example never to present the signal as a call and never to say a call is verified.

## 6:45 to 8:00. The proof

Show `verify.md` and `verify-agent.md` from the proof run of 2026-11-19. Every `dev_` schema, job, App and Genie space was destroyed and rebuilt by one bootstrap from the repository and the local data folder (the principals were reused). Then 20 tables, 5 jobs, the App, the agent profile, the LaunchAgent and the first scheduled score run were checked, and the first agent run after the rebuild wrote 7 recommendations for 7 signal rows with 0 downgrades (row 41). Proof-3 later rebuilt dev again: 55 recommendations for 55 signal rows over 9 runs, all `ok`, and verify passed 37 of 38 checks (rows 41, 42b).

Talking point: the repeatable part is the deliverable. A client engagement ends with someone else running this, so it has to come back from nothing but a repository and a data folder.

## 8:00 to 8:45. Close

State what I'd do for a client next: their data in place of WMT, the agent hosted as a workspace job, the same guard. Stop. About a minute of slack follows.

## Fallback if the App is stopped

Databricks stopped the dev App's compute on its own on 2026-11-19, with the message "stopped due to workspace or account status"; the restart came up with no deployment. The jobs and the agent kept running (`docs/known-issues.md`).

1. `databricks apps start dev-weather-signal-ui --profile weather-signal`.
2. If it reports nothing deployed, run `databricks apps deploy` for that app from the bundle's `files/app` workspace path. Look the path up and rehearse the exact command once beforehand; it isn't recorded in the repository.
3. The first sign-in after a restart can show a one-off "Login error" 403; navigate again (seen once, 2026-11-19, the private run record).
4. If it won't start, show the three pages from the private run record, then go on to the dashboard, which doesn't depend on the App. These screenshots predate the backtest result: `history.jpg` reads "No measured result is recorded", and `outlook.jpg` shows every row on hold. Retake all three on the current dev before the demo.
