/**
 * Create (or reuse) a NanoClaw agent group wired to the Argus channel.
 *
 * Usage (from the NanoClaw checkout):
 *   pnpm exec tsx scripts/init-argus-agent.ts --agent-name "Worker" [--folder argus-worker]
 *
 * The channel itself reads ARGUS_URL and a bootstrap or agent token from the
 * environment of the NanoClaw host process.
 */
import "../src/channels/index.js";
import {
  resolveUnknownSenderPolicy,
  resolveWiringDefaults,
} from "../src/channels/channel-defaults.js";
import { CENTRAL_DB_PATH } from "../src/config.js";
import { createAgentGroup, getAgentGroupByFolder } from "../src/db/agent-groups.js";
import { initDb } from "../src/db/connection.js";
import {
  createMessagingGroup,
  createMessagingGroupAgent,
  getMessagingGroupAgentByPair,
  getMessagingGroupByPlatform,
} from "../src/db/messaging-groups.js";
import { runMigrations } from "../src/db/migrations/index.js";
import { initGroupFilesystem } from "../src/group-init.js";
import { upsertUser } from "../src/modules/permissions/db/users.js";
import type { AgentGroup, MessagingGroup } from "../src/types.js";

const CHANNEL = "argus";
const PLATFORM_ID = "local";

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

const id = (prefix: string) => `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

async function main(): Promise<void> {
  const agentName = arg("--agent-name")?.trim() || "Argus Worker";
  const folder =
    arg("--folder")?.trim() || `argus-${agentName.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
  const db = await initDb(CENTRAL_DB_PATH);
  await runMigrations(db);
  const now = new Date().toISOString();

  await upsertUser({
    id: `${CHANNEL}:${PLATFORM_ID}`,
    kind: CHANNEL,
    display_name: "Argus",
    created_at: now,
  });

  let ag: AgentGroup | undefined = await getAgentGroupByFolder(folder);
  if (!ag) {
    await createAgentGroup({
      id: id("ag"),
      name: agentName,
      folder,
      agent_provider: null,
      created_at: now,
    });
    ag = (await getAgentGroupByFolder(folder))!;
    console.log(`Created agent group: ${ag.id} (${folder})`);
  } else {
    console.log(`Reusing agent group: ${ag.id} (${folder})`);
  }
  await initGroupFilesystem(ag, {
    instructions:
      `# ${agentName}\n\n` +
      `You are ${agentName}, a worker in an Argus fleet. Tasks arrive as messages starting with "[Argus task]". ` +
      'Do the work, send brief progress updates, and finish with a message that starts with "COMPLETE:" and a summary ' +
      '(or "FAIL:" with the reason, or "ASK:" with a question you need answered). Keep messages concise.',
    provider: process.env.NANOCLAW_PICKED_PROVIDER?.trim().toLowerCase(),
  });

  let mg: MessagingGroup | undefined = await getMessagingGroupByPlatform(CHANNEL, PLATFORM_ID);
  if (!mg) {
    mg = {
      id: id("mg"),
      channel_type: CHANNEL,
      platform_id: PLATFORM_ID,
      name: "Argus",
      is_group: 0,
      unknown_sender_policy: resolveUnknownSenderPolicy(CHANNEL, false),
      created_at: now,
    };
    await createMessagingGroup(mg);
    console.log(`Created Argus messaging group: ${mg.id}`);
  }

  if (!(await getMessagingGroupAgentByPair(mg.id, ag.id))) {
    const engage = resolveWiringDefaults(CHANNEL, false, ag.name);
    await createMessagingGroupAgent({
      id: id("mga"),
      messaging_group_id: mg.id,
      agent_group_id: ag.id,
      engage_mode: engage.engage_mode,
      engage_pattern: engage.engage_pattern,
      sender_scope: "all",
      ignored_message_policy: "drop",
      session_mode: "shared",
      priority: 0,
      created_at: now,
    });
    console.log(`Wired ${CHANNEL}: ${mg.id} -> ${ag.id}`);
  }
  console.log(`\nDone. Start the host with ARGUS_URL and a bootstrap token set.`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
