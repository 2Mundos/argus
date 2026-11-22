# dsh headless fixtures

`dsh-headless.jsonl` is a **recording** of pinned dsh 0.2.2-alpha.3, byte for byte: a
successful `dsh --profile headless --patch overlay.yaml --json -` run (exit 0, empty stderr)
in the `ARGUS_DSH_ENABLED=true` agent image as uid 1000, prompt "Reply with exactly the word
OK and nothing else." The model was `deepseek/deepseek-v4.1-flash-1017` **via OpenRouter**
(overlay shape of `dshOverlay()`), not DeepSeek's own API. Recorded 2026-11-22 on the
orchestrator host.

What it shows: events `session`, `status` (`turn_start`, `step_start`, `step_end`,
`turn_end`), `text`, `final`. Token usage is on the `step_end` status event
(`usage.inputTokens/outputTokens/totalTokens`), not on `final`. No model name or cost is in the
stream, so the usage parser reports those as null.

## Recorded: `dsh-missing-credential.jsonl`

A real run of the same dsh version (exit 1) with no `DEEPSEEK_API_KEY`:
`dsh --profile headless --json "say hi"`. The `.stderr.txt` beside it is that run's stderr. Its
`step_end` has no `usage` and its `final` carries `reason.error.{message,code}`, so usage is
`not_reported`.
