# Cost view

# 1. Purpose and Scope

Show, in one place, how many tokens Argus's runs spent and what that probably
cost, per project and per account, over the last 24 hours, 7 days or 30 days.
The dashboard tab is **Cost** (`#cost`). Readable by the manager scope
(orchestrator, admin), like `GET /api/accounts`.

It reads the usage rows of [account usage](account-usage.spec.md) and adds
three things they lack: a **price table**, a **project** dimension (through the
task that produced the run) and a cross-account **window** with a daily series.

# 2. Non-Goals

- No billing, invoicing or budget enforcement; no alerts or thresholds.
- No call to any provider's billing API. Prices are a local file.
- No cost for subscription quota: a price turns tokens into dollars at API
  rates, which is what the same tokens would cost, not what was paid. The
  figure is labelled **estimate** everywhere.
- No write: the endpoint never inserts or updates a row.
- No history of past price tables: the file in force prices the whole window.

# 3. Decisions Made

## 3.1 Honest figures

- A `not_reported` run (account usage 3.2) is counted in `not_reported_runs`
  and contributes **nothing** to tokens or cost. It is never summed as 0.
- Token fields (`input_tokens`, `output_tokens`, `cached_input_tokens`) are
  `null` when no measured run in the group carries that figure. A group of
  only `not_reported` runs, or of no runs at all, therefore has `null` tokens.
  A real `0` means measured runs that reported zero.
- Every group states `runs`, `measured_runs` and `not_reported_runs`;
  `runs = measured_runs + not_reported_runs`. An account with `runs = 0` had no
  activity in the window, which is different from a measured zero.
- **Reported vs estimated cost never mix.** `reported_cost_usd` is the sum of
  the `cost_usd` the engine reported (null when none did). `estimated_cost_usd`
  is computed from tokens and the price table (3.2). They are separate
  fields and the UI shows the estimate, labelled, with the reported figure
  beside it when present.

## 3.2 Price table

- A JSON document: `{currency, as_of, version, models: {<model>: {input,
  output, cached_input}}}`, prices per **1,000,000 tokens**. The default ships
  as `argus_api/accounts/pricing.json`; `ARGUS_PRICING_FILE` replaces it
  wholesale. A malformed file fails the request with `pricing_invalid`; it is
  never half-applied.
- The shipped prices are **estimates**, not contractual rates, dated by
  `as_of`. Operators set their own.
- A model matches its key exactly, else the longest key followed by `-`
  (a dated suffix such as `claude-sonnet-5-5-20261001`). Matching ignores case.
- Estimated cost of a run group = `input × input price + output × output
  price + cached × cached_input price`, per model. A missing component counts
  as 0 only when the run has some other token figure.
- A model without a price (or a run without a model) is **unpriced**: its
  tokens still count, its cost is not estimated. `unpriced_runs` counts those
  runs in the group, `estimated_cost_usd` covers only the priced part and is
  `null` when nothing was priced. It is never 0 by omission. The window's
  `pricing.unpriced_models` lists the model names (`(no model)` for a run
  that named none).
- Runs that reported only a cost (no tokens) have nothing to estimate and are
  not unpriced.

## 3.3 Read API

`GET /api/accounts/cost?window=24h|7d|30d&tz=<IANA>` (default `24h`, `UTC`),
manager scope. The window is `(now − window, now]`. Response:

- `window`, `start`, `end`, `tz`;
- `pricing`: `{currency, as_of, version, source, estimate: true,
  unpriced_models}`; `source` is the file path in force or
  `argus_api/accounts/pricing.json` for the shipped one;
- `totals`, and `by_account`, `by_project`, `daily`: all carry the figures of
  3.1 (`runs`, `measured_runs`, `not_reported_runs`, the three token fields,
  `estimated_cost_usd`, `reported_cost_usd`, `unpriced_runs`);
- `by_account` has one entry for **every** model account (not `gitea`), with
  `account_id`, `name`, `provider` and the same `quota_status` as
  `GET /api/accounts` (the same function; account usage 3.5);
- `by_project` has one entry per project with runs, and an explicit entry with
  `project: null` for runs without a task or whose task is unknown;
- `by_account` and `by_project` are sorted: groups with measured runs first,
  by estimated cost descending (unpriced after priced), then total tokens
  (input + output) descending, then name. The **top 5** is therefore the
  first five entries with `measured_runs > 0`;
- `daily` has one point per calendar day of `tz` overlapping the window
  (first and last are partial), empty days included with `runs = 0` and
  `null` tokens.

## 3.4 Dashboard

- **Cost** tab: a 24h / 7d / 30d filter; tiles for input, output and cached
  tokens and the estimated cost with its currency, labelled "estimate";
  a "not reported" count when any run lacked figures; per-account and
  per-project tables with a bar and the top five; a daily bar chart; each
  account's quota status through the Accounts screen's component.
- A group with `null` tokens renders "—", never 0. An unpriced group renders
  "unpriced". The footer shows the pricing source, currency, `as_of`,
  version and the unpriced models.
- Hand-written SVG; no chart dependency.

# 4. Open Questions

- Whether a reported cost should replace the estimate for API-key accounts
  when every run in the group reported one.
- Per-model breakdown, and pricing tiers (long context, batch discounts).

# 5. Constraints

- Single node, SQLite, no new service or dependency.
- Aggregation is `GROUP BY` in SQL over the window, served by the index
  `account_usage (at, account_id)`; Python only prices the grouped rows
  (groups × models), never the raw rows.
- Read only: no write transaction, no network call, no clock read besides the
  request's `now`.

# 6. Implementation Consequences

- Migration `0020_account_usage_at.sql`: index `account_usage (at, account_id)`.
- `accounts/pricing.py` (+ `pricing.json`), `accounts/cost.py` (aggregation),
  the route in `accounts/routes.py`, models in `argus_core.cost`, setting
  `pricing_file` / `ARGUS_PRICING_FILE`.
- Account usage 2 ("No price table") is narrowed: *that* view still shows only
  reported cost; estimates live here, labelled.
- Dashboard `views/Cost.tsx`, pure helpers in `cost.ts`.
