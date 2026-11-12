import { randomUUID } from "node:crypto";
import {
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  type IntegrationState,
  type IntegrationsSource,
  installTaskIntegrations,
  integrationLines,
  KUBECONFIG_FILE,
  type KubernetesCredential,
  recheckTaskIntegrations,
  removeTaskIntegrations,
  renderKubeconfig,
  sweepTaskIntegrations,
  TASK_INTEGRATIONS_DIR,
  taskIntegrationsPath,
} from "./argus-integrations.js";

const SECRET = "fake-token-NEVER-IN-A-PROMPT";
const dirs: string[] = [];
afterEach(() => {
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
});

function group(): string {
  const d = mkdtempSync(path.join(os.tmpdir(), "argus-integrations-"));
  dirs.push(d);
  return d;
}

class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(`${code}: the API message may echo ${SECRET}`);
  }
}

/** An in-memory Argus: one integration per entry, current version and readiness. */
function fakeArgus(
  entries: Array<Partial<IntegrationState> & { token?: string; ca_data?: string | null }> = [{}],
) {
  const rows = entries.map((e, i) => ({
    integration_id: e.integration_id ?? `int-${i}`,
    name: e.name ?? `cluster-${i}`,
    namespace: e.namespace ?? "team-a",
    secret_version: e.secret_version ?? 1,
    expires_at: e.expires_at ?? "2026-12-01T00:00:00Z",
    readiness: e.readiness ?? "ready",
    token: e.token ?? `${SECRET}-${i}`,
    ca_data: e.ca_data === undefined ? Buffer.from("PEM").toString("base64") : e.ca_data,
  }));
  const calls: string[] = [];
  let statusError: unknown = null;
  let raceOnce = false;
  const source: IntegrationsSource = {
    status: async (taskId, attempt) => {
      calls.push(`status:${taskId}:${attempt}`);
      if (statusError) throw statusError;
      return rows.map(({ token: _t, ca_data: _c, ...s }) => ({ ...s }));
    },
    credential: async (taskId, id, attempt, version) => {
      calls.push(`credential:${id}:${attempt}:${version}`);
      const r = rows.find((x) => x.integration_id === id)!;
      if (raceOnce) {
        raceOnce = false;
        r.secret_version += 1;
        r.token = `${SECRET}-rotated`;
      }
      if (version !== r.secret_version) throw new ApiError(409, "integration_changed");
      if (r.readiness !== "ready") throw new ApiError(409, `integration_${r.readiness}`);
      return {
        integration_id: r.integration_id,
        name: r.name,
        server: "https://k8s.example.test:6443",
        namespace: r.namespace,
        ca_data: r.ca_data,
        access: "read_only",
        token: r.token,
        secret_version: r.secret_version,
        expires_at: r.expires_at,
      } satisfies KubernetesCredential;
    },
  };
  return {
    rows,
    calls,
    source,
    failStatus: (err: unknown) => {
      statusError = err;
    },
    raceNextFetch: () => {
      raceOnce = true;
    },
  };
}

const TASK = "0b5a3a6e-8f4c-4d0e-9a51-7c1f2b3d4e5f";

function install(
  groupDir: string,
  source: IntegrationsSource,
  over: Partial<Parameters<typeof installTaskIntegrations>[0]> = {},
) {
  return installTaskIntegrations({
    groupDir,
    taskId: TASK,
    attempt: 1,
    selected: ["int-0"],
    engineProvider: "anthropic",
    executorProvider: null,
    source,
    ...over,
  });
}

function hostPath(groupDir: string, containerPath: string): string {
  return path.join(groupDir, path.relative("/workspace/agent", containerPath));
}

