# HANDOFF — pump.fun early-warning project

Audience: the implementing model. Follow this file and `config.yaml`. Where they are silent, pick the simplest option, record it in `docs/decisions.md`, and do not change any value in `config.yaml` except through the rules in section 5.

## 0. Fixed decisions and invariants

- **Goal:** at ~5 min after a pump.fun launch, predict "sustained 50% collapse within 60 min" better than RugCheck / GoPlus / Solana Tracker scores taken at the same moment, on later unseen tokens; then test a paper-traded strategy after costs. A negative trading result is an acceptable outcome; report it.
- **Claim sentence (fill X, Y, Z, A, B with measured values only):** "On pump.fun tokens still active five minutes after launch, this model predicts a sustained 50% collapse within 60 minutes with PR-AUC X, against Y for the best of the RugCheck, GoPlus and Solana Tracker scores taken at the same moment, on later unseen tokens; a paper-traded strategy built on it returned Z% per trade after costs (95% interval A to B)."
- **Free tiers only.** No spend without the user's approval (rule 5.3).
- **Paper trading only.** No private keys, no wallet/keypair objects, no `sendTransaction`, no signing code anywhere in the repo until the user explicitly says so. A unit test greps `src/` for `Keypair|sendTransaction|sign_transaction|private_key` and fails on a hit.
- **Time-based splits only.** Never a random split. Never plain accuracy as a headline.
- **Data access:** PumpPortal free streams (new token, migration) on one websocket, plus the public Solana RPC websocket (`wss://api.mainnet-beta.solana.com`) with one `logsSubscribe` on transactions that mention the pump.fun mint authority, i.e. create logs only, as a second launch source; everything else by polling RPC accounts. No trade streams, no Helius websocket (both metered). *Changed 2026-11-17 with the user's approval: PumpPortal's free stream was found to miss ~78% of launches (those in version-1 transactions); see `docs/decisions.md` D-31/D-32. Launch coverage is checked daily against the chain and must stay >= 95%. Any other launch source needs the user's approval.*
- **Hardware:** laptop (Ubuntu, 8 GB) runs the collector only. Desktop (WSL, 16 GB) runs parsing, training, simulator, dashboard. No GPU use.
- **Snapshot definition:** when the 300 s filter tick passes and the token is accepted, fire the snapshot batch. `snapshot_ts` = `observed_at` of the batch's closing reserve read; `P0` = price from that read. Parts not returned within 45 s are null.
- **Two notions of time, used everywhere:**
  - `observed_at` = UTC time the collector received the response.
  - `available_at` = when the fact became knowable: `block_time` for immutable chain history (transactions, signatures); `observed_at` for state reads (account balances, holders, scanner scores); `label_complete_at` for anything derived from an outcome.
  - A feature for snapshot S may use only rows with `available_at <= snapshot_ts(S)`.
