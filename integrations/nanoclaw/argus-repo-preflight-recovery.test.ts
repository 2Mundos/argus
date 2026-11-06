/**
 * Missing-injection recovery of the repository preflight (failure-classes spec 3.3.1), offline.
 * Decision table over `runRepositoryPreflight` with stubbed probe, rearm and evidence source,
 * the real gitea-api.mjs helper against a local fake forge, and rearm idempotency.
 */
import { createServer, request as httpRequest, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  type AccountLease,
  applyRepositoryLease,
  OneCliClient,
  type RepositoryAssignmentCheck,
} from "./argus-accounts.js";
import {
  assertPreflightPositive,
  type InjectionEvidence,
  type PreflightEvidenceEvent,
  type ProbeEvidence,
  REARM_MAX_ATTEMPTS,
  RepositoryPreflightError,
  runRepositoryPreflight,
  unavailableInjectionEvidence,
} from "./argus-repo-preflight.js";
import { main as giteaMain, preflightEvidence } from "./gitea-api.mjs";

const REPO = "https://git.example.test/acme/widget";
const IDENTITY = REPO;
const AGENT = "ag-1";
const LEASE = { lease_id: "lease-1", provider: "gitea", role: "repository" as const };
const assigned = (): RepositoryAssignmentCheck => ({
  agent: "found",
  secretMode: "selective",
  rest: { state: "assigned", scope: "match" },
  git: { state: "assigned", scope: "match" },
});
const proven: ProbeEvidence = {
  helper_error: null,
  http_status: 200,
  permissions: "present",
  push: true,
  visibility: "private",
  authentication: "proven",
};
const notFound: ProbeEvidence = {
  helper_error: "not_found",
  http_status: 404,
  permissions: "absent",
  push: null,
  visibility: "unknown",
  authentication: "unknown",
};
const fail404 = (label = "404") => new RepositoryPreflightError("unknown", label, notFound);
const missingFor = (over: Partial<InjectionEvidence> = {}): InjectionEvidence =>
  ({ state: "missing", agent: AGENT, repository: IDENTITY, ...over }) as InjectionEvidence;

function run(over: Partial<Parameters<typeof runRepositoryPreflight>[0]> = {}) {
  const events: PreflightEvidenceEvent[] = [];
  const rearm = vi.fn(async () => {});
  const probe = vi.fn(async (): Promise<ProbeEvidence | void> => proven);
  const done = runRepositoryPreflight({
    taskId: "t1",
    attempt: 1,
    repositoryUrl: REPO,
    access: "write",
    lease: LEASE,
    verifyAssignment: async () => assigned(),
    agentIdentifier: AGENT,
    probe,
    rearm,
    backoff: async () => {},
    emit: (e) => {
      events.push(structuredClone(e));
    },
    ...over,
  });
  return { done, events, rearm, probe };
}