describe("installTaskIntegrations", () => {
  it("writes a private, namespace-pinned kubeconfig in a fresh attempt directory", async () => {
    const g = group();
    const argus = fakeArgus([{}, { namespace: "team-b", ca_data: null }]);
    const got = await install(g, argus.source, { selected: ["int-0", "int-1"] });
    expect(got.ok).toBe(true);
    if (!got.ok || !got.installed) throw new Error("not installed");
    const { installed } = got;
    expect(installed.kubeconfig).toMatch(
      new RegExp(`^/workspace/agent/${TASK_INTEGRATIONS_DIR}/${TASK}/1-[0-9a-f-]{36}/kubeconfig$`),
    );
    const file = hostPath(g, installed.kubeconfig);
    expect(statSync(file).mode & 0o777).toBe(0o600);
    expect(statSync(path.dirname(file)).mode & 0o777).toBe(0o700);
    expect(statSync(taskIntegrationsPath(g, TASK)).mode & 0o777).toBe(0o700);
    expect(statSync(path.join(g, TASK_INTEGRATIONS_DIR)).mode & 0o777).toBe(0o700);
    // Only the kubeconfig: no temp file left behind.
    expect(readdirSync(path.dirname(file))).toEqual([KUBECONFIG_FILE]);

    const cfg = JSON.parse(readFileSync(file, "utf8"));
    expect(cfg["current-context"]).toBe("cluster-0");
    expect(cfg.contexts.map((c: any) => c.context.namespace)).toEqual(["team-a", "team-b"]);
    expect(cfg.clusters[0].cluster["certificate-authority-data"]).toBeTruthy();
    expect(cfg.clusters[1].cluster).toEqual({ server: "https://k8s.example.test:6443" });
    expect(readFileSync(file, "utf8")).not.toMatch(/insecure/);
    expect(cfg.users[0].user.token).toBe(`${SECRET}-0`);
    expect(argus.calls).toEqual([
      `status:${TASK}:1`,
      "credential:int-0:1:1",
      "credential:int-1:1:1",
    ]);
  });

  it("tells the model the path, namespace and expiry, never the token", async () => {
    const g = group();
    const got = await install(g, fakeArgus().source);
    if (!got.ok || !got.installed) throw new Error("not installed");
    const text = integrationLines(got.installed).join("\n");
    expect(text).toContain(got.installed.kubeconfig);
    expect(text).toContain("namespace team-a");
    expect(text).toContain("2026-12-01T00:00:00Z");
    expect(text).toContain("kubectl config view --raw");
    expect(text).not.toContain(SECRET);
    expect(JSON.stringify(got.installed)).not.toContain(SECRET);
  });

  it("installs nothing for a task without integrations and clears a stale directory", async () => {
    const g = group();
    const argus = fakeArgus();
    const first = await install(g, argus.source);
    if (!first.ok || !first.installed) throw new Error("not installed");
    const got = await install(g, argus.source, { attempt: 2, selected: [] });
    expect(got).toEqual({ ok: true, installed: null });
    expect(existsSync(taskIntegrationsPath(g, TASK))).toBe(false);
  });

  it("gives a retry a distinct directory and removes the previous attempt's", async () => {
    const g = group();
    const argus = fakeArgus();
    const one = await install(g, argus.source);
    const two = await install(g, argus.source, { attempt: 2 });
    if (!one.ok || !one.installed || !two.ok || !two.installed) throw new Error("not installed");
    expect(two.installed.dir).not.toBe(one.installed.dir);
    expect(existsSync(one.installed.dir)).toBe(false);
    expect(readdirSync(taskIntegrationsPath(g, TASK))).toEqual([path.basename(two.installed.dir)]);
  });

  it.each([
    ["revoked", "env_integration_unavailable"],
    ["expired", "env_integration_unavailable"],
    ["unvalidated", "env_integration_unavailable"],
    ["secret_missing", "env_integration_unavailable"],
    ["store_unavailable", "env_integration_unavailable"],
  ] as const)("fails closed when an integration is %s", async (readiness, reason) => {
    const g = group();
    const argus = fakeArgus([{}, { readiness }]);
    const got = await install(g, argus.source, { selected: ["int-0", "int-1"] });
    expect(got).toMatchObject({ ok: false, reason });
    if (got.ok) throw new Error("installed");
    expect(got.error).toContain(`cluster-1 (${readiness})`);
    expect(argus.calls.filter((c) => c.startsWith("credential"))).toEqual([]);
    expect(existsSync(taskIntegrationsPath(g, TASK))).toBe(false);
  });

  it.each([
    [{ engineProvider: "openai" }, "engine"],
    [{ engineProvider: null }, "engine"],
    [{ executorProvider: "openai" }, "executor"],
  ])("refuses an engine it cannot confine (%o) before fetching", async (over, role) => {
    const g = group();
    const argus = fakeArgus();
    const got = await install(g, argus.source, over);
    expect(got).toMatchObject({ ok: false, reason: "env_integration_unsupported" });
    if (got.ok) throw new Error("installed");
    expect(got.error).toContain(`Claude ${role}`);
    expect(argus.calls).toEqual([]);
  });

  it("maps API refusals to reasons with the code only, never the message", async () => {
    for (const [status, code, reason] of [
      [403, "not_assignee", "env_integration_unavailable"],
      [409, "stale_attempt", "env_integration_unavailable"],
      [403, "integration_not_selected", "env_integration_invalid"],
      [409, "integration_engine_unsupported", "env_integration_unsupported"],
      [503, "secret_store_unavailable", "env_integration_unavailable"],
    ] as const) {
      const g = group();
      const argus = fakeArgus();
      argus.failStatus(new ApiError(status, code));
      const got = await install(g, argus.source);
      expect(got).toEqual({ ok: false, reason, error: `Argus refused: ${code}` });
    }
    const g = group();
    const argus = fakeArgus();
    argus.failStatus(new TypeError(`fetch failed for ${SECRET}`));
    expect(await install(g, argus.source)).toEqual({
      ok: false,
      reason: "env_integration_unavailable",
      error: "Argus unreachable",
    });
  });

  it("re-reads after a rotation races the fetch and installs the new version", async () => {
    const g = group();
    const argus = fakeArgus();
    argus.raceNextFetch();
    const got = await install(g, argus.source);
    if (!got.ok || !got.installed) throw new Error(JSON.stringify(got));
    expect(got.installed.integrations[0].secret_version).toBe(2);
    expect(readFileSync(hostPath(g, got.installed.kubeconfig), "utf8")).toContain(
      `${SECRET}-rotated`,
    );
  });

  it.each([
    ["server", { server: "http://k8s.example.test" }],
    ["server", { server: "https://user:pw@k8s.example.test" }],
    ["namespace", { namespace: "../etc" }],
    ["name", { name: "../../x" }],
    ["ca_data", { ca_data: "not base64!" }],
    ["token", { token: "a b" }],
    ["version", { secret_version: 7 }],
  ])("refuses a credential with an unsafe %s", async (_what, bad) => {
    const g = group();
    const argus = fakeArgus();
    const source: IntegrationsSource = {
      ...argus.source,
      credential: async (...a) => ({ ...(await argus.source.credential(...a)), ...bad }),
    };
    const got = await install(g, source);
    expect(got).toMatchObject({ ok: false, reason: "env_integration_invalid" });
    expect(JSON.stringify(got)).not.toContain(SECRET);
    expect(existsSync(taskIntegrationsPath(g, TASK))).toBe(false);
  });

  it("refuses a task id that is not a canonical UUID", async () => {
    const got = await install(group(), fakeArgus().source, { taskId: "../../escape" });
    expect(got).toMatchObject({ ok: false, reason: "env_integration_invalid" });
  });

  it("never writes through a symlinked root or task directory", async () => {
    for (const link of ["root", "task"] as const) {
      const g = group();
      const outside = group();
      if (link === "root") symlinkSync(outside, path.join(g, TASK_INTEGRATIONS_DIR));
      else {
        mkdirSync(path.join(g, TASK_INTEGRATIONS_DIR), { mode: 0o700 });
        symlinkSync(outside, taskIntegrationsPath(g, TASK));
      }
      const got = await install(g, fakeArgus().source);
      expect(got).toMatchObject({ ok: false, reason: "env_integration_invalid" });
      expect(readdirSync(outside)).toEqual([]);
    }
  });

  it("refuses a shared (group-readable) task directory", async () => {
    const g = group();
    mkdirSync(taskIntegrationsPath(g, TASK), { recursive: true, mode: 0o755 });
    const got = await install(g, fakeArgus().source);
    expect(got).toMatchObject({ ok: false, reason: "env_integration_invalid" });
  });

  it("removes a planted symlink in the task directory instead of following it", async () => {
    const g = group();
    const outside = group();
    writeFileSync(path.join(outside, "keep"), "x");
    mkdirSync(taskIntegrationsPath(g, TASK), { recursive: true, mode: 0o700 });
    symlinkSync(outside, path.join(taskIntegrationsPath(g, TASK), "1-planted"));
    const got = await install(g, fakeArgus().source);
    expect(got.ok).toBe(true);
    expect(readFileSync(path.join(outside, "keep"), "utf8")).toBe("x");
  });
});

