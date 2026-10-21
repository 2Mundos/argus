import { describe, expect, it } from "vitest";
import { CLAUDE_GATEWAY_STUB_PATH } from "./argus-claude-stub.js";
import {
  CUSTOM_HARNESS_AUTH_TOKEN,
  customHarnessEnv,
  customHarnessFromLease,
  withCustomHarnessRuntime,
} from "./argus-custom-harness.js";

describe("customHarnessFromLease", () => {
  it("builds container harness config from lease settings", () => {
    const h = customHarnessFromLease("http://host.docker.internal:9123", {
      base_url: "https://openrouter.ai/api/v1",
      label: "openrouter",
      model: "deepseek/deepseek-v4.1-flash-1017",
      provider: null,
    });
    expect(h).toMatchObject({
      anthropic_base_url: "http://host.docker.internal:9123",
      model: "deepseek/deepseek-v4.1-flash-1017",
    });
  });
});

describe("withCustomHarnessRuntime", () => {
  it("sets redirect env and drops cloud credentials", () => {
    const out = withCustomHarnessRuntime(
      {
        env: { CLAUDE_CODE_OAUTH_TOKEN: "sentinel", ANTHROPIC_API_KEY: "x" },
        credentialStubs: [{ containerPath: CLAUDE_GATEWAY_STUB_PATH, content: "{}" }],
      } as never,
      customHarnessFromLease("http://host.docker.internal:9", {
        base_url: "https://openrouter.ai/api/v1",
        label: "openrouter",
        model: "m",
        provider: null,
      })!,
    );
    expect(out.env?.ANTHROPIC_BASE_URL).toBe("http://host.docker.internal:9");
    expect(out.env?.ANTHROPIC_AUTH_TOKEN).toBe(CUSTOM_HARNESS_AUTH_TOKEN);
    expect(out.env?.CLAUDE_CODE_OAUTH_TOKEN).toBeUndefined();
    expect(out.env?.ARGUS_CUSTOM_HARNESS).toBe("1");
    // The Claude gateway stub stays: argus-delegate claude, after dropping the bridge
    // env, runs on the leased Anthropic account through the gateway (never the bridge).
    expect(out.credentialStubs).toEqual([
      { containerPath: CLAUDE_GATEWAY_STUB_PATH, content: "{}" },
    ]);
  });

  it("clears the custom harness marker without a harness", () => {
    const out = withCustomHarnessRuntime(
      {
        env: { ARGUS_CUSTOM_HARNESS: "1", ANTHROPIC_BASE_URL: "http://host.docker.internal:9" },
      } as never,
      null,
    );
    expect(out.env?.ARGUS_CUSTOM_HARNESS).toBeUndefined();
    expect(out.env?.ANTHROPIC_BASE_URL).toBeUndefined();
  });

  it("customHarnessEnv never includes upstream host in no_proxy omission", () => {
    const env = customHarnessEnv({
      version: 1,
      anthropic_base_url: "http://host.docker.internal:1",
      model: "m",
    });
    expect(env.ANTHROPIC_MODEL).toBe("m");
    expect(env.ARGUS_ALLOWED_ANTHROPIC_BASE_URL).toBe("http://host.docker.internal:1");
  });
});

describe("bridge key", () => {
  const settings = {
    base_url: "https://openrouter.ai/api/v1",
    label: "openrouter",
    model: "m",
    provider: null,
  };

  it("the container sends the bridge key as a custom header", () => {
    const key = "ab".repeat(32);
    const h = customHarnessFromLease("http://host.docker.internal:9", settings, key)!;
    expect(h.bridge_key).toBe(key);
    expect(customHarnessEnv(h).ANTHROPIC_CUSTOM_HEADERS).toBe(`x-argus-bridge-key: ${key}`);
  });

  it("ignores a malformed key and clears the header without a harness", () => {
    expect(
      customHarnessFromLease("http://host.docker.internal:9", settings, "x y")!.bridge_key,
    ).toBeUndefined();
    const out = withCustomHarnessRuntime(
      { env: { ANTHROPIC_CUSTOM_HEADERS: "x-argus-bridge-key: old" } } as never,
      null,
    );
    expect(out.env?.ANTHROPIC_CUSTOM_HEADERS).toBeUndefined();
  });
});