- **Budget-forced deviation from the plan doc:** funder-based insider linking (wallets funded by the creator or the creator's funder) is off in v1 (`tracking.insider_funding_link: false`); it costs ~40 calls per token. Insider set v1 = creator + buyers in slot <= creation_slot + 1 + direct transfers from the creator seen in the first 20 trades. State this limitation in the write-up.
- **Milestones:** M1 collector (wk 1–2) · M2 labels, features, leakage tests, benchmark v1 (wk 3–5) · M3 model, calibration, walk-forward, simulator, graduation snapshot (wk 6–8) · M4 forward paper trading, dashboard, text ablation, review loop (wk 9–11) · M5 sensitivity grid, 100-token audit, lockbox, write-up (wk 12–14) · M6 go/no-go (wk 15–17).
- The user's open question (enter at 5 min vs graduated tokens only) is unanswered. Default stands: 5-min snapshot now, graduation snapshot added in M3.

## 1. Config

See `config.yaml` (shipped with this file). Every threshold lives there. `PLACEHOLDER` values are replaced by M1 measurements. Load with a typed schema (pydantic); unknown keys are an error.

## 2. Repo layout and make targets

```
Makefile  pyproject.toml  uv.lock  config.yaml  .env.example  README.md  HANDOFF.md
deploy/rugwatch-collector.service        # systemd user unit, Restart=always
src/rugwatch/
  config.py                              # typed config loader
  collector/  main.py pumpportal.py rpc.py scheduler.py snapshot.py budget.py scanners/{rugcheck,goplus,solanatracker}.py
  chain/      pump_layout.py tx_parse.py # account decoding, trade parsing
  storage/    raw.py parse.py tables.py  # raw writer, raw->Parquet, DuckDB views
  labels/     events.py classes.py sensitivity.py audit.py
  features/   asof.py registry.py groups/{onchain,price_path,early_trades,creator,holders,text}.py
  models/     hazard.py trade.py calibrate.py walkforward.py baselines.py
  eval/       metrics.py benchmark.py scorecard.py
  sim/        engine.py costs.py strategy.py
  review/     misses.py experiments.py
  dashboard/  app.py                     # Streamlit
tests/        unit/ leakage/ fixtures/   # fixtures: ~300 recorded tokens, committed
experiments/log.jsonl                    # pre-registered feature tests
reports/      docs/{decisions.md,writeup.md}    data/ (gitignored)
```

| Target | Does |
| --- | --- |
| `make setup` | Create venv from the lock file, copy `.env.example` to `.env` if missing |
| `make test` | Unit tests + the four leakage tests on fixtures + the no-signing grep |
| `make lint` | ruff + mypy |
| `make install-service` | Install and start the collector systemd unit on the laptop |
| `make status` | Collector uptime, last event age, credits used this month, websocket drops |
| `make report-day1` | Launches/day; pass counts per `threshold_grid_sol` at 60/180/300 s; share graduated or dead before 300 s; credits; drops |
| `make report-week1` | Credits per tracked token-day and monthly projection; storage/day; scanner coverage, latency, error rates; observed fee bps; observed priority fee and tip quantiles |
| `make parse` | Raw JSONL -> Parquet tables (idempotent, by date partition) |
| `make labels` | Build `labels` for tokens with matured horizons |
| `make audit-sheet` | Write the stratified audit CSV with explorer links and the assigned class |
| `make audit-score` | Read the filled CSV; print overall and per-class agreement and the disagreement types |
| `make features` | Build `features` and `intervals` through the as-of loader |
| `make leakcheck` | The four leakage tests on real data (200-token sample) |
| `make benchmark` | Scanner scores vs outcomes for matured benchmark tokens |
| `make train` | Walk-forward fit + calibration; writes `predictions`; hard-fails on split assertions |
| `make backtest` | Simulator on past test weeks, base and stress scenarios |
| `make paper` | Forward step: score newly snapshotted tokens with the frozen model, append `paper_trades` |
| `make scorecard` | Append the weekly row to `scorecard`; regenerate `reports/week-NN.md` |
| `make misses` | Top 50 confident errors from already-used weeks, for feature ideas |
| `make experiment ID=…` | Run one pre-registered feature test on the pending week and log the verdict |
| `make sensitivity` | Label grid: class shares, kappa between neighbours, model-vs-scanner gap per cell |
| `make lockbox` | One-time evaluation on lockbox weeks; refuses to run twice |
| `make dashboard` | Start the Streamlit app |
| `make backup` | rsync `data/` from laptop to desktop |

## 3. Data schemas

### 3.1 Raw record (one JSON object per line, zstd, append-only)

```json
{"schema":1,"observed_at":"2026-11-17T00:00:00.123456Z","source":"pumpportal|publicrpc|helius|alchemy|rugcheck|goplus|solanatracker",
 "kind":"new_token|migration|accounts|signatures|transaction|largest_accounts|scanner",
 "mint":"<base58 or null>","purpose":"launch_log|filter|snapshot|price|balance|creator|holders|scanner",
 "tick_s":300,"slot":123456789,"status":"ok|error|timeout","latency_ms":118,
 "request":{"method":"getMultipleAccounts","params_hash":"<sha256>"},
 "payload":{},"collector_version":"0.1.0"}
```

- `observed_at`: set once by the collector from the system clock (chrony-synced), microseconds, UTC. Never rewritten.
- Errors and timeouts are written too. A batched response is one record; the parser fans it out.
- Path: `data/raw/{source}/{kind}/dt={UTC date of observed_at}/part-{n}.jsonl.zst`.

### 3.2 Parquet tables

All tables carry `observed_at` and `available_at` unless noted. Amounts are raw integers (lamports, base units); `price` is SOL per whole token as float64.

| Table | Key | Columns |
| --- | --- | --- |
| `tokens` | mint | creation_sig, creation_slot, creation_block_time, creator, bonding_curve, token_program, name, symbol, uri, initial_buy_lamports, universe_version |
| `filter_ticks` | mint, tick_s | slot, virtual_sol, virtual_token, real_sol, real_token, complete |
| `tracking` | mint | tracked, reason (filter_pass \| random_rest \| not_tracked), filter_real_sol_300, sample_prob, benchmark, snapshot_ts, snapshot_slot, p0, snapshot_complete, missing_parts |
| `price_ticks` | mint, observed_at | slot, venue (curve \| pumpswap), virtual_sol, virtual_token, real_sol, price, complete |
| `balance_ticks` | mint, token_account, observed_at | slot, owner, role (creator \| early_buyer), is_insider, amount_raw, share_of_supply |
| `early_trades` | mint, sig | slot, block_time, seq, signer, token_account, is_buy, sol_lamports, token_amount, fee_lamports, fee_bps, priority_fee_lamports, tip_lamports, slot_offset_from_creation |
| `sig_stats` | mint | n_sigs_to_snapshot, n_failed, first_block_time, truncated |
| `creators` | creator, observed_at | n_sigs_seen, sigs_truncated, oldest_sig_block_time, funder, cache_hit |
| `holders` | mint, tick (snapshot \| t1800), rank | token_account, amount_raw, share |
| `scanner_scores` | mint, scanner | status, late (observed_at > snapshot_ts), score_raw, score_norm (higher = riskier), flags_json, latency_ms |
| `migrations` | mint | sig, slot, block_time, pool |
| `labels` | mint, label_version | snapshot_ts, p0, collapse_ts, runup_ts, first_event_60, t_event_s, class_24h, peak_price, final_price, insider_share_snapshot, insider_sold_frac, graduated_ts, censored_reason, label_complete_at |
| `features` | mint, feature_version | snapshot_ts, `f_*` columns, built_at |
| `intervals` | mint, k (0–59) | interval_start_ts, at_risk, event (0 none, 1 collapse, 2 runup) |
| `predictions` | mint, model_id | train_end_week, test_week, p_collapse_60, p_runup_60, hazards (list), p_target, p_stop, p_timeout, ev_mean, ev_lower |
| `paper_trades` | trade_id | mint, mode (backtest \| forward), scenario (base \| stress), model_id, week, signal_ts, entry_ts, entry_price, size_sol, tokens, entry_failed, exit_ts, exit_price, exit_reason (target \| stop \| timeout \| hazard \| graduation), fees_sol, priority_sol, net_return |
| `scorecard` | week | per model and scanner: n, base_rate, pr_auc_pos, pr_auc_neg, p_at_r50, p_at_r80, brier, ece; trading: n_trades, mean, median, ci_lo, ci_hi, mean_ex_top1pct, max_drawdown, stress_mean; n_features_tested, n_accepted |
| `credit_ledger` | hour, source, purpose | calls, credits |

Rules: `label_complete_at` = event time + 600 s for event fields, `snapshot_ts + 86400` for `class_24h`. Tokens whose tracking or snapshot failed stay in `tracking` with flags; never drop them silently. `price` = `virtual_sol / virtual_token` scaled to SOL per whole token.

### 3.3 Simulator arithmetic

- Fill tick = the 2nd `price_ticks` row with `observed_at > signal_ts` (+1 more in stress, +1 more with prob `exit_extra_tick_prob` on exits).
- Buy: `s = size * (1 - fee)`; `tokens = vT * s / (vS + s)` at the fill tick's reserves.
- Sell: `sol_out = vS * t / (vT + t) * (1 - fee)`.
- Barriers are checked on spot price against the spot price at the entry fill tick.
- `net_return = (sol_out - 2 * priority_fee - size) / size`. A failed entry returns `-priority_fee / size` and opens nothing.
- `size = min(size_sol, max_frac_of_real_sol * real_sol)`; skip if `< min_size_sol`.

## 4. Leakage tests (all four run in `make test` on fixtures and in `make leakcheck` on real data)

**L1 — Truncation invariance** (`tests/leakage/test_truncation.py`)
1. Sample 200 tracked tokens, seed 17, spread across all available days.
2. For each token T: build features from the full parsed tables. Then build them again from a copy in which every row of every table, for every mint, with `available_at > snapshot_ts(T)` is deleted (global truncation, so cross-token leakage through creator history or other tokens' outcomes is caught).
3. Assert every `f_*` column is identical: exact for ints and strings, `abs diff <= 1e-12` for floats, NaN equals NaN.
4. Same check for `intervals` time-varying covariates, truncating at each `interval_start_ts`, on 20 tokens × 5 random intervals.
5. On failure, print the feature names and mints that differ.

**L2 — Canary** (`tests/leakage/test_canary.py`)
1. With `RUG_TESTING=1`, register two features that bypass the as-of loader: `_canary_future_price` = price at `snapshot_ts + 600 s` / P0, and `_canary_label` = the token's own `class_24h`.
2. Run the L1 check function. Assert it fails and names both canaries.
3. Assert the registry raises if a feature name starting with `_canary` is registered without `RUG_TESTING=1`.

**L3 — Split integrity** (`tests/leakage/test_split.py`, and as hard assertions inside `make train`)
For every walk-forward fold:
1. `train_mints ∩ test_mints = ∅`.
2. Every train `snapshot_ts < test_start`; every test `snapshot_ts >= test_start`.
3. `max(train.label_complete_at) < test_start` for the label in use.
4. Calibration rows are strictly later than model-fit rows and earlier than `test_start`.
5. Hyperparameter search and early stopping read train rows only.
6. Lockbox weeks appear in no fold before `make lockbox`.

**L4 — Scanner timing** (`tests/leakage/test_scanner.py`)
1. A scanner score enters features or a baseline only if `status == ok` and `observed_at <= snapshot_ts`. Otherwise it is NaN and the token leaves that scanner's benchmark; coverage is reported.
2. Exactly one row per (mint, scanner) is ever used: the first one written. A second query for the same pair is a test failure.
3. Model-versus-scanner comparisons use the identical token set (paired).
4. Fixture includes a late score, a failed score and a duplicate; assert all three are excluded.

Structural guard behind all four: feature functions receive only an `AsOfView(snapshot_ts)` object; raw DuckDB connections are not importable from `features/groups/`. A unit test checks the import.

## 5. Decision rules for checkpoints

### 5.1 Activity threshold and tracked cap (from `report-day1`, rechecked at `report-week1`)

1. Take pass counts per day at 300 s for each value in `activity_filter.threshold_grid_sol`.
2. `threshold_sol` = the smallest grid value whose pass count is <= 500 per day. Never below `floor_sol` (4).
3. If even 30 SOL passes more than 500 a day, use 30 and hash-sample down to 500.
4. If the 4 SOL threshold passes fewer than 150 a day, keep 4, keep `random_rest_per_day` at 100, and tell the user the timeline to "enough data" roughly doubles.
5. Tracked cap: after day 7, with measured credits per tracked token-day `c` and fixed daily overhead `b`: `tracked_cap = min(600, floor((800000/30 - b) / c))`. Keep the 5:1 ratio of filter-pass to random-rest.
6. Re-evaluate once at day 7 using the 7-day median. Freeze at day 14. Any later change bumps `universe_version` and restarts the data clock for model claims.

### 5.2 Label adjustment after the hand audit

1. Sheet: 50 tokens, 10 per class, seed 17. The user marks agree or disagree plus the class they would assign.
2. Pass = overall agreement >= 85% and every class >= 70%. On pass, freeze `label_version`.
3. On fail, one adjustment round only: find the most frequent disagreement type and change only threshold values, only to values already in `labels.sensitivity_grid`. No new rule types. One exception is allowed: fixing the transfer-versus-sale check.
4. Re-audit a fresh 50. If it still fails, freeze anyway, publish the agreement rate, and demote the four cause-based classes to secondary analysis.
5. The cause-blind events (collapse, run-up) are never changed by an audit. The headline claim and the simulator depend only on them.
6. The M5 100-token audit is reporting only.

### 5.3 Paying for data (not before week 10; ask the user first, show the inputs)

1. **Do not pay** if the model's paired PR-AUC beats the best scanner in fewer than 4 of the last 6 test weeks. Publish the benchmark and the negative result instead.
2. **Helius Developer ($49/month)** if rule 1 is met and either: the credit meter throttled more than 5% of scheduled polls or the tracked cap is below the daily filter-pass count; or early-trade features are in the top 10 by gain. Then: parse all trades for tracked tokens and enable `insider_funding_link`.
3. **Solana Tracker €50/month** only if the claim is blocked by benchmark sample size: the 95% interval of the model-minus-Solana-Tracker PR-AUC gap has half-width above 0.03 at week 10.
4. Cap total at $100 a month. Review monthly; cancel if the paid data adds less than 0.005 PR-AUC in its first 4 weeks.

### 5.4 Weekly review loop (weeks 6–10)

1. `make misses` shows 50 confident errors from weeks already used for a decision. Never look at the pending week.
2. Propose at most 3 feature groups a week. Before scoring, append each to `experiments/log.jsonl`: id, date, hypothesis, columns, the single test week.
3. Test: train on all earlier weeks with and without the group; score once on the newest matured week, which is used for that week's batch and never again for selection.
4. Accept only if all hold: paired PR-AUC gain >= 0.005 on the headline target; bootstrap probability of a positive gain >= 0.90; calibration error not worse by more than 0.01.
5. Rejected groups are not retried for 4 weeks. No hyperparameter or threshold tuning on test weeks, ever.
6. Feature freeze at the end of week 10. Lockbox weeks 11–13 are scored once in M5. After the freeze the model is retrained weekly but not redesigned.
7. The write-up reports the total number of proposals tested and accepted.
8. Strategy levels (target, stop, timeout, EV threshold) may change only between forward weeks, using training data only, and each change is logged.

## 6. Open questions and risks to verify in M1

| # | Item | How to settle it |
| --- | --- | --- |
| 1 | Bonding-curve account layout, program ids, create instruction variants, Token-2022 mints | Decode against the current official pump.fun IDL; unit-test on 20 live accounts; take token accounts from `postTokenBalances`, never derive them |
| 2 | Real curve fee: 125 bps assumed from secondary sources; the official fee page looked outdated; fee may differ per token under the creator fee models | Compute `fee_bps` on every parsed early trade; report the distribution; use the per-token fee in the simulator |
| 3 | PumpPortal payload fields, reconnect behaviour, missed launches | Log gaps; compare daily launch count with an independent public figure (~26,000 a day in autumn 2026). *Settled 2026-11-17: PumpPortal's free stream misses v1-transaction launches; second source added (section 0), daily coverage check against the chain, alert below 95%.* |
| 4 | Helius: 1 credit per 100-account `getMultipleAccounts`, 10 rps, credit burn | Read the dashboard after day 1; reconcile with `credit_ledger` |
| 5 | RugCheck free limit and whether a key is needed; GoPlus credits per Solana call; Alchemy cost per Solana method | Measure on day 3; record in `docs/decisions.md` |
| 6 | Scanner readiness at 5 min: do they return a score for a 5-minute-old token within 25 s | If a scanner's on-time coverage is below 80%, extend `snapshot_batch.deadline_s` to 60 once, in M1 only |
| 7 | Scanner score direction and scale; GoPlus has flags, not one score | Define `score_norm` per scanner in M1 (GoPlus: count of triggered risk flags); freeze before any outcome is viewed |
| 8 | Scanner terms on storing and publishing scores | User reads terms; a restricted scanner stays in the analysis but out of the public data release |
| 9 | Typical priority fee and tip | Quantiles from parsed early trades and `getRecentPrioritizationFees`; set `priority_fee_sol_per_tx` to the median |
| 10 | Share of tokens dead or graduated before 300 s | In `report-day1`; these are outside the universe and must be stated in the write-up |
| 11 | Clock skew between `block_time` and `observed_at` | chrony on the laptop; log the skew distribution; alert above 2 s |
| 12 | Transfer-versus-sale errors in insider selling | Audit sheet column; feeds rule 5.2 |
| 13 | 15 s ticks miss moves inside a tick; sandwiching is not modelled | Keep the pessimistic fill rule; mention both in the write-up; stress scenario is the guard |
| 14 | Laptop reliability: sleep, network drops, disk | systemd restart, `make status`, weekly `make backup`; exit check is 95% uptime over 10 days |
| 15 | MemeTrans licence (listed as CC BY-NC-SA) | Use for pipeline development only; never redistribute or use for final claims |
| 16 | Insider funding links are off in v1 | Hard and soft rugs are undercounted; state it; first use of paid credits |

**M1 exit checks:** uptime >= 95% over 10 days · projected Helius use <= 800,000 credits a month · audit passes rule 5.2 or is closed by its step 4 · every `PLACEHOLDER` in `config.yaml` replaced with a measured value · scanner terms checked.

**Stop and ask the user before:** spending money, changing the claim sentence, changing anything in section 0, or any step towards real trading.
