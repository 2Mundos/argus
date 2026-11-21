# dsh headless fixtures

`dsh-headless.jsonl` is **synthetic**, not a recording. No `dsh --profile headless --json`
run was available when it was written. The `session`, `text`, `final` (with
`reason.kind` and `text`) and `error` shapes are the ones `dshOutcome` in
`argus-delegate.mjs` already reads; the `usage` and `model` fields on `final` are a
guess (the draft's open question: does the stream carry token usage?). Re-record with the
pinned dsh (0.2.2-alpha.3) per the engine transcript replay draft, then replace this file
and drop this note. Until then the usage parser reports `not_reported` for anything else.

## Recorded: `dsh-missing-credential.jsonl`

A real run of pinned dsh 0.2.2-alpha.3 (installed with `install-dsh.sh`, exit 1) with no
`DEEPSEEK_API_KEY`: `dsh --profile headless --json "say hi"`. The `.stderr.txt` beside it is the
same run's stderr. It shows two shapes the synthetic fixture lacks: `status` events
(`turn_start`, `step_start`, `step_end`, `turn_end`) and a `final` that carries
`reason.error.{message,code}` with no `usage`. No successful run has been recorded (no provider
key was available), so the usage fields on `dsh-headless.jsonl` remain unverified.
