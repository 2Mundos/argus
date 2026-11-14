import { describe, expect, it } from "bun:test";

import {
  baseReceipt,
  EXIT_BLOCKED,
  exitCodeFor,
  isolationBlockers,
  measureIsolation,
  type Probe,
  providerPath,
  providerSourceProblems,
  runFailures,
} from "./installed-provider-preflight";

const DIGEST = `sha256:${"c".repeat(64)}`;
const STATUS =
  "Uid:\t1000\t1000\t1000\t1000\nCapEff:\t0000000000000000\nNoNewPrivs:\t1\nSeccomp:\t2\n";

function probe(overrides: Partial<Probe> = {}, files: Record<string, string> = {}): Probe {
  const all: Record<string, string> = {
    "/proc/self/status": STATUS,
    "/proc/self/attr/current": "argus-codex-worker (enforce)\n",
    ...files,
  };
  return {
    readFile: (f) => all[f] ?? null,
    which: (n) => (n === "bwrap" ? "/usr/bin/bwrap" : null),
    run: () => "bubblewrap 0.11.0\n",
    env: { ARGUS_IMAGE_DIGEST: DIGEST },
    ...overrides,
  };
}

describe("measureIsolation + isolationBlockers", () => {
  it("passes a measured, confined, non-root process with bwrap and a digest", () => {
    const iso = measureIsolation(probe());

    expect(iso).toEqual({
      imageDigest: DIGEST,
      uid: 1000,
      capEff: "0000000000000000",
      noNewPrivs: 1,
      seccomp: 2,
      apparmor: "argus-codex-worker (enforce)",
      bwrap: { path: "/usr/bin/bwrap", version: "bubblewrap 0.11.0" },
    });
    expect(isolationBlockers(iso)).toEqual([]);
  });

  it.each([
    ["bwrap missing", probe({ which: () => null }), /bwrap is not on PATH/],
    [
      "capabilities left",
      probe({}, { "/proc/self/status": STATUS.replace(/CapEff:.*/, "CapEff:\t00000000a80425fb") }),
      /CapEff is 00000000a80425fb/,
    ],
    [
      "NoNewPrivs off",
      probe({}, { "/proc/self/status": STATUS.replace("NoNewPrivs:\t1", "NoNewPrivs:\t0") }),
      /NoNewPrivs is 0/,
    ],
    [
      "no seccomp filter",
      probe({}, { "/proc/self/status": STATUS.replace("Seccomp:\t2", "Seccomp:\t0") }),
      /no seccomp filter/,
    ],
    ["digest unset", probe({ env: {} }), /image digest unknown/],
    ["digest not sha256", probe({ env: { ARGUS_IMAGE_DIGEST: "latest" } }), /image digest unknown/],
    [
      "uid 0",
      probe({}, { "/proc/self/status": STATUS.replace(/Uid:.*/, "Uid:\t0\t0\t0\t0") }),
      /uid 0/,
    ],
  ])("blocks when %s", (_name, p, reason) => {
    expect(isolationBlockers(measureIsolation(p)).join("; ")).toMatch(reason);
  });

  it("blocks on every field when /proc cannot be read, recording nulls", () => {
    const iso = measureIsolation(probe({ readFile: () => null }));

    expect(iso).toMatchObject({ uid: null, capEff: null, noNewPrivs: null, seccomp: null });
    expect(iso.apparmor).toBeNull();
    expect(isolationBlockers(iso)).toHaveLength(4);
  });

  it("ignores worker env flags claiming isolation", () => {
    const env = {
      ARGUS_IMAGE_DIGEST: DIGEST,
      ARGUS_ASSUME_ISOLATED: "1",
      ARGUS_BWRAP: "/usr/bin/bwrap",
      CapEff: "0000000000000000",
    };
    const iso = measureIsolation(probe({ env, which: () => null, readFile: () => null }));

    expect(iso.bwrap.path).toBeNull();
    expect(isolationBlockers(iso)).toContain("bwrap is not on PATH");
  });
});

describe("providerPath", () => {
  it("resolves the provider under an installed runner root", () => {
    expect(providerPath("/srv/nanoclaw/container/agent-runner")).toBe(
      "/srv/nanoclaw/container/agent-runner/src/providers/codex-app-server.ts",
    );
  });

  it.each(["/app", "/app/", "/app/src/.."])("refuses the live runtime %s", (root) => {
    expect(providerPath(root)).toBeNull();
  });

  it("does not mistake a sibling of /app for it", () => {
    expect(providerPath("/application")).not.toBeNull();
  });
});

describe("providerSourceProblems", () => {
  const patched = [
    "import { establishCodexHookTrust } from './codex-hook-trust.js';",
    "export const CODEX_APP_SERVER_ARGS = ['app-server', '--listen', 'stdio://'] as const;",
    "  const trust = await establishCodexHookTrust((m, p) => rpc(m, p), { cwd });",
  ].join("\n");

  it("accepts the patched provider", () => {
    expect(providerSourceProblems(patched)).toEqual([]);
  });

  it.each([
    [patched.replace("['app-server'", "['--dangerously-bypass-hook-trust', 'app-server'")],
    [`${patched}\nconfig: { bypass_hook_trust: true },`],
  ])("refuses a bypass string", (text) => {
    expect(providerSourceProblems(text).join()).toMatch(/contains/);
  });

  it("refuses a provider that does not import or call the trust overlay", () => {
    expect(providerSourceProblems("export const x = 1;")).toEqual([
      "does not import codex-hook-trust",
      "does not call establishCodexHookTrust",
    ]);
  });
});

describe("receipts", () => {
  const iso = measureIsolation(probe({ which: () => null }));

  it("a blocked receipt carries only measured fields and exits 3", () => {
    const r = baseReceipt(iso, null);

    expect(r.status).toBe("blocked");
    expect(exitCodeFor(r.status)).toBe(EXIT_BLOCKED);
    for (const key of [
      "spawnArgs",
      "sandbox",
      "approvalPolicy",
      "runtime",
      "trustStatuses",
      "commandExits",
      "hookEvents",
      "okFile",
      "negativeMarker",
    ] as const)
      expect(r[key]).toBeNull();
  });

  it("a run passes only with no bypass, trusted hooks, the benign file and no marker", () => {
    const passing = {
      ...baseReceipt(iso, null),
      spawnArgs: ["codex", "app-server", "--listen", "stdio://"],
      trustStatuses: { a: "trusted", b: "trusted" },
      okFile: true,
      negativeMarker: false,
    };
    expect(runFailures(passing)).toEqual([]);

    expect(runFailures({ ...passing, negativeMarker: true })).toEqual([
      "negative control ran: guard did not deny",
    ]);
    expect(runFailures({ ...passing, trustStatuses: { a: "trusted", b: "modified" } })).toEqual([
      "expected hooks not all trusted",
    ]);
    expect(runFailures({ ...passing, trustStatuses: {} })).toEqual([
      "expected hooks not all trusted",
    ]);
    expect(
      runFailures({ ...passing, spawnArgs: ["codex", "--dangerously-bypass-hook-trust"] }),
    ).toEqual(["spawned with --dangerously-bypass-hook-trust"]);
    expect(runFailures({ ...passing, okFile: null, negativeMarker: null })).toEqual([
      "benign command did not run",
      "negative control not observed",
    ]);
  });
});
