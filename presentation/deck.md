# weather-signal deck content

Eleven slides. Each has a headline written as a claim, up to five bullets and speaker notes. The visual deck is built from this file; claims.md sources every number. If the deck and the demo share one slot, use slides 1, 5, 6 and 10 and go straight to the demo.

## 1. Title

**Headline: Hourly calls on WMT from the weather, built to show whether they're worth anything**

- weather-signal: buy, sell or hold on WMT from US temperature, on Databricks Free Edition
- Personal, non-commercial demo; not investment advice
- A walkthrough of the method for a technical audience

Speaker notes: Open with the result, as measured: the champion gate fails. After a 0.001 cost on every position day, the walk's calls from 1991 lose 4.48 on a unit stake, worse than 163 of 200 shuffles of its own positions, and buying every day does better gross and net. No slice beats always buying on hit rate, real forecasts included. So no model is promoted and every call holds. Everything after is how that was found and why the method can be trusted.

## 2. The problem

**Headline: The useful question is whether the signal exists, so the build had to be able to say no**

- A weather thesis for a retail stock is easy to believe and easy to overfit
- A client's risk team will ask who decides, who explains, and what happens when inputs go stale
- The design splits those: a model decides, Claude explains, a guard checks, the database writes
- The plan committed in advance to shipping a model whose test says "no edge", with an agent that holds

Speaker notes: The framing comes from the project plan (private). For a client conversation, a consultant who says "nothing here" before the P&L does saves the client the loss.

## 3. Architecture

**Headline: Everything the signal reads lives in Unity Catalog; one piece runs on a Mac, by design**

- Sources land once, as received, in `raw` (bronze); a Lakeflow pipeline builds `daily` (silver); a score job writes `signal`, and with `app` they are gold
- The agent runs hourly on my Mac through Databricks Connect and the AI Gateway
- The dashboard and the Genie space read the same six `v_ui_*` views; the App reads those and the backtest view
- One Asset Bundle plus a bootstrap deploys all of it
- Weather data by Open-Meteo.com (CC BY 4.0)

Speaker notes: Show the diagram from architecture.md. Name the tiers: `raw` is bronze (responses as received, plus the ledger volume), `daily` is silver (observed weather, forecasts, prices, sessions, metros), and `signals` plus `app` are gold (normals, index, signal, backtest, recommendations, agent runs, and the views and functions the App, dashboard and Genie read). The names say what each layer holds; the tier is the Databricks convention. The Mac is a trade: it uses my Claude subscription, and the hour runs only while the Mac is on. The hosted alternative is a workspace job on the agent's own principal, which costs per token and gains scheduler, retries and audit.

## 4. Data and governance

**Headline: Every value carries when it became knowable, and one function decides what a decision may see**

- `known_at` on every value; `asof.visible` is the single rule, used by the live frame and the backtest alike
- Signals and recommendations are append-only with CHECK constraints; a guard reads each table's id, state and history before and after each score
- Separate principals for the jobs' runner and the agent; the agent can read and execute in `signals` and write only `recommendations` and `agent_runs`
- Bad forecast columns are quarantined one metro at a time, and the signal holds with a named reason

Speaker notes: A backtest that peeks at a value before it existed produces a flattering lie, so the next slide says where this one peeks. A backfilled ERA5 day is treated as known eight days after its date. The tamper tests ran as the owner, not as the runner or the agent principal; the design note says so. In Q&A: the observed value is ERA5's latest revision, not as first seen, and the fit pools observed and forecast inputs as one feature.

## 5. The signal and the result

**Headline: After costs the calls lose 4.48 since 1991, below the 95th percentile of their own shuffles, so the gate fails and nothing is promoted**

- 1991 to 2026, 9,030 sessions, 4,876 trades: net P&L -4.48 (gross +0.39) against a shuffle 95th percentile of -2.03; 163 of 200 shuffles do at least as well
- Buying every session makes +5.08 gross, -3.95 net at the same cost per day: ahead of the walk either way
- Hit rate 50.0% against 50.2% always-buy, inside the 95% random band of 48.1% to 50.7%
- Real forecasts, 1,251 calls from 2021: 52.4% against 53.9% always-buy; net -0.73 against a p95 of +0.08 over those sessions
- Normals are 1961 to 1990, before the first scored session; every observed input is 8 days old, none stale

Speaker notes: The gate is the test. Each position is held on WMT's return from close t+1 to close t+2 and pays 0.001 when it isn't flat; the gate shuffles each calendar year's block of positions 200 times and passes only above the 95th percentile and above zero. The walk is below both, and below the shuffles' median of -3.40, so where the calls fell did no better than a shuffle of the same calls. Both sides are too thin to pay the cost: buy days average 0.032% gross, sell days 0.064%, against 0.1%. From 2007 the walk is -1.77 against a p95 of -1.26, and 1991 to 2006 hits 47.2% against 47.6%; no slice shows lift. The App's caption is a hit rate against the next close, the old label; on the label the model learns it barely moves (50.0% against 50.2%). The earlier deck's lift came from the dev set before the rebuild: normals drawn from the scored years and 1,369 buys on one frozen 2007 reading. Both are gone, and so is the lift. WMT is the second stock tried, after a null on CDW (51.1% vs 53.3%), and the metro study, rerun on WMT, found no metro or combination that survives the 2013 to 2026 holdout (smallest Holm p 0.47; New York, Miami and Memphis together hit 50.2% against 49.2% always-buy). With the gate failed there is no `@champion`, so the Outlook holds every target with `no_champion`; History shows `threshold_wf` v2, the threshold model refit each January.

