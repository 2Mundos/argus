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
      },
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
    expect(out.credentialStubs).toEqual([]);
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