describe("recheckTaskIntegrations", () => {
  async function installed(g: string, argus: ReturnType<typeof fakeArgus>) {
    const got = await install(g, argus.source);
    if (!got.ok || !got.installed) throw new Error("not installed");
    return got.installed;
  }

  it("keeps the file while everything is ready and unchanged", async () => {
    const g = group();
    const argus = fakeArgus();
    const inst = await installed(g, argus);
    const r = await recheckTaskIntegrations(g, inst, argus.source);
    expect(r.state).toBe("kept");
    expect(existsSync(hostPath(g, inst.kubeconfig))).toBe(true);
  });

  it.each(["revoked", "expired", "store_unavailable"] as const)(
    "removes the file when the integration becomes %s mid-attempt",
    async (readiness) => {
      const g = group();
      const argus = fakeArgus();
      const inst = await installed(g, argus);
      argus.rows[0].readiness = readiness;
      const r = await recheckTaskIntegrations(g, inst, argus.source);
      expect(r).toMatchObject({ state: "removed", installed: null });
      expect(r.why).toContain(readiness);
      expect(existsSync(taskIntegrationsPath(g, TASK))).toBe(false);
    },
  );

  it("removes the file when the lease is gone, keeps it on a transient failure", async () => {
    const g = group();
    const argus = fakeArgus();
    const inst = await installed(g, argus);
    argus.failStatus(new ApiError(503, "unavailable"));
    expect((await recheckTaskIntegrations(g, inst, argus.source)).state).toBe("kept");
    argus.failStatus(new TypeError("fetch failed"));
    expect((await recheckTaskIntegrations(g, inst, argus.source)).state).toBe("kept");
    argus.failStatus(new ApiError(409, "stale_attempt"));
    const r = await recheckTaskIntegrations(g, inst, argus.source);
    expect(r).toMatchObject({ state: "removed", why: "stale_attempt" });
    expect(existsSync(inst.dir)).toBe(false);
  });

  it("replaces the file atomically in place after a rotation", async () => {
    const g = group();
    const argus = fakeArgus();
    const inst = await installed(g, argus);
    const file = hostPath(g, inst.kubeconfig);
    const before = lstatSync(file).ino;
    argus.rows[0].secret_version = 2;
    argus.rows[0].token = `${SECRET}-v2`;
    const r = await recheckTaskIntegrations(g, inst, argus.source);
    expect(r.state).toBe("refreshed");
    expect(r.installed?.kubeconfig).toBe(inst.kubeconfig);
    expect(r.installed?.integrations[0].secret_version).toBe(2);
    expect(readFileSync(file, "utf8")).toContain(`${SECRET}-v2`);
    expect(lstatSync(file).ino).not.toBe(before);
    expect(statSync(file).mode & 0o777).toBe(0o600);
    expect(readdirSync(path.dirname(file))).toEqual([KUBECONFIG_FILE]);
  });

  it("removes the file when the selection changes under the attempt", async () => {
    const g = group();
    const argus = fakeArgus();
    const inst = await installed(g, argus);
    argus.rows.push({ ...argus.rows[0], integration_id: "int-9", name: "late" });
    const r = await recheckTaskIntegrations(g, inst, argus.source);
    expect(r.state).toBe("removed");
  });
});

