---
status: implemented
created: 2026-10-16
updated: 2026-11-12
---

# Engines, executors and the local model

# 1. Purpose and Scope

Say precisely **who does what** when a NanoClaw worker works, report what is
*really* available, and send cheap work to the model already running on the
node without sending hard work there.

Four axes are kept apart because one string ("anthropic", "codex") hides them:

| Axis | Question | Examples |
|---|---|---|
| model provider / model | whose model answers? | `openai/gpt-*`, `anthropic/claude-*`, `local/qwen3.8-flash-next-iq3_xxs` |
| **engine** (harness/runtime) | what runs the agent loop of the worker? | NanoClaw provider `codex` or `claude`, whichever the best-priority leased account runs |
| **executor** (CLI) | what a worker delegates non-interactive jobs to | `claude`, `codex`, `opencode`, `agent` (Cursor), `dsh` (opt-in) |
| account / auth | which credential or login makes it work? | Argus account (`provider` string), a native CLI login in a private volume |

In scope:

- `GET /api/engines` and MCP `list_engines`: the catalogue, every entry with
  its axes, auth methods and a three-part readiness (below). `GET` only reads.
- `POST /api/engines/route` and MCP `route_task`: a deterministic routing
  decision for one task description (local, or an engine) with a reason.
- `POST /api/engines/local/run` and MCP `run_local`: one bounded, side-effect
  free completion on the local model, checked against caller-supplied
  criteria; the response says `accepted` or `escalate` and where to.
- The NanoClaw side (`integrations/nanoclaw/engines/`): the pinned Codex
  provider payload, the four CLIs in the agent image, `argus-delegate` (the
  non-interactive delegation entry) and the smoke that writes the readiness
  ledger.

# 2. Non-Goals

- Making the local model a Codex backend. Codex custom providers need the
  OpenAI Responses protocol with streaming; the local server was measured to
  answer a basic non-streaming `/v1/responses`, which is not what Codex needs.
  The local model is a **delegated executor** reached through Argus, never
  "the Codex harness on a local model".
- Reading any login from the host or from Hermes by itself. An operator may
  *import* a document they are authorised to use, see
  [codex accounts](codex-accounts.spec.md).
- A required service. The router is functions in the API process; the local
  model is optional and off unless `ARGUS_LOCAL_MODEL_URL` is set.
- Running the cloud opencode account, or any paid smoke, without a login made
  for that container.
- Replacing the NanoClaw provider contract: Codex is the upstream payload,
  unmodified, applied through its documented seams.

# 3. Decisions Made

- **OpenAI-compatible endpoints** (OpenRouter first) are `custom` accounts, see
  [custom accounts](custom-accounts.spec.md) and the NanoClaw [custom harness](custom-harness.spec.md).
  `engines/local.py::validate_url` and the account `base_url` share one URL rule.
  `argus-delegate` runs OpenCode (`base_url`, `model`, `provider` block) and Codex
  (`base_url`, `model`; no `provider` block) on executor leases from `settings`, with
  the key only in `ARGUS_ACCOUNT_API_KEY`; `claude` and `agent` are refused there.
  The harness uses Claude Code against a host bridge, not the provider URL directly.

