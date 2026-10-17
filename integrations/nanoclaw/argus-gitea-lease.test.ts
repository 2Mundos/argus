import { describe, expect, it } from "vitest";
import {
  type AccountLease,
  applyRepositoryLease,
  OneCliClient,
  releaseRepositoryLease,
} from "./argus-accounts.js";

describe("dedicated Gitea repository lease", () => {
  it("writes a generic Authorization secret for the configured host and removes the assignment on release", async () => {
    const calls: Array<{ method: string; path: string; body?: any }> = [];
    const agents: any[] = [{ id: "ag-1", identifier: "group-1" }];
    const secrets: any[] = [
      { id: "sec-engine", name: "argus-group-1" },
      { id: "sec-executor", name: "argus-group-1-openai" },
    ];
    let assigned = ["sec-engine", "sec-executor"];
    const fakeFetch = (async (url: string, init: RequestInit) => {
      const path = new URL(url).pathname.replace(/^\/api/, "");
      const body = init.body ? JSON.parse(init.body as string) : undefined;
      calls.push({ method: init.method ?? "GET", path, body });
      if (path === "/agents" && init.method === "GET") return Response.json(agents);
      if (path === "/secrets" && init.method === "GET") return Response.json(secrets);
      if (path === "/secrets" && init.method === "POST") {
        const secret = { id: "sec-repository", ...body };
        secrets.push(secret);
        return Response.json(secret);
      }
      if (path === "/agents/ag-1/secrets" && init.method === "PUT") {
        assigned = body.secretIds;
        return Response.json({ success: true });
      }
      return Response.json({ success: true });
    }) as typeof fetch;
    const onecli = new OneCliClient("http://onecli.test", fakeFetch);
    const target = { identifier: "group-1", name: "worker" };
    const lease: any = {
      lease_id: "lease-1",
      account_id: "account-1",
      name: "gitea",
      provider: "gitea",
      kind: "api_key",
      role: "repository",
      credential: { token: "dummy-only" },
    };
    const engineLease: AccountLease = {
      lease_id: "eng-lease",
      account_id: "engine-account",
      name: "engine",
      provider: "anthropic",
      kind: "api_key",
      role: "engine",
      credential: { api_key: "dummy-engine" },
    };
    const executorLease: AccountLease = {
      lease_id: "exec-lease",
      account_id: "executor-account",
      name: "executor",
      provider: "openai",
      kind: "api_key",
      role: "executor",
      credential: { auth_mode: "apikey", OPENAI_API_KEY: "dummy-executor" },
    };
    const active = [engineLease, executorLease];
    await applyRepositoryLease(
      onecli,
      target,
      lease,
      "https://git.example.test/team/repo",
      ["https://git.example.test/team/repo"],
      active,
    );
    expect(secrets.find((s) => s.id === "sec-repository")).toMatchObject({
      type: "generic",
      value: "token dummy-only",
      hostPattern: "git.example.test",
      headerName: "Authorization",
    });
    expect(assigned).toEqual(["sec-engine", "sec-executor", "sec-repository"]);
    await releaseRepositoryLease(onecli, target, active);
    expect(assigned).toEqual(["sec-engine", "sec-executor"]);
    expect(calls.some((call) => call.body?.secretIds?.includes("sec-repository") === false)).toBe(
      true,
    );
  });

  it("rejects an unconfigured repository before sending any OneCLI request", async () => {
    let calls = 0;
    const fetch = (async () => {
      calls++;
      return Response.json([]);
    }) as typeof globalThis.fetch;
    await expect(
      applyRepositoryLease(
        new OneCliClient("http://onecli.test", fetch),
        { identifier: "group", name: "worker" },
        { provider: "gitea", role: "repository", credential: { token: "dummy" } } as any,
        "https://git.example.test/team/other",
        ["https://git.example.test/team/repo"],
        [],
      ),
    ).rejects.toThrow(/allowlist/);
    expect(calls).toBe(0);
  });
});
