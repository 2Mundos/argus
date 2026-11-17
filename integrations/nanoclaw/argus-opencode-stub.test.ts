import type { ContainerConfig } from "@onecli-sh/sdk";
import { describe, expect, it } from "vitest";
import { opencodeExecutorAssigned, withOpencodeDelegateRuntime } from "./argus-opencode-stub.js";
import { OPENCODE_GATEWAY_PATHS, OPENCODE_GATEWAY_SENTINEL } from "./engines/opencode-gateway.mjs";

const config: ContainerConfig = {
  env: { ARGUS_ACCOUNT_API_KEY: "dummy-never-copy" },
  caCertificate: "test-ca",
  caCertificateContainerPath: "/test-ca.pem",
};
const secrets = OPENCODE_GATEWAY_PATHS.map((pathPattern, i) => ({
  id: `s${i}`,
  name: `argus-g-custom-executor-${i}`,
  type: "generic",
  hostPattern: "openrouter.ai",
  pathPattern,
}));
const lookup = (rows = secrets, ids = ["s0", "s1"], mode = "selective") => {
  const fake = (async (url: string) =>
    new Response(
      JSON.stringify(
        url.endsWith("/agents")
          ? [{ id: "a", identifier: "g", secretMode: mode }]
          : url.endsWith("/a/secrets")
            ? ids
            : rows,
      ),
    )) as typeof fetch;
  return opencodeExecutorAssigned("http://localhost:10254", undefined, "g", fake);
};

describe("OpenRouter executor sentinel", () => {
  it("requires every exact named selective assignment and never a harness bridge", async () => {
    expect(await lookup()).toBe(true);
    expect(await lookup(secrets, ["s0"])).toBe(false);
    expect(await lookup(secrets, ["s0", "s1"], "all")).toBe(false);
    expect(await lookup(secrets.map((s) => ({ ...s, hostPattern: "evil.example" })))).toBe(false);
    expect(await lookup(secrets.map((s) => ({ ...s, pathPattern: "/api/v1/*" })))).toBe(false);
    expect(await lookup(secrets.map((s) => ({ ...s, name: "argus-g-custom" })))).toBe(false);
    expect(
      await opencodeExecutorAssigned("http://localhost", undefined, "g", async () => {
        throw new Error("unavailable");
      }),
    ).toBe(false);
  });
  it("scrubs ambient credentials, sets only a sentinel, and clears it after release", async () => {
    const applied = await withOpencodeDelegateRuntime(config, () => lookup());
    expect(applied.env.ARGUS_ACCOUNT_API_KEY).toBe(OPENCODE_GATEWAY_SENTINEL);
    expect(JSON.stringify(applied)).not.toContain("dummy-never-copy");
    expect(JSON.parse(applied.env.ARGUS_ACCOUNT_SETTINGS)).toMatchObject({
      base_url: "https://openrouter.ai/api/v1",
      model: null,
    });
    const released = await withOpencodeDelegateRuntime(applied, async () => false);
    expect(released.env.ARGUS_ACCOUNT_API_KEY).toBeUndefined();
    expect(released.env.ARGUS_ACCOUNT_SETTINGS).toBeUndefined();
    const local = await withOpencodeDelegateRuntime(
      {
        ...config,
        env: {
          ARGUS_LOCAL_HARNESS: "1",
          ARGUS_ACCOUNT_API_KEY: "local-placeholder",
        },
      },
      async () => false,
    );
    expect(local.env.ARGUS_ACCOUNT_API_KEY).toBe("local-placeholder");
  });
});