- **`dsh` (DeepSeek Harness) is an opt-in executor** ([draft](../drafts/2026-11-04.dsh-engine.draft.md)),
  off unless `ARGUS_DSH_ENABLED=true`: disabled, the catalogue does not list it and
  `smoke.sh` does not probe it; no other engine's default changes. It is an executor
  only, on the custom-account contract: `argus-delegate dsh` takes `base_url` and
  `model` from the lease `settings` and the key from `ARGUS_ACCOUNT_API_KEY` (never
  in a file or argv). dsh reads its provider from a profile overlay, so the delegate
  writes a per-run overlay (no key) and runs
  `dsh --profile headless --patch OVERLAY --json -` in the task workspace, prompt on
  stdin, `DSH_HOME` a private per-run dir. Its ndjson `final` event is the summary;
  an `error` event, a non-`completed` final or no final is a failure; unknown events
  are ignored; the dsh version is reported. Blocked reports keep the usual
  `blocked` value and add a `code`: `dsh_not_installed` (127),
  `dsh_provider_credential_missing` or `dsh_provider_unconfigured` (78), or the
  `<CODE>` of a stderr `dsh: <CODE>: <message>` line when it is `MISSING_CREDENTIAL`
  or `INVALID_CREDENTIAL` (78). `installed` is the `dsh --version` result and never
  sets `authenticated`: with the key unset, `authenticated` is `false` with the
  `ARGUS_ACCOUNT_API_KEY` reason. dsh needs **Node >= 24.9**; the agent image keeps
  Node 22, so `integrations/nanoclaw/install-dsh.sh` unpacks a pinned Node 24 (sha256
  per arch) and a pinned dsh (npm sha512 integrity) into their own prefixes with
  a `dsh` wrapper, failing closed before extracting on any mismatch or unsupported
  arch. The script is not yet run by the image build (needs `xz-utils`).
  - **ACP verdict for dsh: drop for now.** Measured with dsh 0.2.2-alpha.3 under
    Node v24.22.2, run unchanged via `dsh --profile acp`: `initialize` ok (ACP v1,
    `authMethods []`); `session/new` ok (model and `reasoning_effort` config
    options); `session/prompt` failed with -32603 "llm-deepseek: no API key for
    provider route deepseek-official". The custom OpenRouter route is not wired
    into dsh's ACP config.
  - Comparison agents: Cursor `agent acp` 2026.10.16: `initialize` ok
    (`authMethods` `cursor_login`); `authenticate` got no reply within 120 s;
    `session/new` returned "Authentication required". `copilot --acp` 1.0.105:
    `initialize` ok, then "Authentication required". The worker image has no
    Cursor or Copilot login.
  - No ACP bridge or provider was added. The success criterion (two ACP agents,
    unchanged, end to end) is unmet.
  - The failures are environmental (missing credentials and logins), not an
    architectural limit of the adapter. Do not read this verdict as "the adapter
    cannot work".
  - Reopen when both agents answer `session/prompt` with a live assistant reply,
    which needs a provider key for dsh and a logged-in Cursor or Copilot account
    in the worker image.
  - Source: task 04268b71 attempt 4 (`acp-evaluation-report.md`,
    `acp-run-dsh.txt`, `acp-run-cursor-agent.txt`, `acp-run-copilot.txt`).
- **Readiness is three separate facts, never one.** `installed` (the binary
  exists in the image and reports the pinned version), `authenticated` (a
  credential for *that container's* use exists), `validated` (a real
  non-interactive task completed and was checked). Each is `true`, `false` or
  `null` (unknown) with a detail and a timestamp. `installed` is also false,
  naming them, when the image lacks a binary every worker needs (`ssh`, `rsync`,
  `git`): `smoke.sh` probes the image with `docker run … command -v` and records
  it as `tools` on each row; a missing or inconclusive record (docker failed)
  changes nothing, so listing never runs a container. An entry is `usable` only if
  all three are `true`. Installed does not imply authenticated; the host's
  login proves nothing about a container.
- **Two layers per engine.** An engine is a *harness* (the NanoClaw provider that
  runs the agent loop) and/or an *executor* (the CLI `argus-delegate` runs for
  the code). Matrix: Claude and Codex are both; opencode, Cursor (`agent`) and `dsh` are
  executors only; `local` is an executor only. Each layer has its own
  `readiness`, `accounts` and `usable` in `layers[]`: validating Claude as a
  harness proves nothing about Claude as an executor. The top-level `readiness`,
  `accounts` and `usable` are kept and equal the primary layer (the harness when
  there is one); `role` is deprecated and derived (both layers: `code_executor`,
  harness only: `orchestrator`, executor only: `executor`), so Codex now reads
  `code_executor`. Routing by [task difficulty](task-difficulty.spec.md) reads the
  executor layer only.
