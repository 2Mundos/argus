# dsh headless fixtures

`dsh-headless.jsonl` is **synthetic**, not a recording. No `dsh --profile headless --json`
run was available when it was written. The `session`, `text`, `final` (with
`reason.kind` and `text`) and `error` shapes are the ones `dshOutcome` in
`argus-delegate.mjs` already reads; the `usage` and `model` fields on `final` are a
guess (the draft's open question: does the stream carry token usage?). Re-record with the
pinned dsh (0.2.2-alpha.3) per the engine transcript replay draft, then replace this file
and drop this note. Until then the usage parser reports `not_reported` for anything else.
