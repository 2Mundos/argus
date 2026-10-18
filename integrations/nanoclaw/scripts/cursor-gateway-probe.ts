/**
 * Live probe: OneCLI container-config → Cursor assignment metadata → augment stubs.
 * Optional native CLI step when ARGUS_CURSOR_PROBE_AGENT=1 (scratch stack + agent image).
 *
 *   ONECLI_URL=http://<scratch-onecli-ip>:10254 pnpm exec tsx integrations/nanoclaw/scripts/cursor-gateway-probe.ts
 */
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  CURSOR_AUTH_CONTAINER_PATH,
  CURSOR_SENTINEL_ACCESS_TOKEN,
  cursorExecutorAssigned,
  withCursorDelegateRuntime,
} from "../argus-cursor-stub.js";

const here = dirname(fileURLToPath(import.meta.url));
const agentId = process.env.CURSOR_PROBE_AGENT_ID ?? "scratch-agent";
const onecliUrl = process.env.ONECLI_URL ?? "http://127.0.0.1:10254";

async function main(): Promise<void> {
  const raw = await fetch(`${onecliUrl.replace(/\/$/, "")}/v1/container-config?agent=${agentId}`);
  if (!raw.ok) throw new Error(`container-config ${raw.status}`);
  const before = (await raw.json()) as import("@onecli-sh/sdk").ContainerConfig;
  const assigned = await cursorExecutorAssigned(onecliUrl, process.env.ONECLI_API_KEY, agentId);
  const after = await withCursorDelegateRuntime(before, async () => assigned);
  const auth = after.credentialStubs?.find((s) => s.containerPath === CURSOR_AUTH_CONTAINER_PATH);
  const summary = {
    onecliUrl,
    agentId,
    assigned,
    envKeys: Object.keys(after.env ?? {}).sort(),
    forbiddenCursorEnv: Object.keys(after.env ?? {}).filter((k) =>
      /^(CURSOR_API_KEY|CURSOR_AUTH_TOKEN)$/.test(k),
    ),
    stubPaths: (after.credentialStubs ?? []).map((s) => s.containerPath),
    sentinelAccessTokenInStub: auth
      ? JSON.parse(auth.content).accessToken === CURSOR_SENTINEL_ACCESS_TOKEN
      : false,
  };
  console.log(JSON.stringify(summary, null, 2));
  if (!assigned) {
    console.error("expected Cursor secrets assigned for probe agent");
    process.exit(1);
  }
  if (summary.forbiddenCursorEnv.length > 0 || !summary.sentinelAccessTokenInStub) {
    console.error("augment misconfigured: must use auth.json stub only (no CURSOR_API_KEY)");
    process.exit(1);
  }
  if (process.env.ARGUS_CURSOR_PROBE_SKIP_NATIVE === "1") return;

  const afterPath = join(mkdtempSync(join(tmpdir(), "argus-cursor-probe-")), "after-augment.json");
  writeFileSync(afterPath, JSON.stringify(after));
  const smoke = spawnSync(join(here, "cursor-gateway-native-smoke.sh"), [afterPath], {
    stdio: "inherit",
    env: process.env,
  });
  if (smoke.status !== 0) process.exit(smoke.status ?? 1);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