- **The ledger is a file** (`<data_dir>/engines-ledger.json`), written by
  `integrations/nanoclaw/engines/smoke.sh` and read by the API. It holds facts
  with evidence, never secrets. Absent file or absent entry means `null`. For
  cloud engines, `validated` in the ledger is authoritative when present. For
  the **local** entry, `validated` in the catalogue is **not** taken from a
  bare ledger flag alone: it requires a matching `validation_identity`
  (`endpoint`, `model`, `n_ctx` as probed) on the ledger row, or an
  `engine.local_run` event with `verdict: accepted`, `criteria_effective: true`,
  and the same identity. `GET /api/engines` only reads the ledger and the event
  log; it never runs a probe to infer validation. Per-layer facts live under
  `layers.<harness|executor>` on the row; the flat `installed`/`authenticated`
  are shared by both layers and the flat `validated` applies to the primary layer
  only, so executor validation of Claude or Codex needs `layers.executor.validated`.
- **Local validation from real runs.** `POST /api/engines/local/run` (and MCP
  `run_local`) records `engine.local_run` with `validation_identity` and whether
  criteria were effective. Only `accepted` with effective criteria can satisfy
  `validated` for routing. `escalate` or insufficient answers do not. Changing
  the configured endpoint, served model, or `n_ctx` invalidates earlier events.
  Prompts, answers and credentials are not stored in the catalogue.
- **The engine follows the leased account.** Which of Codex or Claude Code runs the
  worker loop, and which is the preferred executor, is decided by account priority
  ([global account selection](account-selection.spec.md)), not by a fixed role.
- **Codex turn deadline is configurable for Argus delivery.** Upstream NanoClaw
  hard-codes a ten-minute engine turn timeout in `container/agent-runner/src/providers/codex.ts`.
  Argus installs `codex-turn-timeout.ts` (overlay via `integrations/nanoclaw/install.sh`)
  and patches `codex.ts` to read `ARGUS_CODEX_TURN_TIMEOUT_MS` from the agent
  environment (forwarded through `CODEX_ENV_ALLOWLIST`). Unset keeps the
  ten-minute default; valid values are `60000`–`7200000` ms (two-hour delivery
  ceiling). Invalid values fail the turn at resolution time. On expiry the runner
  interrupts the active Codex turn before surfacing the error. Radar project
  images set `ARGUS_CODEX_TURN_TIMEOUT_MS=7200000` in
  `engines/projects/radar.Dockerfile`; other pools may set the same variable on
  the agent image or session env without changing the global default.
- **No silent fallback.** `argus-delegate` for an executor without
  credentials exits `78` and prints `{"blocked":"auth_missing"}`. It never
  substitutes another executor or provider. `argus-delegate EXECUTOR --auth-probe`
  runs only those credential-presence and CLI-install gates (exit `0`, `78`, `127`; `124`
  when `--version` hangs, which is inconclusive and never `ok`; it never asks the
  provider whether the credential is accepted) for the
  [executor probe](failure-classes.spec.md). A route passes over an engine without
  an available account explicitly: the decision names that engine and why
  ([codex accounts](codex-accounts.spec.md)); with nothing usable it is
  `blocked`.
- **Delegation contract** (`argus-delegate EXECUTOR`): prompt on stdin or
  `--prompt-file`; `--cwd` must resolve under `ARGUS_DELEGATE_ROOTS` (default
  `/workspace`); a mandatory timeout; the child runs in its own process group
  and the whole group is killed on timeout or on SIGTERM; stdout of the CLI is
  kept as an artifact file; the final line on stdout is one JSON object with
  `engine` (`ARGUS_ENGINE`), `executor`, the executor version, `model` when
  known, `cwd`, `exit_code`, `timed_out`, `duration_ms`, `artifacts`, and the
  run's `usage`, `run_key` and `usage_report`. Each attempt's token usage is
  parsed, spooled to `<out>/usage-*.json`, reported best-effort to the
  executor's account or appended to `argus-usage.jsonl` for the NanoClaw host
  when the child has no API env; it never changes the exit code
  ([account usage](account-usage.spec.md) §3.8–3.9).
  By default the Argus NanoClaw channel states this contract in every claimed task
  body (`integrations/nanoclaw/argus.ts` `renderTask`; see
  [prompt-data isolation](prompt-data-isolation.spec.md)): implementation must use
  `/usr/local/bin/argus-delegate` (Cursor → `agent`); direct `cursor-agent` /
  vendor CLIs are forbidden for delegated coding; materialized gateway stubs are
  authorized non-secret metadata; the worker attaches the delegate JSON report
  and resumes the same task on failure rather than inheriting another task's block.
  The one exception is the opt-in, experimental single-layer arm below.