## 6. The agent

**Headline: Claude explains the call and may soften it to hold; it cannot name an action**

- Three narrow tools over UC functions; the rationale is Claude's, the action is the model's
- The database writes the action from the signal table with a `MERGE`; the agent binds a downgrade and a reason, never an action
- Each model call goes through the AI Gateway and is logged in the workspace; only a failed gateway pre-check goes direct, recorded as `direct_fallback`
- launchd runs the hour at :40 on my Mac, isolated from my Claude Code setup
- Evaluation of the rationale isn't built; MLflow traces are kept per call

Speaker notes: In the proof run the agent wrote 7 recommendations for 7 signal rows, 0 downgrades; on the rebuilt dev it has written 55 for 55 over 9 runs, all `ok`, 0 downgrades. A downgrade hasn't been exercised in the evidence shown here; say so if asked. The model would be `stock_signal@champion`, which the train job registers in Unity Catalog only when the gate passes; it failed, so today every call the agent explains is a hold. `python -m weather_signal.agent.evaluate` refuses without the evaluation table, which doesn't exist yet. Databricks documents the subscription provider service for a developer running `ug claude`; this unattended use works as measured and isn't documented.

## 7. The guard and the failure modes

**Headline: The system holds, and records why, whenever it can't trust an hour**

- The agent waits until the guard has verified the hour, and holds as `guard_hold` at :55 if it hasn't
- A deadline dump and a watchdog end a stuck run with every process it started
- Stale prices hold the signal; a forecast older than 30 hours counts as stale
- On 2026-11-19, a fresh dev set held h0 to h5 on `stale_prices` and h6 on `horizon_unavailable`

Speaker notes: A visible, named hold beats a quiet wrong number. The agent waits on the guard because timing measured on 2026-11-19 showed the score job's check ending 12 to 16 minutes after its start, and the :40 runs on two hours read the guard first and held.

## 8. Operating it

**Headline: Dev was destroyed and rebuilt by one command, and verified**

- `weather-signal bootstrap` builds principals, bundle, secret, seed, catch-up, model, App, Genie space and the LaunchAgent
- The proof run destroyed every `dev_` schema, job, App and Genie space, rebuilt them from the repository and the local data folder, and passed `verify` and `verify-agent` on 2026-11-19
- The first proof run stopped at destroy on the bundle's `prevent_destroy`; the second run's verify mis-graded two checks; both were fixed and the checks rerun
- CI on GitHub runs lint, types and the offline suite with no secret; the latest main run is green

Speaker notes: The proof is what a client cares about: the system can be handed over. The principals were reused, not recreated. A third proof rebuilt dev from empty on 2026-11-20 with the complete ERA5 history; its verify passed 37 of 38 checks, and the one fail read the first attempt's record instead of the resumes', since fixed and not yet rerun. Name the limits: CI skips the agent's layers because their dependency is private, and `tests/ui` needs the Mac's Chrome.

## 9. What is not finished

**Headline: The known gaps are written down, with their workarounds**

- Databricks stopped the App's compute once, on 2026-11-19 ("workspace or account status"); `databricks apps start` brings it back
- The agent is one host on a Mac; a lost disk stops it until it's reinstalled
- The App's hit rate still scores the old label (close t to t+1) while the model and the gate use close t+1 to t+2
- Known issues, each with a workaround, are in `docs/known-issues.md`

Speaker notes: Everything here is also in the repository.

## 10. What I would do for a client next

**Headline: Keep the controls, swap the subject, and host the agent in the workspace**

- Replace the weather and the stock with the client's data and decision; keep `known_at`, the guard and the append-only tables
- Move the agent to a workspace job on its own service principal, with scheduler, retries and audit
- Fix the question before the build: what result would count as no, and where it will be shown
- Run the destroy-and-rebuild proof in the client's workspace before handover

Speaker notes: These are proposals, expected rather than measured. The hosted agent costs per token against the Mac's subscription. Bootstrap already creates the agent principal's OAuth secret.

## 11. Close

**Headline: The method is the part to trust, and the repository shows it**

- Result first: after costs the calls lose money and do no better than shuffles of themselves, so the gate held every call
- A decision split a risk team can audit
- A deployment that rebuilds from the repository and a data folder

Speaker notes: Stop here and take questions. Offer the demo: App, dashboard, Genie, then the proof report.
