import { describe, expect, it } from "vitest";
import { CLAUDE_GATEWAY_STUB_PATH, withClaudeDelegateRuntime } from "./argus-claude-stub.js";
import {
  LOCAL_HARNESS_AUTH_TOKEN,
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
