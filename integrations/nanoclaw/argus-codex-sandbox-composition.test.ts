/**
 * Codex sandbox profile — host configuration to `docker create`, through the real
 * `composeSessionSpec` and the real Docker driver (recording CLI). Installed into
 * src/ next to container-runner.ts.
 *
 * The "worker" is the NanoClaw host process: each Argus pool's `env` becomes that
 * host's process.env, so two workers are two different env objects here.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ContainerConfig } from "./container-config.js";
import { composeSessionSpec } from "./container-runner.js";
import { CODEX_SANDBOX_ENV } from "./drivers/argus-codex-sandbox.js";
import { DockerSessionDriver } from "./drivers/docker-driver.js";
import { FakeCli } from "./drivers/fake-cli.js";
import { FIXTURE_POLICY, fixtureAuxContainer } from "./drivers/spec-fixture.js";
import { resetGatewayProvider } from "./gateway-providers/index.js";
import type { VolumeMount } from "./providers/provider-container-registry.js";
import type { AgentGroup, Session } from "./types.js";

vi.mock("./log.js", () => ({
  log: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn(), fatal: vi.fn() },
}));
vi.mock("fs", async (importOriginal) => {
  const actual = await importOriginal<typeof import("fs")>();
  return { default: { ...actual, existsSync: vi.fn(() => true) } };
});

const agentGroup = {
  id: "agent-1",
  name: "Agent One",
  folder: "agent-one",
  agent_provider: null,
} as AgentGroup;
const session = (provider: string | null): Session =>
  ({
    id: "session-1",
    agent_group_id: "agent-1",
    agent_provider: provider,
    status: "active",
  }) as Session;
const containerConfig = (provider?: string) =>
  ({
    provider,
    mcpServers: {},
    packages: { apt: [], npm: [] },
    additionalMounts: [],
    skills: [],
  }) as unknown as ContainerConfig;
const mounts: VolumeMount[] = [
  {
    hostPath: "/install/data/v2-sessions/agent-1/session-1",
    containerPath: "/workspace",
    readonly: false,
    mountClass: "group-state",
    scope: "agent-1",
  },
];

function compose(
  opts: {
    sessionProvider?: string | null;
    groupProvider?: string;
    gatewayContainers?: boolean;
  } = {},
) {
  return composeSessionSpec({
    agentGroup,
    session: session(opts.sessionProvider ?? null),
    containerName: "nanoclaw-v2-agent-one-1700000000000",
    mounts,
    containerConfig: containerConfig(opts.groupProvider),
    mailboxEnvironment: {},
    contribution: {} as never,
    gateway: {
      networkAccess: opts.gatewayContainers
        ? { endpoint: "egress-proxy", target: { kind: "session-container", role: "egress-proxy" } }
        : { endpoint: "localhost", target: { kind: "host" } },
      containers: opts.gatewayContainers ? [fixtureAuxContainer()] : undefined,
    } as never,
  });
}

let cli: FakeCli;
async function createArgv(spec: ReturnType<typeof compose>): Promise<string[][]> {
  const driver = new DockerSessionDriver({
    ...FIXTURE_POLICY,
    cli,
    seccompSource: {
      path: "/install/security/argus-codex-worker.seccomp.json",
      read: () => '{"defaultAction":"SCMP_ACT_ERRNO","syscalls":[{}]}',
    },
  });
  await driver.prepare(spec);
  return cli.calls.filter((c) => c.args[0] === "create").map((c) => c.args);
}

beforeEach(() => {
  cli = new FakeCli("docker");
  cli.responses = [
    { match: /^inspect /, throws: new Error("No such object") },
    { match: /^info /, output: '["name=apparmor","name=seccomp,profile=builtin"]' },
    {
      match: /^run /,
      output:
        "argus-codex-worker (enforce)\nNoNewPrivs:\t1\nSeccomp:\t2\nCapEff:\t0000000000000000\n",
    },
  ];
});
afterEach(() => {
  vi.unstubAllEnvs();
  resetGatewayProvider();
});

describe("composition: codex-bwrap default on", () => {
  it("default (unset): profile on the codex agent with the fixed flags", async () => {
    const spec = compose({ groupProvider: "codex" });
    expect(spec.containers[0].securityProfile).toBe("codex-bwrap");
    const [args] = await createArgv(spec);
    const joined = args.join(" ");
    expect(args).toContain("--cap-drop=ALL");
    expect(joined).toContain("--security-opt no-new-privileges");
    expect(joined).toContain("--security-opt apparmor=argus-codex-worker");
    expect(joined).toContain(
      "--security-opt seccomp=/install/security/argus-codex-worker.seccomp.json",
    );
  });

  it("explicit off: no profile and docker is never asked for security opts", async () => {
    vi.stubEnv(CODEX_SANDBOX_ENV, "off");
    const spec = compose({ groupProvider: "codex" });
    expect(spec.containers.every((c) => c.securityProfile === undefined)).toBe(true);
    const [args] = await createArgv(spec);
    expect(args.join(" ")).not.toMatch(/apparmor|seccomp/);
    expect(cli.joined().some((c) => c.startsWith("info "))).toBe(false);
  });

  it("explicit codex-bwrap matches the default", () => {
    vi.stubEnv(CODEX_SANDBOX_ENV, "codex-bwrap");
    expect(compose({ groupProvider: "codex" }).containers[0].securityProfile).toBe("codex-bwrap");
  });

  it("a session-pinned codex provider counts, as spawn resolves it", () => {
    expect(
      compose({ sessionProvider: "codex", groupProvider: "claude" }).containers[0].securityProfile,
    ).toBe("codex-bwrap");
    expect(
      compose({ sessionProvider: "claude", groupProvider: "codex" }).containers[0].securityProfile,
    ).toBeUndefined();
  });

  it("non-codex group (claude, opencode, unset): no profile on the agent", async () => {
    for (const groupProvider of [undefined, "claude", "opencode"]) {
      const spec = compose({ groupProvider });
      expect(spec.containers[0].securityProfile, String(groupProvider)).toBeUndefined();
    }
    const [args] = await createArgv(compose({ groupProvider: "claude" }));
    expect(args.join(" ")).not.toMatch(/apparmor|seccomp/);
  });

  it("a neighbouring worker with off is unaffected by a default-on peer", () => {
    const defaultOn = compose({ groupProvider: "codex" });
    vi.stubEnv(CODEX_SANDBOX_ENV, "off");
    const neighbour = compose({ groupProvider: "codex" });
    expect(defaultOn.containers[0].securityProfile).toBe("codex-bwrap");
    expect(neighbour.containers[0].securityProfile).toBeUndefined();
  });

  it("auxiliary gateway containers never get it, and the agent does", async () => {
    const spec = compose({ groupProvider: "codex", gatewayContainers: true });
    expect(spec.containers.map((c) => [c.role, c.securityProfile])).toEqual([
      ["agent", "codex-bwrap"],
      ["egress-proxy", undefined],
    ]);
    const [aux, agent] = await createArgv(spec);
    expect(aux.join(" ")).not.toMatch(/apparmor|seccomp/);
    expect(agent.join(" ")).toContain("apparmor=argus-codex-worker");
  });

  it("unknown values refuse the spawn for every provider; nothing is created", () => {
    for (const bad of ["true", "unconfined", "codex-bwrap2"]) {
      vi.stubEnv(CODEX_SANDBOX_ENV, bad);
      expect(() => compose({ groupProvider: "codex" }), bad).toThrow(/not a known profile/);
      expect(() => compose({ groupProvider: "claude" }), bad).toThrow(/not a known profile/);
    }
    expect(cli.calls).toEqual([]);
  });

  it("the profile env never reaches the agent container environment", () => {
    vi.stubEnv(CODEX_SANDBOX_ENV, "codex-bwrap");
    const agent = compose({ groupProvider: "codex" }).containers[0];
    expect(Object.keys({ ...agent.env, ...agent.contributedEnv })).not.toContain(CODEX_SANDBOX_ENV);
  });

  it("a daemon without AppArmor refuses even when composition succeeded", async () => {
    cli.responses[1] = { match: /^info /, output: '["name=seccomp"]' };
    await expect(createArgv(compose({ groupProvider: "codex" }))).rejects.toThrow(/AppArmor/);
    expect(cli.calls.some((c) => c.args[0] === "create")).toBe(false);
  });
});
