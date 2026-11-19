/**
 * The Copilot gateway sentinel through the real install path: the real OneCLI adapter (with
 * the Argus Copilot hook) → composeSessionSpec → the real Docker driver's admission
 * (validateSpec, recording CLI). OneCLI's SDK and REST API are mocked; nothing else is.
 * Covers ordinary and repository-preflight sessions, with and without a Copilot lease, and
 * the negative controls: real credential values stay refused in both env lanes.
 * Installed into src/ next to container-runner.ts.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ContainerConfig } from "./container-config.js";
import { composeSessionSpec } from "./container-runner.js";
import { DockerSessionDriver } from "./drivers/docker-driver.js";
import { FakeCli } from "./drivers/fake-cli.js";
import { FIXTURE_POLICY } from "./drivers/spec-fixture.js";
import type { GatewayContribution } from "./gateway-providers/gateway-provider-registry.js";
import type { VolumeMount } from "./providers/provider-container-registry.js";
import type { AgentGroup, Session } from "./types.js";

vi.mock("./db/agent-groups.js", async (original) => ({
  ...(await original<typeof import("./db/agent-groups.js")>()),
  getAgentGroup: async () => undefined,
}));
vi.mock("@onecli-sh/sdk", () => ({
  OneCLI: class {
    ensureAgent = async () => ({ created: false });
    getContainerConfig = async () => ({
      env: { HTTPS_PROXY: "http://host.docker.internal:15001" },
      caCertificate: "synthetic-ca",
      caCertificateContainerPath: "/tmp/onecli-ca.pem",
    });
  },
  ApprovalClient: class {
    start() {
      return new Promise<void>(() => undefined);
    }
    stop() {}
  },
}));
vi.mock("./env.js", () => ({
  readEnvFile: () => ({ ONECLI_URL: "http://onecli.test", ONECLI_API_KEY: "unused" }),
}));
vi.mock("./log.js", () => ({
  log: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn(), fatal: vi.fn() },
}));
vi.mock("fs", async (importOriginal) => {
  const actual = await importOriginal<typeof import("fs")>();
  return { default: { ...actual, existsSync: vi.fn(() => true) } };
});

import { getGatewayProviderRegistration } from "./gateway-providers/gateway-provider-registry.js";
import "./gateway-providers/onecli.js";

// Same constant as integrations/nanoclaw/argus-copilot-stub.ts (installed alongside).
import { COPILOT_INJECTED, COPILOT_SENTINEL_TOKEN } from "./gateway-providers/argus-copilot-stub.js";

// Fake only: stands in for real material that must never be admitted.
const FAKE_REAL_PAT = "github_pat_FAKE_REAL_VALUE_NEVER_IN_CONTAINER";
const FAKE_GHP = "ghp_FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0000";

const agentGroup = {
  id: "agent-1",
  name: "Agent One",
  folder: "agent-one",
  agent_provider: null,
} as AgentGroup;
const session = {
  id: "session-1",
  agent_group_id: "agent-1",
  agent_provider: null,
  status: "active",
} as Session;
const mounts: VolumeMount[] = [
  {
    hostPath: "/install/data/v2-sessions/agent-1/session-1",
    containerPath: "/workspace",
    readonly: false,
    mountClass: "group-state",
    scope: "agent-1",
  },
];

/** OneCLI REST as the adapter hook reads it: metadata only, no secret values. */
function onecliApi(leased: boolean): typeof fetch {
  const secrets = COPILOT_INJECTED.map(([hostPattern, pathPattern], i) => ({
    id: `c${i}`,
    type: "generic",
    hostPattern,
    pathPattern,
  }));
  return (async (url: string) => {
    const path = new URL(url).pathname;
    if (path === "/api/agents") return Response.json([{ id: "ag-1", identifier: "agent-1" }]);
    if (path === "/api/agents/ag-1/secrets")
      return Response.json(leased ? secrets.map((s) => s.id) : []);
    if (path === "/api/secrets") return Response.json(secrets);
    return new Response("not found", { status: 404 });
  }) as typeof fetch;
}

async function leaseContribution(leased: boolean): Promise<GatewayContribution> {
  vi.spyOn(globalThis, "fetch").mockImplementation(onecliApi(leased));
  const provider = getGatewayProviderRegistration("onecli");
  if (!provider) throw new Error("the onecli gateway provider is not registered");
  const lease = await provider.sessions.ensure(
    {
      key: { installSlug: "install", agentGroupId: "agent-1", sessionId: "session-1" },
      runtimeIdentity: "install/agent-1/session-1",
      groupName: "Agent One",
      containerName: "nanoclaw-v2-agent-one-1700000000000",
      capabilities: {} as never,
    },
    new AbortController().signal,
  );
  return lease.contribution;
}

