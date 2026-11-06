# Engine transcript recordings

Real engine output, recorded once and replayed in tests without Docker, a model
or the network. Design: [engine transcript replay](../../../../docs/drafts/2026-11-04.engine-transcript-replay.draft.md).

**Status:** two Claude recordings are committed: `claude/success-stream` (a full
`claude -p` stream-json run, Claude Code 2.1.296) and `claude/success-result` (a lone
result event). `transcripts.test.ts` replays both. Session-limit and auth-failure
cases are still to record.
Custom and local bridge-error cases (OpenRouter 402/429, Strata unavailable) are
also not recorded yet: no raw evidence exists on the worker.

## Layout

```
transcripts/<engine>/<case>/
  events.jsonl   required
  stderr.txt     only when the case depends on stderr
  expected.json  required
  source.md      required
```

| File | Holds |
| --- | --- |
| `events.jsonl` | The engine's raw output, one event or line per row. |
| `stderr.txt` | The engine's stderr, only when the case depends on it. |
| `expected.json` | What Argus must derive from the recording: failure kind, `providerFailure`, usage totals, stall cause, final output presence. Compared field by field. |
| `source.md` | One line naming the task or incident and the engine version. |

## Engines

`<engine>` is one of `claude`, `codex`, `copilot`, `cursor`. Custom and local
run Claude Code, so their recordings go under `claude/`.

## Recording

Recordings are made by `scripts/record_transcript.py`, never written by hand:
they must come from real engine output, not hand-written strings.

- **Size cap:** 200 KB per case (`events.jsonl` + `stderr.txt` + `expected.json`
  + `source.md`).
- **Trimming:** the recorder trims strings over 2000 characters, then 500, then
  120 if the case is still too large, and keeps the event structure.
- **Redaction:** credentials, emails and home paths are masked; pass `--redact`
  for repository names and anything else.
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