- **Single-layer coding run (experiment, off by default;
  [single-layer experiment](single-layer-experiment.spec.md)).** With
  `ARGUS_SINGLE_LAYER=claude` and an assignment file on the NanoClaw host, a claim
  the file assigns to `single_layer` runs with the Claude harness as the coding
  model. The harness gets the implementation-tier chain the delegate would have
  used for a Claude executor, and the prompt replaces the delegation block with
  "work directly, do not call `argus-delegate`". It applies to the Claude harness
  on an `anthropic` engine lease only. Any other flag value (`codex`, `cursor`,
  `local`, `custom`, `copilot`, …) leaves the flag off and the host logs the
  refusal. A claim whose precondition fails, for example a non-Claude lease, a
  pinned harness or executor model, or no implementation chain, runs the default
  path and records the reason. It is never emulated through the delegate.
  Unchanged in both arms: default routing and account leases, isolation and
  workspace, the task protocol directives, structured PR delivery, credentials
  held by the gateway and never shown to the model, and cancellation. The arm is
  recorded in the harness model selection's `explanation`, with no schema change.
- **Local routing is conservative and deterministic.** A task goes to the
  local model only if *all* hold: the caller declares it simple and low-risk
  and gives deterministic acceptance criteria; it touches no auth, security or
  secret topics; the **estimated input plus the
  reserved output fits** the served context times a safety fraction
  (default 50 % of `n_ctx`, output reserve 2 048); the input estimate counts
  instructions, tool schemas, history and inputs. This is an **upper bound**, not
  a tokenizer guarantee: when only a character count is known, assume up to four
  UTF-8 bytes per codepoint; when text or the serialized chat payload is known,
  assume one token per UTF-8 byte, plus a configurable chat-template overhead
  (`ARGUS_LOCAL_CHAT_TEMPLATE_OVERHEAD`, default 512). `run_local` budgets the
  full JSON body sent to `/v1/chat/completions` (messages, `response_format`,
  schema). Unknown context size (model not probed)
  routes away. Nothing is truncated: a task that does not fit is routed away,
  never trimmed.
  File count is a rule of the single-completion path only. A `route` decision
  without a `difficulty` that would send the task to the local model (the
  `run_local` completion: one chat completion without tools that act on the
  filesystem) fits only a single narrow change, so `multi_file` is rejected there
  (`multi-file change (single completion without tools)`). `run_local` itself
  takes no file count. The NanoClaw local harness is different: it orchestrates
  only (small context, [local harness](local-harness.spec.md)) and delegates the
  file writes to the Cursor executor (`agent`, `composer-2.5`, see
  [task difficulty](task-difficulty.spec.md)), which makes multi-file edits. A
  `route` decision with a `difficulty` whose executor resolves to `agent` is that
  harness path, so `multi_file` does not block it; the limit there is context.
- **Escalation is bounded and effect free.** The local run is a single
  completion without tools that act, so a retry or an escalation duplicates
  nothing. `max_attempts` (default 1, cap 2) counts local attempts; an
  insufficient answer returns `verdict: escalate` with `escalate_to`
  (`codex`). Argus does not start the escalated work; the orchestrator creates
  the task, so the usual task rules apply.
- **Every decision is recorded** in the event log (`engine.routed`,
  `engine.local_run`): target, reason, effective model, token usage and
  latency when the server reported them. Cost is `null` (unknown) for the
  local model, not `0`.
