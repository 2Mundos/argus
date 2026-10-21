import type { ContainerConfig } from "@onecli-sh/sdk";
import { describe, expect, it } from "vitest";
import {
  CLAUDE_GATEWAY_STUB_PATH,
  claudeGatewayStub,
  isGatewaySentinel,
  proxyAddress,
  withClaudeDelegateRuntime,
} from "./argus-claude-stub.js";

// Built at runtime so no literal looks like a real credential to a scanner.
const REAL_LOOKING = "sk-ant-" + "oat01-" + "R".repeat(96);
const SENTINEL = "onecli-managed-claude-sentinel";
const PROXY_SECRET = "proxy-pass-SECRET";

const config = (over: Partial<ContainerConfig["env"]> = {}, extra: object = {}): ContainerConfig =>
  ({
    env: {
      CLAUDE_CODE_OAUTH_TOKEN: SENTINEL,
      HTTPS_PROXY: `http://x:${PROXY_SECRET}@host.docker.internal:10255`,
      NODE_EXTRA_CA_CERTS: "/tmp/onecli-ca.pem",
      ...over,
    },
    caCertificate: "-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----",
    caCertificateContainerPath: "/tmp/onecli-ca.pem",
    credentialStubs: [],
    ...extra,
  }) as unknown as ContainerConfig;

describe("isGatewaySentinel", () => {
  it.each([
    SENTINEL,
    "placeholder-token",
    "sk-ant-oat01-onecli-managed-placeholder",
    "opaque-gateway-value-12345",
  ])("accepts %s", (v) => expect(isGatewaySentinel(v)).toBe(true));

  it.each([
    ["a real-looking subscription token", REAL_LOOKING],
    ["a real-looking API key", "sk-ant-" + "api03-" + "K".repeat(90)],
    ["an unmarked long value", "x".repeat(130)],
    ["whitespace", "onecli managed value"],
    ["a newline", "onecli-managed\nvalue"],
    ["too short", "abc"],
    ["empty", ""],
    ["too long", "onecli-" + "a".repeat(260)],
    ["a number", 12345678],
    ["null", null],
  ])("rejects %s", (_name, v) => expect(isGatewaySentinel(v)).toBe(false));
});

describe("proxyAddress", () => {
  it("keeps only host:port, never the proxy credentials", () => {
    expect(proxyAddress(`http://u:${PROXY_SECRET}@Gateway.Local:8080/path`)).toBe(
      "gateway.local:8080",
    );
    expect(proxyAddress("https://gw.local")).toBe("gw.local:443");
    expect(proxyAddress("socks5://gw.local:1080")).toBeNull();
    expect(proxyAddress("not a url")).toBeNull();
    expect(proxyAddress(undefined)).toBeNull();
  });
});

describe("withClaudeDelegateRuntime", () => {
  it("adds a read-only-mounted stub with the sentinel, proxy address and CA path only", () => {
    const out = withClaudeDelegateRuntime(config());
    const stub = out.credentialStubs!.find((s) => s.containerPath === CLAUDE_GATEWAY_STUB_PATH)!;
    expect(JSON.parse(stub.content)).toEqual({
      version: 1,
      provider: "anthropic",
      variable: "CLAUDE_CODE_OAUTH_TOKEN",
      sentinel: SENTINEL,
      proxy: "host.docker.internal:10255",
      ca: "/tmp/onecli-ca.pem",
    });
    expect(stub.content).not.toContain(PROXY_SECRET);
    expect(out.env).toEqual(config().env); // the container env is untouched
  });

  it("keeps the other stubs and replaces a pre-existing file at its path", () => {
    const cfg = config(
      {},
      {
        credentialStubs: [
          { containerPath: "/home/node/.codex/auth.json", content: "{}" },
          { containerPath: CLAUDE_GATEWAY_STUB_PATH, content: REAL_LOOKING },
        ],
      },
    );
    const stubs = withClaudeDelegateRuntime(cfg).credentialStubs!;
    expect(stubs.map((s) => s.containerPath)).toEqual([
      "/home/node/.codex/auth.json",
      CLAUDE_GATEWAY_STUB_PATH,
    ]);
    expect(JSON.stringify(stubs)).not.toContain(REAL_LOOKING);
  });

  it.each([
    ["no Anthropic assignment (no sentinel issued)", { CLAUDE_CODE_OAUTH_TOKEN: undefined }],
    [
      "an API-key assignment only",
      { CLAUDE_CODE_OAUTH_TOKEN: undefined, ANTHROPIC_API_KEY: SENTINEL },
    ],
    ["a real credential inadvertently in the env", { CLAUDE_CODE_OAUTH_TOKEN: REAL_LOOKING }],
    ["no proxy", { HTTPS_PROXY: undefined }],
    ["a non-http proxy", { HTTPS_PROXY: "socks5://gw:1" }],
    ["a redirected API base URL", { ANTHROPIC_BASE_URL: "https://evil.example" }],
    ["a local harness redirect", { ANTHROPIC_BASE_URL: "http://host.docker.internal:8080" }],
    ["an API unix socket", { ANTHROPIC_UNIX_SOCKET: "/tmp/s.sock" }],
  ])("emits no stub for %s", (_name, env) => {
    const out = withClaudeDelegateRuntime(config(env as ContainerConfig["env"]));
    expect(out.credentialStubs ?? []).toEqual([]);
    expect(claudeGatewayStub(config(env as ContainerConfig["env"]))).toBeNull();
  });

  it("emits no stub without a CA path, and drops a foreign file at the stub path", () => {
    expect(
      claudeGatewayStub(config({}, { caCertificateContainerPath: "relative/ca.pem" })),
    ).toBeNull();
    const foreign = config(
      { CLAUDE_CODE_OAUTH_TOKEN: undefined },
      { credentialStubs: [{ containerPath: CLAUDE_GATEWAY_STUB_PATH, content: "{}" }] },
    );
    expect(withClaudeDelegateRuntime(foreign).credentialStubs).toEqual([]);
  });
});
