/**
 * Codex sandbox profile — the opt-in token, the seccomp/daemon checks and the Docker
 * driver's argv. Installed into src/drivers/ next to the files it exercises.
 *
 * The driver cases run the real DockerSessionDriver against a recording CLI: they
 * prove which `docker create` argv is issued, not that Bubblewrap works under the
 * profile (that needs the root-only RUNBOOK probe).
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  APPARMOR_PROFILE_NAME,
  assertDaemonSupportsProfile,
  assertDriverSupportsSecurityProfiles,
  CODEX_SANDBOX_ENV,
  codexSandboxProfileFromEnv,
  defaultSeccompSource,
  type SeccompSource,
  securityProfileArgs,
  securityProfileForProvider,
  validateSeccompProfile,
} from "./argus-codex-sandbox.js";
import { DockerSessionDriver } from "./docker-driver.js";
import { FakeCli } from "./fake-cli.js";
import {
  FIXTURE_POLICY,
  fixtureAuxContainer,
  fixtureSpec,
  fixtureSpecWithAux,
} from "./spec-fixture.js";
import { type DriverCapabilities, validateSpec } from "./types.js";

vi.mock("../log.js", () => ({
  log: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn(), fatal: vi.fn() },
}));
vi.mock("fs", async (importOriginal) => {
  const actual = await importOriginal<typeof import("fs")>();
  return { default: { ...actual, existsSync: vi.fn(() => true) } };
});

const GOOD_SECCOMP = JSON.stringify({
  defaultAction: "SCMP_ACT_ERRNO",
  syscalls: [{ names: ["read"], action: "SCMP_ACT_ALLOW" }],
});
const DAEMON_OK = '["name=apparmor","name=seccomp,profile=builtin","name=cgroupns"]';
const ENFORCE = `argus-codex-worker (enforce)\nName:\tcat\nNoNewPrivs:\t1\nSeccomp:\t2\nCapEff:\t0000000000000000\n`;
const source = (text: string | Error = GOOD_SECCOMP): SeccompSource => ({
  path: "/install/security/argus-codex-worker.seccomp.json",
  read: () => {
    if (text instanceof Error) throw text;
    return text;
  },
});

let cli: FakeCli;
let clock = 1_000;
const driver = (seccompSource: SeccompSource = source()) =>
  new DockerSessionDriver({ ...FIXTURE_POLICY, cli, seccompSource, now: () => clock });
const creates = () => cli.calls.filter((c) => c.args[0] === "create").map((c) => c.args);
const profiled = () => {
  const spec = fixtureSpec();
  spec.containers[0].securityProfile = "codex-bwrap";
  return spec;
};

beforeEach(() => {
  cli = new FakeCli("docker");
  cli.responses = [
    { match: /^inspect /, throws: new Error("No such object") },
    { match: /^info /, output: DAEMON_OK },
    { match: /^run /, output: ENFORCE },
  ];
  clock = 1_000;
});

describe("opt-in token", () => {
  it("is off by default and for blank values", () => {
    expect(codexSandboxProfileFromEnv({})).toBeUndefined();
    expect(codexSandboxProfileFromEnv({ [CODEX_SANDBOX_ENV]: "  " })).toBeUndefined();
  });

  it("accepts only the exact token", () => {
    expect(codexSandboxProfileFromEnv({ [CODEX_SANDBOX_ENV]: "codex-bwrap" })).toBe("codex-bwrap");
    for (const bad of [
      "true",
      "1",
      "off",
      "CODEX-BWRAP",
      "codex-bwrap,unconfined",
      "unconfined",
      "../x",
    ]) {
      expect(() => codexSandboxProfileFromEnv({ [CODEX_SANDBOX_ENV]: bad }), bad).toThrow(
        /not a known profile/,
      );
    }
  });

  it("refuses a private inner Docker daemon", () => {
    expect(() =>
      codexSandboxProfileFromEnv({
        [CODEX_SANDBOX_ENV]: "codex-bwrap",
        ARGUS_NANOCLAW_INNER_DOCKER: "1",
      }),
    ).toThrow(/inner Docker daemon/);
  });

  it("applies to the native codex provider only, but validates for every provider", () => {
    const env = { [CODEX_SANDBOX_ENV]: "codex-bwrap" };
    expect(securityProfileForProvider("codex", env)).toBe("codex-bwrap");
    for (const other of ["claude", "opencode", "cursor"])
      expect(securityProfileForProvider(other, env)).toBeUndefined();
    expect(securityProfileForProvider("codex", {})).toBeUndefined();
    expect(() => securityProfileForProvider("claude", { [CODEX_SANDBOX_ENV]: "nope" })).toThrow();
  });
});

describe("seccomp profile", () => {
  it("accepts the candidate JSON this PR ships", () => {
    const shipped = defaultSeccompSource();
    expect(shipped.path).toMatch(/\/security\/argus-codex-worker\.seccomp\.json$/);
    expect(() => validateSeccompProfile(shipped.read(shipped.path))).not.toThrow();
  });

  it("rejects malformed, non-object, default-allow and empty profiles", () => {
    expect(() => validateSeccompProfile("{")).toThrow(/not valid JSON/);
    expect(() => validateSeccompProfile("[]")).toThrow(/JSON object/);
    expect(() => validateSeccompProfile("null")).toThrow(/JSON object/);
    for (const action of ["SCMP_ACT_ALLOW", "SCMP_ACT_LOG", "SCMP_ACT_NOTIFY", undefined]) {
      expect(
        () => validateSeccompProfile(JSON.stringify({ defaultAction: action, syscalls: [{}] })),
        String(action),
      ).toThrow(/must deny/);
    }
    expect(() =>
      validateSeccompProfile(JSON.stringify({ defaultAction: "SCMP_ACT_ERRNO", syscalls: [] })),
    ).toThrow(/no syscall rules/);
  });

  it("produces exactly the AppArmor and seccomp options, and nothing for no profile", () => {
    expect(securityProfileArgs(undefined, source())).toEqual([]);
    expect(securityProfileArgs("codex-bwrap", source())).toEqual([
      "--security-opt",
      `apparmor=${APPARMOR_PROFILE_NAME}`,
      "--security-opt",
      "seccomp=/install/security/argus-codex-worker.seccomp.json",
    ]);
  });

  it("fails closed on a missing file or a path that docker would split", () => {
    expect(() => securityProfileArgs("codex-bwrap", source(new Error("ENOENT")))).toThrow(
      /not readable/,
    );
    expect(() => securityProfileArgs("codex-bwrap", { ...source(), path: "/a,b/p.json" })).toThrow(
      /commas or whitespace/,
    );
    expect(() => securityProfileArgs("nope" as never, source())).toThrow(
      /unknown security profile/,
    );
  });
});

describe("daemon support", () => {
  it("requires AppArmor and seccomp to be reported", () => {
    expect(() => assertDaemonSupportsProfile(DAEMON_OK)).not.toThrow();
    expect(() => assertDaemonSupportsProfile('["name=seccomp,profile=builtin"]')).toThrow(
      /AppArmor/,
    );
    expect(() => assertDaemonSupportsProfile('["name=apparmor"]')).toThrow(/seccomp/);
    expect(() => assertDaemonSupportsProfile('["name=rootless","name=seccomp"]')).toThrow(
      /AppArmor/,
    );
    expect(() => assertDaemonSupportsProfile("not json")).toThrow(/security options/);
  });
});

describe("docker driver argv", () => {
  it("without a profile issues the unchanged hardening flags and never asks the daemon", async () => {
    await driver().prepare(fixtureSpec());
    const [args] = creates();
    expect(args).not.toContain("apparmor=argus-codex-worker");
    expect(args.join(" ")).not.toMatch(/apparmor|seccomp/);
    expect(cli.joined().some((c) => c.startsWith("info "))).toBe(false);
  });

  it("with the profile appends the options and keeps cap-drop and no-new-privileges", async () => {
    await driver().prepare(profiled());
    const [args] = creates();
    const joined = args.join(" ");
    expect(args).toContain("--cap-drop=ALL");
    expect(joined).toContain("--security-opt no-new-privileges");
    expect(joined).toContain("--security-opt apparmor=argus-codex-worker");
    expect(joined).toContain(
      "--security-opt seccomp=/install/security/argus-codex-worker.seccomp.json",
    );
    expect(args).toContain("--init");
    expect(joined).not.toMatch(/unconfined|--privileged|SYS_ADMIN|--cap-add/);
    // the daemon was asked before anything was created
    const order = cli.joined();
    expect(order.findIndex((c) => c.startsWith("info "))).toBeLessThan(
      order.findIndex((c) => c.startsWith("create ")),
    );
  });

  it.each([
    [
      "a daemon without AppArmor",
      () => (cli.responses[1] = { match: /^info /, output: '["name=seccomp"]' }),
      source(),
      /AppArmor/,
    ],
    [
      "an unreadable daemon",
      () =>
        (cli.responses[1] = {
          match: /^info /,
          throws: new Error("Cannot connect to the Docker daemon"),
        }),
      source(),
      /runtime-unavailable/,
    ],
    ["a missing seccomp file", () => {}, source(new Error("ENOENT")), /not readable/],
    [
      "a default-allow seccomp file",
      () => {},
      source(JSON.stringify({ defaultAction: "SCMP_ACT_ALLOW", syscalls: [{}] })),
      /must deny/,
    ],
    ["a malformed seccomp file", () => {}, source("{"), /not valid JSON/],
  ])("refuses to create for %s", async (_name, arrange, seccomp, message) => {
    arrange();
    await expect(driver(seccomp).prepare(profiled())).rejects.toThrow(message);
    expect(creates()).toEqual([]);
  });

  it("puts the profile on the agent only, never on the auxiliary container", async () => {
    const spec = fixtureSpecWithAux();
    spec.containers[0].securityProfile = "codex-bwrap";
    await driver().prepare(spec);
    const [aux, agent] = creates();
    expect(aux.join(" ")).not.toMatch(/apparmor|seccomp/);
    expect(agent.join(" ")).toContain("apparmor=argus-codex-worker");
  });

  it("validateSpec refuses unknown tokens, an auxiliary role and a driver without the capability", () => {
    const docker = driver().capabilities();
    expect(docker.securityProfiles).toEqual(["codex-bwrap"]);
    expect(() => validateSpec(profiled(), FIXTURE_POLICY, docker)).not.toThrow();
    const unknown = fixtureSpec();
    (unknown.containers[0] as { securityProfile?: string }).securityProfile = "unconfined";
    expect(() => validateSpec(unknown, FIXTURE_POLICY, docker)).toThrow(/not allowed/);
    const aux = fixtureSpecWithAux();
    aux.containers[1] = { ...fixtureAuxContainer(), securityProfile: "codex-bwrap" };
    expect(() => validateSpec(aux, FIXTURE_POLICY, docker)).toThrow(
      /not allowed on role egress-proxy/,
    );
    // no capabilities handle, or a driver that does not list the token: refused, never ignored
    expect(() => validateSpec(profiled(), FIXTURE_POLICY)).toThrow(/cannot realize/);
    const other: DriverCapabilities = { ...docker, securityProfiles: undefined };
    expect(() => validateSpec(profiled(), FIXTURE_POLICY, other)).toThrow(/cannot realize/);
    expect(() => validateSpec(fixtureSpec(), FIXTURE_POLICY, other)).not.toThrow();
  });
});

describe("backend without the capability", () => {
  const noProfiles = {} as { securityProfiles?: readonly string[] };
  it("the spawn gate refuses a profiled spec and leaves a default spec alone", () => {
    expect(() => assertDriverSupportsSecurityProfiles(noProfiles, profiled(), "vm")).toThrow(
      /driver 'vm' cannot realize/,
    );
    expect(() =>
      assertDriverSupportsSecurityProfiles({ securityProfiles: [] }, profiled()),
    ).toThrow(/cannot realize/);
    expect(() =>
      assertDriverSupportsSecurityProfiles(noProfiles, fixtureSpec(), "vm"),
    ).not.toThrow();
    expect(() =>
      assertDriverSupportsSecurityProfiles(driver().capabilities(), profiled(), "docker"),
    ).not.toThrow();
  });
});

describe("enforcement is checked on the daemon, not inferred from docker info", () => {
  const runCalls = () => cli.calls.filter((c) => c.args[0] === "run").map((c) => c.args);

  it("runs a disposable probe with the same flags and nothing else", async () => {
    await driver().prepare(profiled());
    const [probe] = runCalls();
    const joined = probe.join(" ");
    expect(probe.slice(-5)).toEqual(
      [
        "--entrypoint",
        "cat",
        "nanoclaw-agent:spike-p0",
        "/proc/self/attr/current",
        "/proc/self/status",
      ].slice(-5),
    );
    expect(joined).toContain("--network none");
    expect(probe).toContain("--cap-drop=ALL");
    expect(joined).toContain("--security-opt no-new-privileges");
    expect(joined).toContain("--security-opt apparmor=argus-codex-worker");
    expect(joined).toContain(
      "--security-opt seccomp=/install/security/argus-codex-worker.seccomp.json",
    );
    expect(joined).toContain("--pull=never");
    expect(joined).not.toMatch(
      / -v | -e |--env|--mount|--privileged|--cap-add|unconfined|SYS_ADMIN|--sysctl|docker\.sock/,
    );
    const order = cli.joined();
    expect(order.findIndex((c) => c.startsWith("run "))).toBeLessThan(
      order.findIndex((c) => c.startsWith("create ")),
    );
  });

  it.each([
    [
      "complain mode",
      "argus-codex-worker (complain)\nNoNewPrivs:\t1\nSeccomp:\t2\nCapEff:\t0\n",
      /not enforcing/,
    ],
    [
      "the docker default profile",
      "docker-default (enforce)\nNoNewPrivs:\t1\nSeccomp:\t2\nCapEff:\t0\n",
      /not enforcing/,
    ],
    [
      "an unconfined process",
      "unconfined\nNoNewPrivs:\t1\nSeccomp:\t2\nCapEff:\t0\n",
      /not enforcing/,
    ],
    ["an unreadable label", "", /not enforcing/],
    [
      "seccomp not applied",
      "argus-codex-worker (enforce)\nNoNewPrivs:\t1\nSeccomp:\t0\nCapEff:\t0\n",
      /did not show/,
    ],
    [
      "no-new-privileges not applied",
      "argus-codex-worker (enforce)\nNoNewPrivs:\t0\nSeccomp:\t2\nCapEff:\t0\n",
      /did not show/,
    ],
    [
      "capabilities left",
      "argus-codex-worker (enforce)\nNoNewPrivs:\t1\nSeccomp:\t2\nCapEff:\t0000000000000400\n",
      /did not show/,
    ],
  ])("refuses %s and creates nothing", async (_name, output, message) => {
    cli.responses[2] = { match: /^run /, output };
    await expect(driver().prepare(profiled())).rejects.toThrow(message);
    expect(cli.calls.some((c) => c.args[0] === "create")).toBe(false);
  });

  it("refuses when the probe container cannot start (profile not loaded)", async () => {
    cli.responses[2] = {
      match: /^run /,
      throws: new Error("runc: apparmor failed to apply profile: change_onexec: label not found"),
    };
    await expect(driver().prepare(profiled())).rejects.toThrow(
      /probe could not run.*not be loaded/,
    );
    expect(cli.calls.some((c) => c.args[0] === "create")).toBe(false);
  });

  it("caches a pass briefly and probes again after it expires", async () => {
    await driver().prepare(profiled());
    const second = profiled();
    second.key = { ...second.key, sessionId: "s2" };
    await driver().prepare(second);
    expect(runCalls()).toHaveLength(1);
    clock += 31_000;
    const third = profiled();
    third.key = { ...third.key, sessionId: "s3" };
    await driver().prepare(third);
    expect(runCalls()).toHaveLength(2);
  });

  it("a default spec never probes", async () => {
    await driver().prepare(fixtureSpec());
    expect(runCalls()).toEqual([]);
  });
});

describe("adopting an existing session container", () => {
  const SEP = " ARGUS-SEP ";
  const expectedOpts = (extra: string[] = []) => [
    "no-new-privileges",
    "apparmor=argus-codex-worker",
    `seccomp=${JSON.stringify(JSON.parse(GOOD_SECCOMP))}`,
    ...extra,
  ];
  const hostConfig = (
    o: Partial<{
      opts: string[];
      capDrop: unknown;
      capAdd: unknown;
      priv: boolean;
      aa: string;
      init: boolean;
    }> = {},
  ) =>
    [
      JSON.stringify(o.opts ?? expectedOpts()),
      JSON.stringify("capDrop" in o ? o.capDrop : ["ALL"]),
      JSON.stringify("capAdd" in o ? o.capAdd : null),
      JSON.stringify(o.priv ?? false),
      JSON.stringify(o.aa ?? "argus-codex-worker"),
      JSON.stringify(o.init ?? true),
    ].join(SEP);
  const existing = (config?: string) => {
    cli.responses = [
      ...(config === undefined ? [] : [{ match: /HostConfig\.SecurityOpt/, output: config }]),
      { match: /^inspect /, output: "spike|g1|s1" },
      { match: /^info /, output: DAEMON_OK },
      { match: /^run /, output: ENFORCE },
    ];
  };
  const destructive = () =>
    cli.calls.filter((c) => ["rm", "stop", "kill", "create", "start"].includes(c.args[0]));

  it("default: adopts as before, reading nothing about hardening", async () => {
    existing();
    await driver().prepare(fixtureSpec());
    expect(cli.calls.map((c) => c.args[0])).toEqual(["inspect"]);
  });

  it("opted-in: adopts a container really created with the profile", async () => {
    existing(hostConfig());
    await driver().prepare(profiled());
    expect(destructive()).toEqual([]);
  });

  it("reads only hardening fields, never the environment", async () => {
    existing(hostConfig());
    await driver().prepare(profiled());
    const fmt = cli.calls
      .map((c) => c.args.join(" "))
      .find((c) => c.includes("HostConfig.SecurityOpt"));
    expect(fmt).toBeDefined();
    expect(fmt).not.toMatch(/Config\.Env|\{\{json \.\}\}|\{\{json \.Config/);
  });

  it.each([
    [
      "a container created without the profile",
      hostConfig({ opts: ["no-new-privileges"], aa: "" }),
    ],
    [
      "a different AppArmor profile",
      hostConfig({
        opts: ["no-new-privileges", "apparmor=docker-default", expectedOpts()[2]],
        aa: "docker-default",
      }),
    ],
    [
      "unconfined AppArmor",
      hostConfig({
        opts: ["no-new-privileges", "apparmor=unconfined", expectedOpts()[2]],
        aa: "unconfined",
      }),
    ],
    [
      "a different seccomp profile",
      hostConfig({
        opts: [
          "no-new-privileges",
          "apparmor=argus-codex-worker",
          'seccomp={"defaultAction":"SCMP_ACT_ALLOW"}',
        ],
      }),
    ],
    [
      "seccomp=unconfined",
      hostConfig({
        opts: ["no-new-privileges", "apparmor=argus-codex-worker", "seccomp=unconfined"],
      }),
    ],
    ["no no-new-privileges", hostConfig({ opts: expectedOpts().slice(1) })],
    ["an extra security option", hostConfig({ opts: expectedOpts(["label=disable"]) })],
    ["capabilities not dropped", hostConfig({ capDrop: null })],
    ["an added capability", hostConfig({ capAdd: ["SYS_ADMIN"] })],
    ["privileged", hostConfig({ priv: true })],
    ["no init", hostConfig({ init: false })],
    ["unreadable host config", "garbage"],
  ])("rejects %s without touching it", async (_name, config) => {
    existing(config);
    await expect(driver().prepare(profiled())).rejects.toThrow(/not adopting|cannot (read|parse)/);
    expect(destructive()).toEqual([]);
  });

  it("rejects adoption while the profile is not enforcing", async () => {
    existing(hostConfig());
    cli.responses[3] = { match: /^run /, output: "argus-codex-worker (complain)\n" };
    await expect(driver().prepare(profiled())).rejects.toThrow(/not enforcing/);
    expect(destructive()).toEqual([]);
  });
});
