# Engine transcript recordings

Real engine output, recorded once and replayed in tests without Docker, a model
or the network. Design: [engine transcript replay](../../../../docs/drafts/2026-11-04.engine-transcript-replay.draft.md).

**Status:** seven cases are committed:

| Case | Recording |
| --- | --- |
| `claude/success-stream` | A full `claude -p` stream-json run, Claude Code 2.1.296. |
| `claude/success-result` | A lone result event copied from `engines/fixtures/claude-result.json`. |
| `claude/failure-auth` | Invalid key, 401, from a disposable run of the candidate image. |
| `claude/failure-network` | No network: `api_retry` events until the timeout, no terminal event. A connection failure, not a mid-stream drop. |
| `claude/failure-oom` | Container memory-cap kill at startup: no events, so no `events.jsonl`, only `stderr.txt`. Not an in-session OOM of a long run. |
| `codex/failure-unauth` | Real pinned codex-cli 0.162.1 unauthenticated 401 capture. |
| `cursor/success-stream` | Cursor agent CLI stream-json, success. |

The three Claude failures come from disposable runs of the candidate image, not Argus
task attempts. `transcripts.test.ts` replays every committed case. A case without
`events.jsonl` derives from zero events and must have `stderr.txt`.

**No Copilot recordings are committed.** `copilot/success` and `copilot/auth-rejected`
are listed as not recorded in `transcripts.test.ts`, which fails if either directory
appears without being removed from that list. The Copilot route is implemented but
gated: `integrations/nanoclaw/argus-copilot-stub.ts` gives the worker its sentinel token
and injects the Copilot endpoints only when every Copilot generic secret is assigned to
the agent group, and `argus-delegate copilot` otherwise exits 78 (`auth_missing`).
Whether an account is assigned in any given deployment is not verified by this
repository. The only Copilot evidence here is `fixtures/copilot-cli/probe-1.0.105.json`,
a probe of CLI 1.0.105 failure signals, not a transcript. Obtaining real Copilot
recordings is tracked by task cfc28599.

Still to record: Claude session limit, Codex usage limit and success, Copilot success
and auth-rejected (see above), and the custom and local bridge errors (OpenRouter
402/429, Strata unavailable): no raw evidence exists on the worker.

## Layout

```
transcripts/<engine>/<case>/
  events.jsonl   required, except a case where the engine wrote nothing (failure-oom)
  stderr.txt     only when the case depends on stderr
  expected.json  required
  source.md      required
```

| File | Holds |
| --- | --- |
| `events.jsonl` | The engine's raw output, one event or line per row. |
| `stderr.txt` | The engine's stderr, only when the case depends on it. |
| `expected.json` | The values `transcripts-derive.ts` must derive from the recording, compared field by field, with no extra or missing keys. See [What is replayed](#what-is-replayed). |
| `source.md` | One line naming the task or incident and the engine version. |

## Engines

`<engine>` is one of `claude`, `codex`, `copilot`, `cursor`. Custom and local
run Claude Code, so their recordings go under `claude/`.

## What is replayed

The replay tests compare the keys in each `expected.json` with what the deriver
returns from the recording, and fail on any extra or missing key. The keys below are
the ones the deriver in `transcripts-derive.ts` actually produces.

| Engine | Derived keys (committed cases) |
| --- | --- |
| `claude` | `result.is_error`, `result.subtype`, `result.terminal_reason`, `rateLimitError`, `rateLimitFailure`, `authExpiredError`, `authExpiredFailure` |
| `codex` | `turnFailed`, `codexUsageLimitFailure`, `usage` (from `turn.completed`) |
| `cursor` | `result.is_error`, `result.subtype`, `usageRecordPresent`, `usage` (token counts, `cost_usd`, `model`, `turns`) |
| `copilot` | `authRejected`, `usage`. Replayed from `stdout.txt` only if a case is recorded; none is. |

Not replayed:

- **Stall cause.** `transcripts-derive.ts` has no stall output, and no `expected.json`
  has a stall key. `claude/failure-network` contains the `api_retry` events, but its
  expected values are only an empty `result` and null failure fields; the retries and
  the timeout are not asserted. `argus-stderr-tail.ts` is not replayed either.
- **Final output presence** and a `providerFailure` key: neither exists in the deriver
  or in any `expected.json`. The failure fields are named per provider, as above.
- **Usage for Claude**: the Claude expected files have no `usage` key, and `codex`
  asserts `usage: null` only for the unauthenticated 401 case.
- **`stderr.txt` contents**: the tests check only that it is at most 16 KiB and free of
  home paths and email addresses. They do not parse it.

## Recording

Recordings are made by `scripts/record_transcript.py`, never written by hand:
they must come from real engine output, not hand-written strings.

- **Size cap:** 200 KB per case (`events.jsonl` + `stderr.txt` + `expected.json`
  + `source.md`).
- **Trimming:** the recorder trims strings over 2000 characters, then 500, then
  120 if the case is still too large, and keeps the event structure.
- **Redaction:** credentials, emails and home paths are masked; pass `--redact`
  for repository names and anything else.
- **Stderr:** `--stderr PATH` takes the stderr tail (the last 16 KiB, redacted
  like the events). `stderr.txt` is written only when that input is authentic and
  non-empty; a re-record without it removes a stale one. The three Claude failure cases carry the
  `.err` files of their disposable runs (`exit=<code>`, `Killed`); `failure-oom` has nothing else.
- **expected.json** is drafted by `transcripts-derive.ts` (needs `bun`).
- **Review:** a person checks the redaction and the expected values before
  committing.

## Rule

An engine version bump re-records at least the success case and one failure case
for that engine in the same PR. `make check-engine-transcripts` (part of `make
lint`) enforces it: it reads each engine's pin, and when one differs from the
merge base with `origin/main` it requires an added or changed `success-*` case
and `failure-*` case under `transcripts/<engine>/`. Name cases by type, for
example `success-result` and `failure-auth`.