describe("private-repository 404 recovery needs independent evidence", () => {
  it("rearms on independent missing-injection evidence, then needs a proven probe", async () => {
    const source = vi.fn(async () => missingFor());
    const probe = vi.fn().mockRejectedValueOnce(fail404()).mockResolvedValueOnce(proven);
    const { done, events, rearm } = run({ probe, injectionEvidence: source });
    expect(await done).toMatchObject({ outcome: "ok", reason: null });
    expect(source).toHaveBeenCalledWith({ agent: AGENT, repository: IDENTITY });
    expect(rearm).toHaveBeenCalledTimes(1);
    expect(events[0]).toMatchObject({
      outcome: "ok",
      injection: "observed",
      independent_evidence: "missing",
      rearm_attempts: 1,
    });
  });

  it("a post-rearm probe that does not prove authentication is not ok", async () => {
    const probe = vi
      .fn()
      .mockRejectedValueOnce(fail404())
      .mockResolvedValueOnce({ ...proven, authentication: "unknown", push: null });
    const { done, events } = run({ probe, injectionEvidence: async () => missingFor() });
    expect(await done).toMatchObject({ outcome: "unknown", reason: "env_repository_unreachable" });
    expect(events[0].diagnosis).toBe("rearm_unproven");
  });

  it("a genuine 404 with no evidence source stays unknown and names the missing source", async () => {
    const probe = vi.fn().mockRejectedValue(fail404());
    const { done, events, rearm } = run({ probe });
    const result = await done;
    expect(result.outcome).toBe("unknown");
    expect(result.error).toMatch(/independent injection evidence is unavailable/);
    expect(rearm).not.toHaveBeenCalled();
    expect(events[0]).toMatchObject({
      diagnosis: "injection_unverifiable",
      independent_evidence: "unavailable",
      injection: "unknown",
    });
  });

  it("the production source is unavailable, so a 404 stays fail-closed", async () => {
    const probe = vi.fn().mockRejectedValue(fail404());
    const { done, rearm } = run({ probe, injectionEvidence: unavailableInjectionEvidence });
    expect((await done).outcome).toBe("unknown");
    expect(rearm).not.toHaveBeenCalled();
    expect(probe).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["unavailable", async () => ({ state: "unavailable" }) as InjectionEvidence, "unavailable"],
    ["injection observed", async () => missingFor({ state: "observed" }), "observed"],
    ["inconclusive", async () => missingFor({ state: "inconclusive" }), "inconclusive"],
    ["another agent", async () => missingFor({ agent: "ag-2" } as never), "mismatch"],
    [
      "another repository",
      async () => missingFor({ repository: `${REPO}-x` } as never),
      "mismatch",
    ],
    [
      "a throwing source",
      async () => {
        throw new Error("boom");
      },
      "error",
    ],
  ])("%s keeps a 404 fail-closed without a rearm", async (_l, source, seen) => {
    const probe = vi.fn().mockRejectedValue(fail404());
    const { done, events, rearm } = run({ probe, injectionEvidence: source });
    expect((await done).outcome).toBe("unknown");
    expect(rearm).not.toHaveBeenCalled();
    expect(probe).toHaveBeenCalledTimes(1);
    expect(events[0].independent_evidence).toBe(seen);
  });

  it("a rearm that throws stops with a distinct diagnosis and the preserved probe evidence", async () => {
    const probe = vi.fn().mockRejectedValue(fail404());
    const rearm = vi.fn().mockRejectedValue(new Error("OneCLI 500 token=abc"));
    const { done, events } = run({ probe, rearm, injectionEvidence: async () => missingFor() });
    const result = await done;
    expect(result.outcome).toBe("unknown");
    expect(result.error).toMatch(/could not be republished/);
    expect(result.error).not.toContain("abc");
    expect(events[0]).toMatchObject({ diagnosis: "rearm_failed", rearmed: false });
    expect(events[0].probe).toMatchObject({ http_status: 404 });
    expect(probe).toHaveBeenCalledTimes(1);
  });

  it("a persistent failure is capped at REARM_MAX_ATTEMPTS and ends blocked, not requeued", async () => {
    const source = vi.fn(async () => missingFor());
    const probe = vi.fn().mockRejectedValue(fail404());
    const { done, events, rearm } = run({ probe, injectionEvidence: source });
    const result = await done;
    expect(rearm).toHaveBeenCalledTimes(REARM_MAX_ATTEMPTS);
    expect(probe).toHaveBeenCalledTimes(REARM_MAX_ATTEMPTS + 1);
    expect(result).toMatchObject({ outcome: "unknown", reason: "env_repository_unreachable" });
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      diagnosis: "rearm_exhausted",
      rearm_attempts: REARM_MAX_ATTEMPTS,
    });
  });

  it("an actual 403 is forge_denied and never consults the evidence source", async () => {
    const source = vi.fn(async () => missingFor());
    const probe = vi.fn().mockRejectedValue(
      new RepositoryPreflightError("forge_denied", "403", {
        ...notFound,
        helper_error: "forbidden",
        http_status: 403,
      }),
    );
    const { done, events, rearm } = run({ probe, injectionEvidence: source });
    expect((await done).outcome).toBe("forge_denied");
    expect(source).not.toHaveBeenCalled();
    expect(rearm).not.toHaveBeenCalled();
    expect(events[0].diagnosis).toBe("forge_denied");
  });

  it("insufficient permission on a proven credential is a denial, not a rearm", async () => {
    const probe = vi.fn().mockRejectedValue(
      new RepositoryPreflightError("forge_denied", "no push", {
        ...proven,
        helper_error: "policy_denied",
        push: false,
      }),
    );
    const source = vi.fn(async () => missingFor());
    const { done, rearm } = run({ probe, injectionEvidence: source });
    expect((await done).outcome).toBe("forge_denied");
    expect(source).not.toHaveBeenCalled();
    expect(rearm).not.toHaveBeenCalled();
  });

  it("a stale or revoked lease is assignment_missing with no probe and no evidence read", async () => {
    const source = vi.fn(async () => missingFor());
    const stale = run({ lease: null, injectionEvidence: source });
    expect((await stale.done).outcome).toBe("assignment_missing");
    const revoked = run({
      injectionEvidence: source,
      verifyAssignment: async () => ({ ...assigned(), agent: "missing" }),
    });
    expect((await revoked.done).outcome).toBe("assignment_missing");
    expect(stale.probe).not.toHaveBeenCalled();
    expect(revoked.probe).not.toHaveBeenCalled();
    expect(source).not.toHaveBeenCalled();
  });

  it("a wrong repository or provider in the verdict stays fail-closed", () => {
    const ok = {
      ok: true,
      provider: "gitea",
      repository: `${REPO}-other`,
      authenticated: true,
      write: true,
      allowlist_refuses_other_host: true,
      allowlist_refuses_sibling: true,
    };
    expect(() =>
      assertPreflightPositive(
        { exitCode: 0, verdict: ok },
        { provider: "gitea", repository: REPO },
      ),
    ).toThrow(/different repository/);
  });
});

