import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ContainerConfig } from "@onecli-sh/sdk";
import { describe, expect, it } from "vitest";
import {
  augmentCopilotContainerConfig,
  COPILOT_HOME_CONTAINER_DIR,
  COPILOT_INJECTED,
  COPILOT_SENTINEL_TOKEN,
  copilotExecutorAssigned,
  withCopilotDelegateRuntime,
} from "./argus-copilot-stub.js";

// Fake only: stands in for a real token that must never reach the container.
const FAKE_REAL_TOKEN = "github_pat_FAKE_REAL_VALUE_NEVER_IN_CONTAINER";

const baseConfig = () =>
  JSON.parse(
    readFileSync(
      join(
        dirname(fileURLToPath(import.meta.url)),
        "fixtures/cursor-gateway/container-config-before-augment.json",
      ),
      "utf8",
    ),
  ) as ContainerConfig;

/** A OneCLI API answering metadata only, as the real one lists secrets (no values). */
function onecli(assignedIds: string[], secrets: Array<Record<string, string>>): typeof fetch {
  return (async (url: string) => {
    const path = new URL(url).pathname;
    if (path === "/api/agents") return Response.json([{ id: "ag-1", identifier: "g" }]);
    if (path === "/api/agents/ag-1/secrets") return Response.json(assignedIds);
    if (path === "/api/secrets") return Response.json(secrets);
    return new Response("not found", { status: 404 });
  }) as typeof fetch;
}

const copilotSecrets = COPILOT_INJECTED.map(([hostPattern, pathPattern], i) => ({
  id: `c${i}`,
  name: `argus-g-copilot-${i}`,
  type: "generic",
  hostPattern,
  pathPattern,
}));
const repositorySecret = {
  id: "r",
  name: "argus-g-repository-rest",
  type: "generic",
  hostPattern: "api.github.com",
  pathPattern: "/repos/acme/widget/*",
};
const allIds = copilotSecrets.map((s) => s.id);

describe("copilotExecutorAssigned", () => {
  const assigned = (ids: string[], secrets = [...copilotSecrets, repositorySecret]) =>
    copilotExecutorAssigned("http://onecli.test", undefined, "g", onecli(ids, secrets));

  it("is true only when every Copilot endpoint is assigned", async () => {
    expect(await assigned(allIds)).toBe(true);
    expect(await assigned([...allIds, "r"])).toBe(true);
  });

  it("is false for an absent or stale lease", async () => {
    expect(await assigned([])).toBe(false); // released: secrets kept, none assigned
    expect(await assigned(["r"])).toBe(false); // a repository lease is not Copilot
    expect(await assigned(allIds.slice(1))).toBe(false); // partial
    const widened = copilotSecrets.map((s) =>
      s.pathPattern === "/copilot_internal/user" ? { ...s, pathPattern: "/copilot_internal/*" } : s,
    );
    expect(await assigned(allIds, widened)).toBe(false); // an older or foreign shape
    expect(
      await copilotExecutorAssigned("http://onecli.test", undefined, "other", onecli(allIds, [])),
    ).toBe(false);
  });

  it("is false when OneCLI does not answer", async () => {
    const down = (async () => {
      throw new Error("ECONNREFUSED");
    }) as typeof fetch;
    expect(await copilotExecutorAssigned("http://onecli.test", undefined, "g", down)).toBe(false);
  });
});

describe("augmentCopilotContainerConfig", () => {
  it("gives the sentinel and a container-local home when assigned", () => {
    const out = augmentCopilotContainerConfig(baseConfig(), true);
    expect(out.env?.COPILOT_GITHUB_TOKEN).toBe(COPILOT_SENTINEL_TOKEN);
    expect(out.env?.COPILOT_HOME).toBe(COPILOT_HOME_CONTAINER_DIR);
    expect(COPILOT_SENTINEL_TOKEN.startsWith("github_pat_")).toBe(true);
    expect(COPILOT_SENTINEL_TOKEN).toContain("not_a_credential");
  });

  it("never passes another Copilot token into the container", () => {
    const config = {
      ...baseConfig(),
      env: { ...baseConfig().env, COPILOT_GITHUB_TOKEN: FAKE_REAL_TOKEN },
    };
    for (const assigned of [true, false]) {
      const out = augmentCopilotContainerConfig(config, assigned);
      expect(JSON.stringify(out)).not.toContain(FAKE_REAL_TOKEN);
    }
    expect(augmentCopilotContainerConfig(config, false).env?.COPILOT_GITHUB_TOKEN).toBeUndefined();
  });

  it("leaves the rest of the recorded config unchanged", async () => {
    const before = baseConfig();
    const after = await withCopilotDelegateRuntime(before, async () => false);
    expect(after).toEqual(before);
    const on = await withCopilotDelegateRuntime(before, async () => true);
    expect(on.env?.HTTPS_PROXY).toBe(before.env?.HTTPS_PROXY);
    expect(on.credentialStubs).toEqual(before.credentialStubs);
  });
});
