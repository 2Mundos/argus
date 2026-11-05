# Codex protocol pin: codex-cli 0.162.1

Source of truth for the Codex parsers in `overlays/harness-usage.ts` (app-server
notifications) and `engines/livelog.mjs` (`codex exec --json`). Pinned to the package
that the agent image ships (`@openai/codex@0.162.1`, native `codex-linux-x64`).

## Provenance

| Evidence | How it was obtained | Checked-in file |
| --- | --- | --- |
| App-server protocol (v2 types and notifications) | `codex app-server generate-json-schema --out <dir>` run from the pinned binary; the subset read by the parsers is copied from `codex_app_server_protocol.v2.schemas.json` | `app-server-v2.extract.json` |
| `codex exec --json` stream | One real run of the pinned binary with an empty `CODEX_HOME` (no credentials) and no network auth, so every turn was rejected with HTTP 401; the stream is the real event shape, with the OneCLI account URL replaced by `https://example.invalid/manage` | `exec-unauth.sanitized.jsonl` |
| `exec --json` item and event names not in the stream | Serde names found in the pinned binary (`strings`): `thread.started`, `turn.started`, `turn.completed`, `turn.failed`, `item.started`, `item.updated`, `item.completed`; items `agent_message`, `reasoning`, `command_execution`, `file_change`, `mcp_tool_call`, `web_search`, `todo_list`; fields `aggregated_output`, `exit_code` | binary, not checked in |

Binary: `vendor/x86_64-unknown-linux-musl/bin/codex`, sha256
`628e35487d888a28f0427fc37501da529dd3d81fcbad7a2c3421c1e8670d92e1`, under
`/pnpm/global/5/.pnpm/@openai+codex@0.162.1-linux-x64/node_modules/@openai/codex/`.

Re-derive:

```bash
/pnpm/codex app-server generate-json-schema --out /tmp/codex-schema
python3 -c "import json;d=json.load(open('/tmp/codex-schema/codex_app_server_protocol.v2.schemas.json'))['definitions'];print(sorted(d)[:5])"
```

The capture was not produced from a logged-in account, so the stream contains no
prompt, reasoning, usage, or account data. Only the unauthenticated error path is pinned
by a live run; the success path (`item.completed` with `agent_message`, `turn.completed`
with `usage`) rests on the binary's names and on the repo's existing `usage.test.mjs`
fixtures, which are marked synthetic there.

## Event names and fields the parsers read

App-server (`harnessLogLines` / `codexLogLines`), from the schema:

- `item/completed` `{threadId, turnId, completedAtMs, item}`; `item.type` in
  `agentMessage` (`text`), `commandExecution` (`command`, `aggregatedOutput`, `exitCode`,
  `status`: inProgress|completed|failed|declined), `fileChange` (`changes[].path`, `status`),
  `mcpToolCall` (`server`, `tool`, `arguments`, `error: {message}`, `status`), `dynamicToolCall`
  (`tool`, `arguments`, `success`, `status`), `webSearch` (`query`).
- `error` `{error: TurnError{message}, willRetry, threadId, turnId}`.
- `turn/completed` `{threadId, turn: {status: completed|failed|interrupted|inProgress, error: TurnError|null}}`.
- `thread/tokenUsage/updated` `{tokenUsage: {total, last: TokenUsageBreakdown}}`, with
  `inputTokens`, `cachedInputTokens`, `outputTokens`.

`exec --json` (`codexEventLines`), from the captured stream and the binary names:

- `thread.started {thread_id}`, `turn.started`, `turn.completed {usage}`, `turn.failed {error: {message}}`.
- top-level `error {message}`. The captured stream shows these carry retry notices
  (`Reconnecting... N/5 (...)`) before the final `turn.failed`; the event has no `willRetry`
  field.
- `item.completed` / `item.started` with `item.type` `error` (`message`); the captured
  stream shows a transport fallback notice (`Falling back from WebSockets to HTTPS transport`)
  in this form.
- item types as listed above; `command_execution` uses `aggregated_output`, `exit_code`,
  `status`; `file_change` uses `changes[].path`.

## Known gaps

- The success path of `exec --json` (`agent_message`, `command_execution` results,
  `turn.completed` with usage) is not in a live capture. It is pinned by the binary's
  names only. A logged-in capture would close this; it needs credentials that are not
  available to the worker.
- The `exec --json` item field names other than the stream's own (`aggregated_output`,
  `exit_code`) are taken from the binary's strings, not from a schema file.
