// Quota resume spike (docs/research/2026-11-02.quota-resume-spike.research.md): one
// turn of the pinned NanoClaw agent runner, outside a container. quota-resume-spike.sh
// copies this file into a scratch copy of container/agent-runner/src whose two mailbox
// paths it has pointed at SPIKE_INBOUND_DB / SPIKE_OUTBOUND_DB, and runs it with bun.
//
//   bun spike-runner.ts prepare <session dir>      # the host's mailbox schema, empty
//   bun spike-runner.ts turn "<text>"              # one inbound message, one turn
//
// `turn` runs the runner's own poll loop and Claude provider until the turn has
// finished the message (acknowledged it completed or failed), then prints only the
// continuation it left and that status.
import { Database } from "bun:sqlite";
import "./modules/index.js";
import "./providers/index.js";
import "./provider-contracts/index.js";
import { getAgentMailbox } from "./mailbox/index.js";
import { runPollLoop } from "./poll-loop.js";
import { registerProviderMemorySessionHook } from "./provider-contracts/realize.js";
import { createProvider } from "./providers/factory.js";
import { getProviderRuntimeContract } from "./providers/provider-registry.js";

const [mode, arg] = process.argv.slice(2);
const inbound = process.env.SPIKE_INBOUND_DB ?? "";
const outbound = process.env.SPIKE_OUTBOUND_DB ?? "";
if (!inbound || !outbound) throw new Error("SPIKE_INBOUND_DB and SPIKE_OUTBOUND_DB are required");

if (mode === "prepare") {
  const { INBOUND_SCHEMA, OUTBOUND_SCHEMA } = await import(process.env.SPIKE_HOST_SCHEMA ?? "");
  const inDb = new Database(inbound);
  inDb.exec("PRAGMA journal_mode = DELETE");
  inDb.exec(INBOUND_SCHEMA);
  inDb.close();
  const outDb = new Database(outbound);
  outDb.exec("PRAGMA journal_mode = DELETE");
  outDb.exec(OUTBOUND_SCHEMA);
  outDb.close();
  process.exit(0);
}
if (mode !== "turn" || !arg) throw new Error("usage: prepare | turn <text>");

const inDb = new Database(inbound);
inDb
  .prepare(
    "INSERT INTO messages_in (id, seq, kind, timestamp, status, trigger, platform_id, channel_type, thread_id, content) VALUES (?, 2, 'chat', ?, 'pending', 1, 'spike', 'spike', 'spike', ?)",
  )
  .run(
    `in-${Date.now()}`,
    new Date().toISOString(),
    JSON.stringify({ sender: "spike", text: arg }),
  );
inDb.close();

const mailbox = getAgentMailbox();
await mailbox.start(null);
const provider = createProvider("claude", { mcpServers: {}, env: { ...process.env } });
// The image's memory hook script is not here; a no-op command keeps the provider's
// own registration path without it.
registerProviderMemorySessionHook("claude", provider, {
  command: "true",
  legacyCommands: [],
  sources: ["startup", "clear", "compact"],
});

const settled = () => {
  const db = new Database(outbound, { readonly: true });
  try {
    const row = db
      .prepare("SELECT status FROM processing_ack WHERE status IN ('completed', 'failed') LIMIT 1")
      .get() as { status: string } | undefined;
    return row?.status ?? null;
  } finally {
    db.close();
  }
};
const stop = new AbortController();
const deadline = Date.now() + 120_000;
const watch = setInterval(() => {
  if (settled() || Date.now() > deadline) stop.abort();
}, 200);
await runPollLoop({
  provider,
  providerContract: getProviderRuntimeContract("claude"),
  providerName: "claude",
  cwd: process.env.SPIKE_CWD ?? process.cwd(),
  systemContext: { instructions: "Quota resume spike." },
  signal: stop.signal,
});
clearInterval(watch);
await mailbox.stop();

const db = new Database(outbound, { readonly: true });
const row = db.prepare("SELECT value FROM session_state WHERE key = 'continuation:claude'").get() as
  | { value: string }
  | undefined;
db.close();
console.log(JSON.stringify({ continuation: row?.value ?? null, message: settled() ?? "timeout" }));
process.exit(0);
