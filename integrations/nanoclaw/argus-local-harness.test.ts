import { describe, expect, it } from "vitest";
import { CLAUDE_GATEWAY_STUB_PATH, withClaudeDelegateRuntime } from "./argus-claude-stub.js";
import {
  DEFAULT_LOCAL_CONTEXT_TOKENS,
  LOCAL_HARNESS_AUTH_TOKEN,
  LOCAL_HARNESS_ENV_KEYS,
  localContextEnv,
  localCredentialProblem,
  localHarnessEnv,
  localHarnessFromCredential,
  withLocalHarnessRuntime,
} from "./argus-local-harness.js";
import {
  anthropicRedirectRejection,
  normalizeAnthropicBaseUrl,
} from "./engines/anthropic-redirect.mjs";

describe("normalizeAnthropicBaseUrl", () => {
  it("normalises host.docker.internal without a path", () => {
    expect(normalizeAnthropicBaseUrl("http://host.docker.internal:8080/")).toBe(
      "http://host.docker.internal:8080",
    );
  });
});

describe("anthropicRedirectRejection", () => {
  const allowed = "http://host.docker.internal:8080";
  it("accepts the exact allowed local URL with only the harness placeholder token", () => {
    expect(
      anthropicRedirectRejection(
        {
          ANTHROPIC_BASE_URL: allowed,
          ARGUS_ALLOWED_ANTHROPIC_BASE_URL: allowed,
          ANTHROPIC_AUTH_TOKEN: LOCAL_HARNESS_AUTH_TOKEN,
        },
        allowed,
      ),
    ).toBeNull();
  });
  it("rejects another URL", () => {
    expect(
      anthropicRedirectRejection(
        { ANTHROPIC_BASE_URL: "https://evil.example", ARGUS_ALLOWED_ANTHROPIC_BASE_URL: allowed },
        allowed,
      ),
    ).toBe("api_redirected");
  });
  it("rejects a matching allowlist URL when the host is not local", () => {
    expect(
      anthropicRedirectRejection(
        {
          ANTHROPIC_BASE_URL: "http://10.0.0.5:8080",
          ARGUS_ALLOWED_ANTHROPIC_BASE_URL: "http://10.0.0.5:8080",
          ANTHROPIC_AUTH_TOKEN: LOCAL_HARNESS_AUTH_TOKEN,
        },
        "http://10.0.0.5:8080",
      ),
    ).toBe("api_redirected");
  });
  it("rejects allowed URL with a cloud oauth token", () => {
    expect(
      anthropicRedirectRejection(
        {
          ANTHROPIC_BASE_URL: allowed,
          ARGUS_ALLOWED_ANTHROPIC_BASE_URL: allowed,
          CLAUDE_CODE_OAUTH_TOKEN: "onecli-managed-sentinel",
        },
        allowed,
      ),
    ).toBe("api_redirected");
  });
});

describe("withLocalHarnessRuntime", () => {
  it("sets redirect env and strips cloud Anthropic variables", () => {
    const harness = localHarnessFromCredential({
      base_url: "http://host.docker.internal:8080",
    })!;
    const withStub = withClaudeDelegateRuntime({
      env: {
        CLAUDE_CODE_OAUTH_TOKEN: "onecli-managed-sentinel",
        HTTPS_PROXY: "http://x@host.docker.internal:10255",
      },
      caCertificateContainerPath: "/tmp/ca.pem",
      credentialStubs: [],
    } as never);
    const out = withLocalHarnessRuntime(withStub, harness);
    expect(out.env?.ANTHROPIC_BASE_URL).toBe("http://host.docker.internal:8080");
    expect(out.env?.ARGUS_ALLOWED_ANTHROPIC_BASE_URL).toBe(out.env?.ANTHROPIC_BASE_URL);
    expect(out.env?.ANTHROPIC_AUTH_TOKEN).toBe(LOCAL_HARNESS_AUTH_TOKEN);
    expect(out.env?.CLAUDE_CODE_OAUTH_TOKEN).toBeUndefined();
    expect(out.env?.NO_PROXY).toContain("host.docker.internal");
    expect(out.env?.no_proxy).toBe(out.env?.NO_PROXY);
    expect(out.credentialStubs?.some((s) => s.containerPath === CLAUDE_GATEWAY_STUB_PATH)).toBe(
      false,
    );
    expect(localHarnessEnv(harness).ANTHROPIC_MODEL).toBeUndefined();
  });
});

