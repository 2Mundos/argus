/**
 * Read one stopped container's session evidence the way the NanoClaw channel does, and say
 * whether it would send `oom_kill` (failure-classes.spec.md 3.2.1). Read-only: runs
 * `docker inspect` and `docker logs` on the named container and nothing else.
 *
 *   node --experimental-strip-types scripts/oom-evidence.ts <container-name>
 *
 * ARGUS_DOCKER overrides the docker binary. See docs/runbooks/2026-10-31.oom-smoke.ref.md.
 */
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { isOomKill, readSessionEvidence } from "../integrations/nanoclaw/argus-stderr-tail.ts";

const name = process.argv[2];
if (!name) {
  console.error("usage: oom-evidence.ts <container-name>");
  process.exit(2);
}
const bin = process.env.ARGUS_DOCKER || "docker";
const run = async (args: string[]) => {
  const { stdout, stderr } = await promisify(execFile)(bin, args, { timeout: 30_000 });
  return { stdout, stderr };
};
const evidence = await readSessionEvidence(run, name);
const { logTail: _, ...state } = evidence;
console.log(JSON.stringify({ ...state, oom_kill: isOomKill(evidence, name) }));
