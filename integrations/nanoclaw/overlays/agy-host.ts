/**
 * Host side of the Google Antigravity (`agy`) harness (google accounts spec 3).
 * Installed by engines/install-engines.sh as src/providers/agy.ts.
 *
 * A per-group state directory keeps Antigravity sessions across container respawns
 * (`--gemini-dir /home/node/.agy-data`), so a stored continuation can be loaded while
 * `/home/node` stays untouched for git, ssh and other tools. No credential passes
 * through here: the gateway sentinel or read-only OAuth stub comes from the OneCLI
 * hook (argus-agy-stub.ts).
 */
import fs from "fs";
import path from "path";

import { DATA_DIR } from "../config.js";
import { registerProviderContainerConfig } from "./provider-container-registry.js";

registerProviderContainerConfig("agy", (ctx) => {
  const hostPath = path.join(DATA_DIR, "v2-sessions", ctx.agentGroupId, ".agy-shared");
  fs.mkdirSync(hostPath, { recursive: true });
  return { mounts: [{ hostPath, containerPath: "/home/node/.agy-data", readonly: false }] };
});