- **The local endpoint is reached by Argus, not by workers.** The model
  listens on loopback only and has one slot. Workers already reach Argus, so
  `run_local` goes through it: no new bind, no firewall change, no Docker
  socket, and the allow-list (one configured model, token and time budgets)
  lives in one place. Concurrency is one at a time (the server has one slot);
  a waiting request that exceeds its budget fails with `busy`.
- **Local harness failover** ([local harness](local-harness.spec.md)): a `local`
  account provider leases after every cloud account; NanoClaw runs `claude` against
  Strata via `host.docker.internal` with a strict `ANTHROPIC_BASE_URL` allowlist.
- **Scopes:** `admin` and `orchestrator` read the catalogue and may route;
  `worker` tokens may call `run_local` (budgeted) and `list_engines`.

# 4. Open Questions

- Codex credentials are Argus accounts (import or dedicated device login),
  delivered through the OneCLI vault, see [codex accounts](codex-accounts.spec.md).
  Until an `openai` account exists, `authenticated` stays `false`.
- Whether a Responses-streaming shim for the local server is worth owning.
  Not decided; this spec does not need it.
- arm64 pin for the Cursor agent package (only x64 is checksummed).

# 5. Constraints

- Single-node spec applies: no new required service, SQLite untouched (the
  ledger is a file, decisions are events, no migration).
- No secrets in the ledger, the catalogue, the image or the source bundle.
- The local model URL must be loopback or a private address the operator
  names; the API refuses a URL with credentials in it.
- Budgets are configuration (`ARGUS_LOCAL_*`), conservative by default.

# 6. Implementation Consequences

- `packages/core`: `engines.py` models (catalogue, readiness, route request
  and decision).
- `apps/api`: `engines/` (catalogue, ledger reader, local probe and client,
  `local_readiness` event reader, router), routes under `/api/engines`, three
  MCP tools, settings
  `ARGUS_LOCAL_MODEL_URL`, `ARGUS_LOCAL_MODEL`, `ARGUS_LOCAL_MAX_INPUT_FRACTION`,
  `ARGUS_LOCAL_OUTPUT_RESERVE`, `ARGUS_LOCAL_CHAT_TEMPLATE_OVERHEAD`,
  `ARGUS_ENGINES_LEDGER`, `ARGUS_DIFFICULTY_EXECUTORS`, `ARGUS_DSH_ENABLED`.
- Local acceptance criteria: `must_contain`, literal `equals`, `max_chars`, and a
  **restricted** `json_schema` subset (no `$ref`, `pattern`, or remote resolution).
  Legacy `must_match` (regex) is rejected with 422. Empty criteria are rejected
  with 422; answers are never `accepted` without an effective check.
- `integrations/nanoclaw/argus-single-layer.ts`: the experiment flag, assignment,
  eligibility, harness selection and arm marker, wired into `argus.ts`; its vitest
  includes a fake-API end-to-end run of both arms and the fallbacks.
- `integrations/nanoclaw/engines/`: installer for the Codex payload and the
  CLI manifest, the image steps for opencode and the Cursor agent (checksum
  pinned), `install-dsh.sh` (and its test), `argus-delegate.mjs` and its tests, `smoke.sh`, and the tools layer
  (`Dockerfile.tools-layer`, `apply-tools-layer.sh`: openssh-client, rsync and
  `kubectl` (`install-kubectl.sh`: pinned version, SHA-256 checked, probed by
  `image_tools`) on top of NanoClaw's image, applied by `provision.sh`; its build context is a
  staged copy of `engines/` plus `../gitea-api.mjs`, and CI's `tools-layer` job
  builds it on a minimal base and loads every `argus-delegate` import).
  `patch-engines-dockerfile.py` (called by `install-engines.sh`) writes the
  specialist CLIs block into NanoClaw's Dockerfile and upgrades blocks earlier
  installs left: each module gets its install step, then the COPY line lists
  every file the RUN step reads from `/tmp/argus-engines/`. Its tests run on
  real Dockerfile states (`integrations/nanoclaw/fixtures/engines-dockerfile/`).
- The dashboard combo reads `GET /api/engines` (a later increment); the
  catalogue already carries everything it needs.