describe("cleanup", () => {
  it("removeTaskIntegrations is idempotent and leaves other tasks alone", async () => {
    const g = group();
    const other = randomUUID();
    const argus = fakeArgus();
    await install(g, argus.source);
    await install(g, argus.source, { taskId: other });
    await removeTaskIntegrations(g, TASK);
    await removeTaskIntegrations(g, TASK);
    expect(existsSync(taskIntegrationsPath(g, TASK))).toBe(false);
    expect(existsSync(taskIntegrationsPath(g, other))).toBe(true);
    await removeTaskIntegrations(group(), TASK); // no root at all
  });

  it("sweepTaskIntegrations empties the root at startup", async () => {
    const g = group();
    await install(g, fakeArgus().source);
    await sweepTaskIntegrations(g);
    expect(readdirSync(path.join(g, TASK_INTEGRATIONS_DIR))).toEqual([]);
    await sweepTaskIntegrations(group());
  });
});

it("renderKubeconfig never emits insecure-skip-tls-verify", () => {
  const text = renderKubeconfig([
    {
      integration_id: "i",
      name: "c",
      server: "https://x.test",
      namespace: "ns",
      ca_data: null,
      access: "read_only",
      token: "t",
      secret_version: 1,
      expires_at: null,
    },
  ]);
  expect(text).not.toMatch(/insecure/i);
  expect(JSON.parse(text).contexts[0].context.namespace).toBe("ns");
});