describe("rearm is idempotent and keeps the secret scope exact", () => {
  it("concurrent duplicate applies leave the same host/path-exact secrets and assignments", async () => {
    const secrets: Array<Record<string, unknown>> = [];
    const puts: string[][] = [];
    const fetchImpl = (async (url: string, init: RequestInit) => {
      const p = new URL(url).pathname.replace(/^\/api/, "");
      const body = init.body ? JSON.parse(init.body as string) : undefined;
      const m = init.method ?? "GET";
      if (p === "/agents" && m === "GET") return Response.json([{ id: "a", identifier: "w" }]);
      if (p === "/secrets" && m === "GET") return Response.json(secrets.map((s) => ({ ...s })));
      if (p === "/secrets" && m === "POST") {
        const s = { id: `s${secrets.length}`, ...body };
        secrets.push(s);
        return Response.json(s);
      }
      const patch = /^\/secrets\/(.+)$/.exec(p);
      if (patch && m === "PATCH") {
        Object.assign(secrets.find((s) => s.id === patch[1]) as object, body);
        return Response.json({});
      }
      if (p === "/agents/a/secrets" && m === "PUT") puts.push(body.secretIds);
      return Response.json({ success: true });
    }) as typeof fetch;
    const onecli = new OneCliClient("http://127.0.0.1:10254", fetchImpl);
    const lease = {
      lease_id: "l",
      account_id: "x",
      name: "r",
      provider: "gitea",
      kind: "api_key",
      role: "repository",
      credential: { username: "u", token: "dummy-token" },
    } as AccountLease;
    const target = { identifier: "w", name: "w" };
    await applyRepositoryLease(onecli, target, lease, REPO, [REPO], []);
    const before = structuredClone(secrets);
    await Promise.all([
      applyRepositoryLease(onecli, target, lease, REPO, [REPO], []),
      applyRepositoryLease(onecli, target, lease, REPO, [REPO], []),
    ]);
    expect(secrets).toEqual(before);
    expect(secrets.map((s) => s.pathPattern).sort()).toEqual([
      "/acme/widget.git/*",
      "/api/v1/repos/acme/widget/*",
    ]);
    expect(new Set(puts.map((ids) => ids.join()))).toEqual(new Set(["s0,s1"]));
    // A repository outside the allowlist is never published by a rearm.
    await expect(
      applyRepositoryLease(
        onecli,
        target,
        lease,
        REPO,
        ["https://git.example.test/acme/other"],
        [],
      ),
    ).rejects.toThrow();
  });
});

