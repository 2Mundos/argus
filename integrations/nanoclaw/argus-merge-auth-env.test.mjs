// The merge authorization must survive the host -> session boundary: minted into the host env by
// argus-merge-auth.ts, forwarded by the install.sh-patched container-runner allowlist
// (patch-merge-auth-env.py), and verified by the CLI in the session.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHmac } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import {
  mergeAuthorizationToken,
  scopeMergeAuthorization,
  taskMayMerge,
} from "./argus-merge-auth.ts";
import { main } from "./github-api.mjs";

const NAMES = [
  "ARGUS_MERGE_AUTH_KEY",
  "ARGUS_MERGE_AUTH_TASK",
  "ARGUS_MERGE_AUTH_ATTEMPT",
  "ARGUS_MERGE_AUTHORIZED",
];
const REPO = "https://github.com/acme/widget";
const SHA = "a".repeat(40);
const open = { number: 4, state: "open", merged: false, head: { sha: SHA } };
const merged = { number: 4, state: "closed", merged: true, merge_commit_sha: "m".repeat(40) };

const task = (over = {}) => ({
  id: "t1",
  attempt: 3,
  deliverable: "pr_merged",
  required_capabilities: ["repository:write"],
  ...over,
});
/** What the channel does at the start of an attempt. */
const scope = (env, t) => scopeMergeAuthorization(env, t, taskMayMerge(t));

/** The runner's loop over the allowlist, after install.sh patched a runner that already had it. */
function forwardedBy(env) {
  const root = mkdtempSync(join(tmpdir(), "argus-merge-auth-"));
  mkdirSync(join(root, "src"));
  const runner = join(root, "src", "container-runner.ts");
  writeFileSync(
    runner,
    "  for (const key of ['ARGUS_GIT_AUTHOR_NAME', 'ARGUS_COMMAND_GUARD', 'ARGUS_FORGE_REVIEW']) {\n  }\n",
  );
  execFileSync("python3", [join(import.meta.dirname, "patch-merge-auth-env.py"), root]);
  const list = /for \(const key of \[([^\]]*)\]\)/.exec(readFileSync(runner, "utf8"))[1];
  const out = {};
  for (const [, key] of list.matchAll(/'([A-Z_]+)'/g)) if (env[key]?.trim()) out[key] = env[key];
  return out;
}

/** The real CLI verifier; reports its exit code and how many PUTs reached the forge. */
async function cliMerge(env) {
  let puts = 0;
  const fetch = async (url, init = {}) => {
    if (init.method === "PUT") puts++;
    return Response.json(init.method === "PUT" ? { merged: true } : puts ? merged : open);
  };
  const sink = { write: () => true };
  const code = await main(
    ["merge", "--number", "4", "--sha", SHA, "--repository", REPO],
    { ARGUS_REPOSITORIES_JSON: JSON.stringify({ widget: REPO }), ...env },
    { fetch, sleep: async () => {}, stdout: sink, stderr: sink },
  );
  return { code, puts };
}

describe("merge authorization reaches the session env", () => {
  it("forwards all four names for an authorized pr_merged attempt and the CLI accepts them", async () => {
    const host = {};
    scope(host, task({ auto_merge: true }));
    const session = forwardedBy(host);
    assert.deepEqual(Object.keys(session).sort(), [...NAMES].sort());
    assert.equal(
      session.ARGUS_MERGE_AUTHORIZED,
      mergeAuthorizationToken(session.ARGUS_MERGE_AUTH_KEY, "t1", 3),
    );
    assert.equal(
      session.ARGUS_MERGE_AUTHORIZED,
      createHmac("sha256", session.ARGUS_MERGE_AUTH_KEY).update("merge:t1:3").digest("hex"),
    );
    assert.deepEqual(await cliMerge(session), { code: 0, puts: 1 });
  });

  it("forwards none for pr_green, read-only and unauthorized tasks", () => {
    for (const t of [
      task({ auto_merge: true, deliverable: "pr_green" }),
      task({ auto_merge: true, required_capabilities: ["repository:read"] }),
      task(),
    ]) {
      const host = { ARGUS_MERGE_AUTHORIZED: "1" };
      scope(host, t);
      assert.deepEqual(forwardedBy(host), {});
    }
  });

  it("is per attempt: the next attempt's token differs and the previous one is refused as stale", async () => {
    const host = {};
    scope(host, task({ auto_merge: true, attempt: 1 }));
    const first = forwardedBy(host);
    scope(host, task({ auto_merge: true, attempt: 2 }));
    const second = forwardedBy(host);
    assert.notEqual(second.ARGUS_MERGE_AUTHORIZED, first.ARGUS_MERGE_AUTHORIZED);
    assert.equal(second.ARGUS_MERGE_AUTH_ATTEMPT, "2");
    assert.deepEqual(await cliMerge(second), { code: 0, puts: 1 });
    const stale = { ...second, ARGUS_MERGE_AUTHORIZED: first.ARGUS_MERGE_AUTHORIZED };
    assert.deepEqual(await cliMerge(stale), { code: 13, puts: 0 });
  });

  it("refuses a worker-set ARGUS_MERGE_AUTHORIZED without the matching key", async () => {
    assert.deepEqual(await cliMerge({ ARGUS_MERGE_AUTHORIZED: "1" }), { code: 13, puts: 0 });
    const host = {};
    scope(host, task({ auto_merge: true }));
    const { ARGUS_MERGE_AUTH_KEY: _key, ...noKey } = forwardedBy(host);
    assert.deepEqual(await cliMerge(noKey), { code: 13, puts: 0 });
    const forged = { ...noKey, ARGUS_MERGE_AUTH_KEY: "w".repeat(64) };
    assert.deepEqual(await cliMerge(forged), { code: 13, puts: 0 });
  });
});
