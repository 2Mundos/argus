import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ContainerConfig } from "@onecli-sh/sdk";
import { describe, expect, it } from "vitest";
import {
  AGY_GEMINI_HOST,
  AGY_GEMINI_PATH,
  AGY_OAUTH_CONTAINER_PATH,
  AGY_OAUTH_INJECTED,
  AGY_SENTINEL_API_KEY,
  AGY_SENTINEL_OAUTH_TOKEN,
  agyExecutorAssigned,
  augmentAgyContainerConfig,
  withAgyDelegateRuntime,
} from "./argus-agy-stub.js";

const FAKE_REAL_KEY = "AIzaSyFAKE_REAL_KEY_NEVER_IN_CONTAINER";

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

function onecli(assignedIds: string[], secrets: Array<Record<string, string>>): typeof fetch {
  return (async (url: string) => {
    const path = new URL(url).pathname;
    if (path === "/api/agents") return Response.json([{ id: "ag-1", identifier: "g" }]);
    if (path === "/api/agents/ag-1/secrets") return Response.json(assignedIds);
    if (path === "/api/secrets") return Response.json(secrets);
    return new Response("not found", { status: 404 });
  }) as typeof fetch;
}

const geminiSecret = {
  id: "g-api",
  name: "argus-g-google-gemini",
  type: "generic",
  hostPattern: AGY_GEMINI_HOST,
  pathPattern: AGY_GEMINI_PATH,
};

const oauthSecrets = AGY_OAUTH_INJECTED.map(([hostPattern, pathPattern], i) => ({
  id: `g-oauth-${i}`,
  name: `argus-g-google-oauth-${i}`,
  type: "generic",
  hostPattern,
  pathPattern,
}));

describe("agyExecutorAssigned", () => {
  it("returns api_key when the Gemini API generic secret is assigned", async () => {
    const mode = await agyExecutorAssigned(
      "http://onecli.test",
      undefined,
      "g",
      onecli(["g-api"], [geminiSecret, ...oauthSecrets]),
    );
    expect(mode).toBe("api_key");
  });

  it("returns oauth only when every Google OAuth endpoint is assigned", async () => {
    const allOauth = oauthSecrets.map((s) => s.id);
    expect(
      await agyExecutorAssigned(
        "http://onecli.test",
        undefined,
        "g",
        onecli(allOauth, [geminiSecret, ...oauthSecrets]),
      ),
    ).toBe("oauth");
    expect(
      await agyExecutorAssigned(
        "http://onecli.test",
        undefined,
        "g",
        onecli(allOauth.slice(1), oauthSecrets),
      ),
    ).toBeNull();
  });

  it("returns null when unassigned or OneCLI is unreachable", async () => {
    expect(
      await agyExecutorAssigned("http://onecli.test", undefined, "g", onecli([], [geminiSecret])),
    ).toBeNull();
    const down = (async () => {
      throw new Error("ECONNREFUSED");
    }) as typeof fetch;
    expect(await agyExecutorAssigned("http://onecli.test", undefined, "g", down)).toBeNull();
  });
});

describe("augmentAgyContainerConfig", () => {
  it("sets only GEMINI_API_KEY sentinel in api_key mode and strips OAuth stubs", () => {
    const withStaleOauth = augmentAgyContainerConfig(baseConfig(), "oauth");
    const out = augmentAgyContainerConfig(withStaleOauth, "api_key");
    expect(out.env?.GEMINI_API_KEY).toBe(AGY_SENTINEL_API_KEY);
    expect(out.env?.ARGUS_AGY_OAUTH_STUB).toBeUndefined();
    expect(
      (out.credentialStubs ?? []).some((s) => s.containerPath === AGY_OAUTH_CONTAINER_PATH),
    ).toBe(false);
  });

  it("mounts far-future sentinel OAuth stub in oauth mode and scrubs GEMINI_API_KEY", () => {
    const config = {
      ...baseConfig(),
      env: { ...baseConfig().env, GEMINI_API_KEY: FAKE_REAL_KEY },
    };
    const out = augmentAgyContainerConfig(config, "oauth");
    expect(out.env?.GEMINI_API_KEY).toBeUndefined();
    expect(out.env?.ARGUS_AGY_OAUTH_STUB).toBe(AGY_OAUTH_CONTAINER_PATH);
    expect(JSON.stringify(out)).not.toContain(FAKE_REAL_KEY);
    const stub = (out.credentialStubs ?? []).find(
      (s) => s.containerPath === AGY_OAUTH_CONTAINER_PATH,
    );
    expect(stub).toBeDefined();
    const parsed = JSON.parse(stub!.content);
    expect(parsed.token.access_token).toBe(AGY_SENTINEL_OAUTH_TOKEN);
    expect(parsed.token.expiry).toBe("2099-01-01T00:00:00Z");
    expect(parsed.auth_method).toBe("consumer");
  });

  it("strips both sentinels when unassigned", async () => {
    const seeded = augmentAgyContainerConfig(
      { ...baseConfig(), env: { ...baseConfig().env, GEMINI_API_KEY: FAKE_REAL_KEY } },
      "oauth",
    );
    const cleared = await withAgyDelegateRuntime(seeded, async () => null);
    expect(cleared.env?.GEMINI_API_KEY).toBeUndefined();
    expect(cleared.env?.ARGUS_AGY_OAUTH_STUB).toBeUndefined();
    expect(
      (cleared.credentialStubs ?? []).some((s) => s.containerPath === AGY_OAUTH_CONTAINER_PATH),
    ).toBe(false);
  });
});