describe("real helper against a local fake forge (private repo: 404 anonymous, 200 with a token)", () => {
  let server: Server | undefined;
  afterEach(() => new Promise<void>((r) => (server ? server.close(() => r()) : r())));

  it("404 stays unknown without evidence; rearm + injection yields ok", async () => {
    const FAKE = "token fake-credential";
    server = createServer((req, res) => {
      res.setHeader("content-type", "application/json");
      if (req.headers.authorization !== FAKE) {
        res.statusCode = 404;
        return res.end(JSON.stringify({ message: "Not Found" }));
      }
      res.end(
        JSON.stringify({
          full_name: "acme/widget",
          private: true,
          permissions: { pull: true, push: true },
        }),
      );
    });
    await new Promise<void>((r) => server!.listen(0, "127.0.0.1", r));
    const port = (server.address() as AddressInfo).port;
    // Stands in for the gateway: injects the header only once the "policy" is published.
    let published = false;
    const gateway = (async (url: string, init: RequestInit) => {
      const headers: Record<string, string> = {};
      new Headers(init.headers).forEach((v, k) => {
        headers[k] = v;
      });
      if (published) headers.authorization = FAKE;
      // node:http, not fetch: a sandbox proxy in the environment must not sit in between.
      return new Promise<Response>((resolve, reject) => {
        const req = httpRequest(
          { host: "127.0.0.1", port, path: new URL(url).pathname, method: "GET", headers },
          (res) => {
            const chunks: Buffer[] = [];
            res.on("data", (c) => chunks.push(c));
            res.on("end", () =>
              resolve(new Response(Buffer.concat(chunks), { status: res.statusCode })),
            );
          },
        );
        req.on("error", reject);
        req.end();
      });
    }) as typeof fetch;
    // /preflight is not writable offline, so the verdict file is rebuilt from the helper's own
    // output the way it writes it: stdout on success, `preflightEvidence` + stderr error on failure.
    const probe = async () => {
      let out = "";
      let err = "";
      const exitCode = await giteaMain(
        ["repo-preflight", "--repository", REPO, "--access", "write"],
        { ARGUS_REPOSITORIES_JSON: JSON.stringify({ r: REPO }) },
        {
          fetch: gateway,
          stdout: { write: (c: string) => (out += c) },
          stderr: { write: (c: string) => (err += c) },
        },
      );
      const error = err ? JSON.parse(err).error : null;
      const verdict = out
        ? JSON.parse(out)
        : { ok: false, evidence: preflightEvidence(null, error?.status), error };
      return assertPreflightPositive(
        { exitCode, verdict },
        { provider: "gitea", repository: REPO },
      );
    };
    // No evidence source: the real helper's 404 verdict is unknown and nothing is re-armed.
    const bare = run({ probe: probe as never });
    expect(await bare.done).toMatchObject({ outcome: "unknown" });
    expect(bare.rearm).not.toHaveBeenCalled();
    expect(bare.events[0]).toMatchObject({
      diagnosis: "injection_unverifiable",
      probe: { http_status: 404, authentication: "unknown" },
    });
    // With evidence for this agent and repository: rearm publishes, the probe then proves access.
    const rearm = vi.fn(async () => {
      published = true;
    });
    const recovered = run({
      probe: probe as never,
      rearm,
      injectionEvidence: async () => missingFor(),
    });
    expect(await recovered.done).toMatchObject({ outcome: "ok" });
    expect(rearm).toHaveBeenCalledTimes(1);
    expect(recovered.events[0]).toMatchObject({ injection: "observed", rearm_attempts: 1 });
  });
});