function compose(gateway: GatewayContribution) {
  return composeSessionSpec({
    agentGroup,
    session,
    containerName: "nanoclaw-v2-agent-one-1700000000000",
    mounts,
    containerConfig: {
      mcpServers: {},
      packages: { apt: [], npm: [] },
      additionalMounts: [],
      skills: [],
    } as unknown as ContainerConfig,
    mailboxEnvironment: {},
    contribution: {} as never,
    gateway: { ...gateway, networkAccess: { endpoint: "localhost", target: { kind: "host" } } },
  } as never);
}

/** The repository-preflight session: the worker composition with PID 1 and args replaced. */
function asPreflight(spec: ReturnType<typeof compose>) {
  const agent = spec.containers.find((c) => c.role === "agent");
  if (!agent) throw new Error("no agent container");
  agent.command = ["/usr/local/bin/argus-delegate"];
  agent.args = ["copilot", "--auth-probe", "--result-file", "/preflight/result.json"];
  agent.contributedEnv = {
    ...agent.contributedEnv,
    ARGUS_REPOSITORIES_JSON: JSON.stringify({ repository: "https://github.com/o/r.git" }),
  };
  return spec;
}

let cli: FakeCli;
async function admit(spec: ReturnType<typeof compose>): Promise<string[]> {
  const driver = new DockerSessionDriver({ ...FIXTURE_POLICY, cli });
  await driver.prepare(spec);
  return cli.calls.filter((c) => c.args[0] === "create").map((c) => c.args.join(" "));
}

beforeEach(() => {
  cli = new FakeCli("docker");
  cli.responses = [{ match: /^inspect /, throws: new Error("No such object") }];
});
afterEach(() => {
  vi.restoreAllMocks();
});

const SESSIONS = [
  ["ordinary task session", (s: ReturnType<typeof compose>) => s],
  ["repository-preflight session", asPreflight],
] as const;

describe.each(SESSIONS)("%s", (_name, shape) => {
  it("with a Copilot lease: the sentinel passes admission and reaches the container", async () => {
    const spec = shape(compose(await leaseContribution(true)));
    const agent = spec.containers[0];
    expect(agent.contributedEnv?.COPILOT_GITHUB_TOKEN).toBe(COPILOT_SENTINEL_TOKEN);
    expect(agent.env.COPILOT_GITHUB_TOKEN).toBeUndefined();
    const [create] = await admit(spec);
    expect(create).toContain(`COPILOT_GITHUB_TOKEN=${COPILOT_SENTINEL_TOKEN}`);
  });

  it("without a Copilot lease: no sentinel, and admission is unchanged", async () => {
    const spec = shape(compose(await leaseContribution(false)));
    expect(spec.containers[0].contributedEnv).not.toHaveProperty("COPILOT_GITHUB_TOKEN");
    const [create] = await admit(spec);
    expect(create).not.toContain("COPILOT_GITHUB_TOKEN");
  });

  it("a real token under the sentinel's key is refused, lease or not", async () => {
    for (const leased of [true, false]) {
      const spec = shape(compose(await leaseContribution(leased)));
      for (const real of [FAKE_REAL_PAT, FAKE_GHP, `${COPILOT_SENTINEL_TOKEN}x`]) {
        spec.containers[0].contributedEnv = {
          ...spec.containers[0].contributedEnv,
          COPILOT_GITHUB_TOKEN: real,
        };
        await expect(admit(spec), real).rejects.toThrow(/credential value in contributed env/);
      }
    }
    expect(cli.calls.filter((c) => c.args[0] === "create")).toEqual([]);
  });

  it("the sentinel is exempt only for its own key", async () => {
    const spec = shape(compose(await leaseContribution(false)));
    for (const key of ["GITHUB_TOKEN", "GH_TOKEN", "OTHER", "copilot_github_token"]) {
      spec.containers[0].contributedEnv = {
        ...spec.containers[0].contributedEnv,
        [key]: COPILOT_SENTINEL_TOKEN,
      };
      await expect(admit(spec), key).rejects.toThrow(/credential value in contributed env/);
    }
  });

  it("other credential-shaped contributed values and the ordinary env lane stay refused", async () => {
    const spec = shape(compose(await leaseContribution(true)));
    const agent = spec.containers[0];
    agent.contributedEnv = { ...agent.contributedEnv, SOME_CONFIG: FAKE_GHP };
    await expect(admit(spec)).rejects.toThrow(/credential value in contributed env/);

    const spec2 = shape(compose(await leaseContribution(true)));
    spec2.containers[0].env.COPILOT_GITHUB_TOKEN = COPILOT_SENTINEL_TOKEN;
    await expect(admit(spec2)).rejects.toThrow(/secret-shaped env/);
    const spec3 = shape(compose(await leaseContribution(false)));
    spec3.containers[0].env.ANYTHING = FAKE_REAL_PAT;
    await expect(admit(spec3)).rejects.toThrow(/secret-shaped env/);
  });
});