describe("withLocalHarnessRuntime without a harness (cloud mode)", () => {
  it("keeps the Claude gateway stub and drops only stale redirect env", () => {
    const withStub = withClaudeDelegateRuntime({
      env: {
        CLAUDE_CODE_OAUTH_TOKEN: "onecli-managed-sentinel",
        HTTPS_PROXY: "http://x@host.docker.internal:10255",
        ARGUS_ALLOWED_ANTHROPIC_BASE_URL: "http://host.docker.internal:8080",
      },
      caCertificateContainerPath: "/tmp/ca.pem",
      credentialStubs: [],
    } as never);
    expect(
      withStub.credentialStubs?.some((s) => s.containerPath === CLAUDE_GATEWAY_STUB_PATH),
    ).toBe(true);
    const out = withLocalHarnessRuntime(withStub, null);
    expect(out.credentialStubs?.some((s) => s.containerPath === CLAUDE_GATEWAY_STUB_PATH)).toBe(
      true,
    );
    expect(out.env?.CLAUDE_CODE_OAUTH_TOKEN).toBe("onecli-managed-sentinel");
    expect(out.env?.ARGUS_ALLOWED_ANTHROPIC_BASE_URL).toBeUndefined();
  });
});

describe("localCredentialProblem", () => {
  it("rejects a public-network base URL", () => {
    expect(localCredentialProblem({ base_url: "http://192.168.1.5:8080" })).toContain(
      "host.docker.internal",
    );
  });
});

describe("local context window (local harness spec)", () => {
  const base = "http://host.docker.internal:8080";
  it("sizes Claude Code's window and auto-compact from the account's context_tokens", () => {
    const harness = localHarnessFromCredential({ base_url: base, context_tokens: 65536 })!;
    expect(harness.context_tokens).toBe(65536);
    const env = localHarnessEnv(harness);
    expect(env.CLAUDE_CODE_MAX_CONTEXT_TOKENS).toBe("65536");
    const compactAt = Number(env.CLAUDE_CODE_AUTO_COMPACT_WINDOW);
    const output = Number(env.CLAUDE_CODE_MAX_OUTPUT_TOKENS);
    // compaction starts with room for the reply and a tokenizer margin to spare
    expect(compactAt + output).toBeLessThan(65536 * 0.85);
    expect(compactAt).toBeGreaterThan(65536 / 2);
    expect(env.ARGUS_LOCAL_HARNESS).toBe("1");
    expect(Number(env.CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS)).toBeLessThanOrEqual(4000);
    expect(Number(env.BASH_MAX_OUTPUT_LENGTH)).toBeLessThanOrEqual(8000);
  });
  it("assumes a small window when neither the account nor the probe names one", () => {
    const env = localHarnessEnv(localHarnessFromCredential({ base_url: base })!);
    expect(env.CLAUDE_CODE_MAX_CONTEXT_TOKENS).toBe(String(DEFAULT_LOCAL_CONTEXT_TOKENS));
    expect(Number(env.CLAUDE_CODE_AUTO_COMPACT_WINDOW)).toBeLessThan(DEFAULT_LOCAL_CONTEXT_TOKENS);
  });
  it("follows a bigger configured window", () => {
    expect(localContextEnv(131072).CLAUDE_CODE_MAX_CONTEXT_TOKENS).toBe("131072");
    expect(Number(localContextEnv(131072).CLAUDE_CODE_AUTO_COMPACT_WINDOW)).toBeGreaterThan(
      Number(localContextEnv(65536).CLAUDE_CODE_AUTO_COMPACT_WINDOW),
    );
  });
  it("validates context_tokens", () => {
    expect(localCredentialProblem({ base_url: base, context_tokens: 65536 })).toBeNull();
    for (const bad of [1024, 65536.5, "65536", true])
      expect(localCredentialProblem({ base_url: base, context_tokens: bad })).toContain(
        "context_tokens",
      );
  });
  it("cloud mode clears every local harness variable", () => {
    const local = withLocalHarnessRuntime(
      { env: {} } as never,
      localHarnessFromCredential({ base_url: base, context_tokens: 65536 }),
    );
    const cloud = withLocalHarnessRuntime(local, null);
    for (const key of LOCAL_HARNESS_ENV_KEYS) expect(cloud.env?.[key]).toBeUndefined();
  });
});
