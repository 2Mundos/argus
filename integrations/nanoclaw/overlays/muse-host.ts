/**
 * Host side of the Muse harness (muse accounts spec 3). Installed by
 * engines/install-engines.sh as src/providers/muse.ts.
 *
 * A per-group state directory keeps Muse sessions across container respawns, so a
 * stored continuation can be loaded. No credential passes through here: the gateway
 * sentinel comes from the OneCLI hook (argus-muse-stub.ts).
 */
import fs from "fs";
import path from "path";

import { DATA_DIR } from "../config.js";
import { registerProviderContainerConfig } from "./provider-container-registry.js";

registerProviderContainerConfig("muse", (ctx) => {
  const hostPath = path.join(DATA_DIR, "v2-sessions", ctx.agentGroupId, ".muse-shared");
  fs.mkdirSync(hostPath, { recursive: true });
  return { mounts: [{ hostPath, containerPath: "/home/node/.muse-data", readonly: false }] };
});
