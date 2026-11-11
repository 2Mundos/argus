import { describe, expect, it } from "vitest";
import { onecliPathMatches } from "../gateway-providers/onecli-path-match.js";
import {
  type AccountLease,
  applyRepositoryLease,
  assignmentConfirmed,
  normalizeRepositoryUrl,
  OneCliClient,
  releaseRepositoryLease,
  verifiedCanonicalRepositoryUrl,
  verifyRepositoryAssignment,
} from "./argus-accounts.js";

describe("dedicated Gitea repository lease", () => {
  it("uses a OneCLI 1.42-compatible git path pattern (not /**)", () => {
    const { gitPathPattern } = normalizeRepositoryUrl("https://git.example.test/team/repo");
    const smartHttpPaths = [
      "/team/repo.git/info/refs",
      "/team/repo.git/git-upload-pack",
      "/team/repo.git/HEAD",
    ];
    for (const path of smartHttpPaths) {
      expect(onecliPathMatches(path, gitPathPattern)).toBe(true);
      expect(onecliPathMatches(path, "/team/repo.git/**")).toBe(false);
    }
  });

  it("normalizes repo and repo.git task URLs and allowlist entries consistently", async () => {
    for (const configuredSuffix of ["", ".git"]) {
      for (const allowedSuffix of ["", ".git"]) {
        const calls: Array<{ method: string; path: string; body?: any }> = [];
        const secrets: any[] = [];
        let assigned: string[] = [];
        const fetchImpl = (async (url: string, init: RequestInit) => {
          const path = new URL(url).pathname.replace(/^\/api/, "");
          const body = init.body ? JSON.parse(init.body as string) : undefined;
          calls.push({ method: init.method ?? "GET", path, body });
          if (path === "/agents" && init.method === "GET")
            return Response.json([{ id: "agent", identifier: "worker" }]);
          if (path === "/secrets" && init.method === "GET") return Response.json(secrets);
          if (path === "/secrets" && init.method === "POST") {
            const secret = { id: `secret-${secrets.length}`, ...body };
            secrets.push(secret);
            return Response.json(secret);
          }
          if (path === "/agents/agent/secrets" && init.method === "PUT") {
            assigned = body.secretIds;
            return Response.json({ success: true });
          }
          return Response.json({ success: true });
        }) as typeof globalThis.fetch;
        const configured = `https://git.example.test/team/repo${configuredSuffix}`;
        const allowlisted = `https://git.example.test/team/repo${allowedSuffix}`;
        const normalized = normalizeRepositoryUrl(configured);
        expect(normalized.cloneUrl).toBe("https://git.example.test/team/repo.git");
        expect(normalized.apiPath).toBe("/api/v1/repos/team/repo");
        expect(normalized.gitPathPattern).toBe("/team/repo.git/*");

        await applyRepositoryLease(
          new OneCliClient("http://127.0.0.1:10254", fetchImpl),
          { identifier: "worker", name: "worker" },
          {
            lease_id: "repository-lease",
            account_id: "repository-account",
            name: "repo",
            provider: "gitea",
            kind: "api_key",
            role: "repository",
            credential: { username: "service-user", token: "dummy-token" },
          },
          configured,
          [allowlisted],
          [],
        );
        expect(secrets.find((secret) => secret.name.endsWith("-rest"))?.pathPattern).toBe(
          "/api/v1/repos/team/repo/*",
        );
        expect(secrets.find((secret) => secret.name.endsWith("-git"))?.pathPattern).toBe(
          "/team/repo.git/*",
        );
        expect(assigned).toEqual(["secret-0", "secret-1"]);
        expect(calls.some((call) => call.body?.pathPattern === "/team/repo.git.git/*")).toBe(false);
      }
    }
  });

  it("injects a GitHub token on api.github.com (Bearer) and github.com Git (Basic), never a Gitea lease", async () => {
    const secrets: any[] = [];
    const fetchImpl = (async (url: string, init: RequestInit) => {
      const path = new URL(url).pathname.replace(/^\/api/, "");
      const body = init.body ? JSON.parse(init.body as string) : undefined;
      if (path === "/agents" && init.method === "GET")
        return Response.json([{ id: "agent", identifier: "worker" }]);
      if (path === "/secrets" && init.method === "GET") return Response.json(secrets);
      if (path === "/secrets" && init.method === "POST") {
        const secret = { id: `secret-${secrets.length}`, ...body };
        secrets.push(secret);
        return Response.json(secret);
      }
      return Response.json({ success: true });
    }) as typeof globalThis.fetch;
    const repository = "https://github.com/acme/widget";
    const normalized = normalizeRepositoryUrl(repository);
    expect(normalized).toMatchObject({
      provider: "github",
      apiHost: "api.github.com",
      apiPath: "/repos/acme/widget",
      cloneUrl: "https://github.com/acme/widget.git",
    });
    const lease = {
      lease_id: "repository-lease",
      account_id: "repository-account",
      name: "gh",
      provider: "github",
      kind: "api_key",
      role: "repository",
      credential: { token: "dummy-gh-token" },
    } as AccountLease;
    const onecli = new OneCliClient("http://127.0.0.1:10254", fetchImpl);
    const target = { identifier: "worker", name: "worker" };
    await expect(
      applyRepositoryLease(
        onecli,
        target,
        { ...lease, provider: "gitea" },
        repository,
        [repository],
        [],
      ),
    ).rejects.toThrow(/github provider/);
    await applyRepositoryLease(onecli, target, lease, repository, [repository], []);
    const rest = secrets.find((secret) => secret.name.endsWith("-rest"));
    const git = secrets.find((secret) => secret.name.endsWith("-git"));
    expect(rest).toMatchObject({
      hostPattern: "api.github.com",
      pathPattern: "/repos/acme/widget/*",
      value: "dummy-gh-token",
      injectionConfig: { headerName: "Authorization", valueFormat: "Bearer {value}" },
    });
    expect(git).toMatchObject({
      hostPattern: "github.com",
      pathPattern: "/acme/widget.git/*",
      value: Buffer.from("x-access-token:dummy-gh-token").toString("base64"),
      injectionConfig: { headerName: "Authorization", valueFormat: "Basic {value}" },
    });
  });

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
        const secret = {
          id: `sec-${body.name.endsWith("-git") ? "repository-git" : "repository-rest"}`,
          ...body,
        };
        secrets.push(secret);
        return Response.json(secret);
      }
      if (path.startsWith("/secrets/sec-repository-") && init.method === "PATCH") {
        Object.assign(secrets.find((secret) => secret.id === path.split("/").at(-1))!, body);
        return Response.json({ success: true });
      }
      if (path === "/agents/ag-1/secrets" && init.method === "PUT") {
        assigned = body.secretIds;
        return Response.json({ success: true });
      }
      return Response.json({ success: true });
    }) as typeof fetch;
    const onecli = new OneCliClient("http://127.0.0.1:10254", fakeFetch);
    const target = { identifier: "group-1", name: "worker" };
    const lease: any = {
      lease_id: "lease-1",
      account_id: "account-1",
      name: "gitea",
      provider: "gitea",
      kind: "api_key",
      role: "repository",
      credential: { username: "service-user", token: "dummy-only" },
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
    const createCalls = calls.filter((call) => call.path === "/secrets" && call.method === "POST");
    expect(createCalls).toHaveLength(2);
    const restSecret = createCalls.find((call) => call.body.name.endsWith("-rest"))!.body;
    const gitSecret = createCalls.find((call) => call.body.name.endsWith("-git"))!.body;
    expect(restSecret).toEqual({
      name: "argus-group-1-repository-rest",
      type: "generic",
      value: "dummy-only",
      hostPattern: "git.example.test",
      pathPattern: "/api/v1/repos/team/repo/*",
      injectionConfig: { headerName: "Authorization", valueFormat: "token {value}" },
    });
    expect(gitSecret).toEqual({
      name: "argus-group-1-repository-git",
      type: "generic",
      value: Buffer.from("service-user:dummy-only").toString("base64"),
      hostPattern: "git.example.test",
      pathPattern: "/team/repo.git/*",
      injectionConfig: { headerName: "Authorization", valueFormat: "Basic {value}" },
    });
    expect(assigned).toEqual([
      "sec-engine",
      "sec-executor",
      "sec-repository-rest",
      "sec-repository-git",
    ]);

    await applyRepositoryLease(
      onecli,
      target,
      { ...lease, credential: { username: "service-user", token: "dummy-updated" } },
      "https://git.example.test/team/repo",
      ["https://git.example.test/team/repo"],
      active,
    );
    expect(secrets.find((s) => s.id === "sec-repository-rest")?.value).toBe("dummy-updated");
    expect(secrets.find((s) => s.id === "sec-repository-git")?.value).toBe(
      Buffer.from("service-user:dummy-updated").toString("base64"),
    );
    expect(assigned).toEqual([
      "sec-engine",
      "sec-executor",
      "sec-repository-rest",
      "sec-repository-git",
    ]);
    await releaseRepositoryLease(onecli, target, active);
    expect(assigned).toEqual(["sec-engine", "sec-executor"]);
    expect(
      calls.some(
        (call) =>
          call.body?.secretIds?.includes("sec-repository-rest") === false &&
          call.body?.secretIds?.includes("sec-repository-git") === false,
      ),
    ).toBe(true);
  });

  it("rejects an unconfigured repository before sending any OneCLI request", async () => {
    let calls = 0;
    const fetch = (async () => {
      calls++;
      return Response.json([]);
    }) as typeof globalThis.fetch;
    await expect(
      applyRepositoryLease(
        new OneCliClient("http://127.0.0.1:10254", fetch),
        { identifier: "group", name: "worker" },
        { provider: "gitea", role: "repository", credential: { token: "dummy" } } as any,
        "https://git.example.test/team/other",
        ["https://git.example.test/team/repo"],
        [],
      ),
    ).rejects.toThrow(/allowlist/);
    expect(calls).toBe(0);
  });

  it("compares the repository allowlist case-insensitively on GitHub only", async () => {
    const secrets: any[] = [];
    const fetchImpl = (async (url: string, init: RequestInit) => {
      const path = new URL(url).pathname.replace(/^\/api/, "");
      const body = init.body ? JSON.parse(init.body as string) : undefined;
      if (path === "/agents" && init.method === "GET")
        return Response.json([{ id: "agent", identifier: "worker" }]);
      if (path === "/secrets" && init.method === "GET") return Response.json(secrets);
      if (path === "/secrets" && init.method === "POST") {
        const secret = { id: `secret-${secrets.length}`, ...body };
        secrets.push(secret);
        return Response.json(secret);
      }
      return Response.json({ success: true });
    }) as typeof globalThis.fetch;
    const onecli = new OneCliClient("http://127.0.0.1:10254", fetchImpl);
    const target = { identifier: "worker", name: "worker" };
    const lease = (provider: string) =>
      ({
        lease_id: "l",
        account_id: "a",
        name: "n",
        provider,
        kind: "api_key",
        role: "repository",
        credential: { username: "u", token: "dummy-token" },
      }) as AccountLease;
    await applyRepositoryLease(
      onecli,
      target,
      lease("github"),
      "https://github.com/2Mundos/Argus.git",
      ["https://github.com/2mundos/argus"],
      [],
    );
    // Scope patterns still follow the requested URL, not the allowlist entry.
    expect(secrets.find((s) => s.name.endsWith("-rest"))?.pathPattern).toBe(
      "/repos/2Mundos/Argus/*",
    );
    await expect(
      applyRepositoryLease(
        onecli,
        target,
        lease("github"),
        "https://github.com/2mundos/other",
        ["https://github.com/2mundos/argus"],
        [],
      ),
    ).rejects.toThrow(/allowlist/);
    await expect(
      applyRepositoryLease(
        onecli,
        target,
        lease("gitea"),
        "https://git.example.test/Team/Repo",
        ["https://git.example.test/team/repo"],
        [],
      ),
    ).rejects.toThrow(/allowlist/);
  });
});

describe("GitHub canonical spelling: one repository, two exact spellings", () => {
  const LOWER = "https://github.com/2mundos/argus.git";
  const CANONICAL = "https://github.com/2Mundos/argus.git";
  const TOKEN = "dummy-gh-token-not-real";
  const target = { identifier: "worker", name: "worker" };
  const lease = {
    lease_id: "repository-lease",
    account_id: "repository-account",
    name: "gh",
    provider: "github",
    kind: "api_key",
    role: "repository",
    credential: { token: TOKEN },
  } as AccountLease;
  const engine: AccountLease = {
    lease_id: "eng-lease",
    account_id: "engine-account",
    name: "engine",
    provider: "anthropic",
    kind: "api_key",
    role: "engine",
    credential: { api_key: "dummy-engine" },
  };

  /** A OneCLI admin API that keeps secrets and the agent's assignment, and counts calls. */
  const fakeOneCli = () => {
    const secrets: any[] = [{ id: "sec-engine", name: "argus-worker" }];
    const state = { assigned: ["sec-engine"] as string[], calls: 0 };
    const fetchImpl = (async (url: string, init: RequestInit) => {
      state.calls++;
      const path = new URL(url).pathname.replace(/^\/api/, "");
      const body = init.body ? JSON.parse(init.body as string) : undefined;
      if (path === "/agents" && init.method === "GET")
        return Response.json([{ id: "agent", identifier: "worker", secretMode: "selective" }]);
      if (path === "/secrets" && init.method === "GET") return Response.json(secrets);
      if (path === "/secrets" && init.method === "POST") {
        const secret = { id: `sec-${body.name.replace(/^argus-worker-/, "")}`, ...body };
        secrets.push(secret);
        return Response.json(secret);
      }
      if (path.startsWith("/secrets/") && init.method === "PATCH") {
        Object.assign(secrets.find((s) => s.id === path.split("/").at(-1))!, body);
        return Response.json({ success: true });
      }
      if (path === "/agents/agent/secrets" && init.method === "GET")
        return Response.json(state.assigned);
      if (path === "/agents/agent/secrets" && init.method === "PUT") {
        state.assigned = body.secretIds;
        return Response.json({ success: true });
      }
      return Response.json({ success: true });
    }) as typeof globalThis.fetch;
    const onecli = new OneCliClient("http://127.0.0.1:10254", fetchImpl);
    /**
     * The gateway's decision for one request, as OneCLI 1.42 makes it: an assigned secret
     * whose host is the request host exactly and whose path pattern matches the path.
     */
    const injects = (request: string) => {
      const u = new URL(request);
      return secrets.some(
        (s) =>
          state.assigned.includes(s.id) &&
          s.hostPattern === u.host &&
          typeof s.pathPattern === "string" &&
          onecliPathMatches(u.pathname + u.search, s.pathPattern),
      );
    };
    const repositoryScopes = () =>
      secrets
        .filter((s) => state.assigned.includes(s.id) && s.name.includes("-repository-"))
        .map((s) => `${s.name} ${s.hostPattern}${s.pathPattern}`)
        .sort();
    return { onecli, secrets, state, injects, repositoryScopes };
  };

  const smartHttp = (repository: string) => [
    `${repository}/info/refs?service=git-upload-pack`,
    `${repository}/git-upload-pack`,
    `${repository}/info/refs?service=git-receive-pack`,
    `${repository}/git-receive-pack`,
  ];
  const rest = (owner: string, repo: string) => [
    `https://api.github.com/repos/${owner}/${repo}`,
    `https://api.github.com/repos/${owner}/${repo}/pulls`,
  ];

  it("verifies GitHub's reported spelling of the same repository, and nothing else", () => {
    const reported = { full_name: "2Mundos/argus", clone_url: CANONICAL };
    expect(verifiedCanonicalRepositoryUrl(LOWER, reported)).toBe(CANONICAL);
    expect(verifiedCanonicalRepositoryUrl("https://github.com/2mundos/argus", reported)).toBe(
      CANONICAL,
    );
    expect(verifiedCanonicalRepositoryUrl(CANONICAL, reported)).toBe(CANONICAL);
    const r = (clone_url: string, full_name = "2Mundos/argus") => ({ full_name, clone_url });
    const refused: Array<[string, unknown]> = [
      [
        "a sibling repository",
        r("https://github.com/2Mundos/argus-fork.git", "2Mundos/argus-fork"),
      ],
      ["another owner", r("https://github.com/evil/argus.git", "evil/argus")],
      ["another host", r("https://evil.test/2Mundos/argus.git")],
      ["a subdomain", r("https://gist.github.com/2Mundos/argus.git")],
      ["plain http", r("http://github.com/2Mundos/argus.git")],
      ["userinfo", r("https://x:y@github.com/2Mundos/argus.git")],
      ["a query", r("https://github.com/2Mundos/argus.git?x=1")],
      ["an empty query", r("https://github.com/2Mundos/argus.git?")],
      ["a fragment", r("https://github.com/2Mundos/argus.git#x")],
      ["a port", r("https://github.com:443/2Mundos/argus.git")],
      ["a path extension", r("https://github.com/2Mundos/argus.git/x")],
      ["a dot segment", r("https://github.com/a/../2Mundos/argus.git")],
      ["no .git suffix", r("https://github.com/2Mundos/argus")],
      ["a host in upper case", r("https://GITHUB.com/2Mundos/argus.git")],
      ["full_name disagreeing with clone_url", r(CANONICAL, "2MUNDOS/argus")],
      ["full_name of another repository", r(CANONICAL, "2Mundos/other")],
      ["no clone_url", { full_name: "2Mundos/argus" }],
      ["non-string fields", { full_name: ["2Mundos/argus"], clone_url: { href: CANONICAL } }],
      ["no answer", null],
    ];
    for (const [label, answer] of refused)
      expect(verifiedCanonicalRepositoryUrl(LOWER, answer), label).toBeNull();
    // Only github.com repositories have a reported spelling to adopt.
    expect(
      verifiedCanonicalRepositoryUrl("https://git.example.test/team/repo.git", {
        full_name: "Team/Repo",
        clone_url: "https://git.example.test/Team/Repo.git",
      }),
    ).toBeNull();
  });

  it("scopes REST and Git to exactly both spellings, for upload-pack and receive-pack", async () => {
    const f = fakeOneCli();
    await applyRepositoryLease(f.onecli, target, lease, CANONICAL, [LOWER], [engine], LOWER);
    expect(f.repositoryScopes()).toEqual([
      "argus-worker-repository-git github.com/2Mundos/argus.git/*",
      "argus-worker-repository-git-alias github.com/2mundos/argus.git/*",
      "argus-worker-repository-rest api.github.com/repos/2Mundos/argus/*",
      "argus-worker-repository-rest-alias api.github.com/repos/2mundos/argus/*",
    ]);
    expect(f.state.assigned).toEqual([
      "sec-engine",
      "sec-repository-rest",
      "sec-repository-git",
      "sec-repository-rest-alias",
      "sec-repository-git-alias",
    ]);
    for (const request of [
      ...smartHttp("https://github.com/2Mundos/argus.git"),
      ...smartHttp("https://github.com/2mundos/argus.git"),
      ...rest("2Mundos", "argus"),
      ...rest("2mundos", "argus"),
    ])
      expect(f.injects(request), request).toBe(true);
    for (const request of [
      // A third spelling, a sibling, the owner, another host: never a wildcard or a fold.
      ...smartHttp("https://github.com/2MUNDOS/argus.git"),
      ...smartHttp("https://github.com/2Mundos/argus-fork.git"),
      ...smartHttp("https://github.com/2mundos/argus.git.evil"),
      "https://github.com/2Mundos/",
      "https://github.com/2Mundos/other.git/info/refs?service=git-upload-pack",
      ...smartHttp("https://gist.github.com/2Mundos/argus.git"),
      ...smartHttp("https://evil.test/2Mundos/argus.git"),
      ...rest("2Mundos", "other"),
      "https://api.github.com/repos/2Mundos",
      "https://api.github.com/user",
    ])
      expect(f.injects(request), request).toBe(false);
    const git = f.secrets.filter((s) => s.name.includes("-repository-git"));
    for (const s of git)
      expect(s).toMatchObject({
        value: Buffer.from(`x-access-token:${TOKEN}`).toString("base64"),
        injectionConfig: { headerName: "Authorization", valueFormat: "Basic {value}" },
      });
    for (const s of f.secrets.filter((s) => s.name.includes("-repository-rest")))
      expect(s).toMatchObject({ value: TOKEN, injectionConfig: { valueFormat: "Bearer {value}" } });
  });

  it("adopts nothing from a malicious or mismatching answer: the configured spelling only", async () => {
    for (const answer of [
      { full_name: "2Mundos/argus-fork", clone_url: "https://github.com/2Mundos/argus-fork.git" },
      { full_name: "2Mundos/argus", clone_url: "https://evil.test/2Mundos/argus.git" },
      { full_name: "2Mundos/argus", clone_url: "https://x:y@github.com/2Mundos/argus.git" },
      { full_name: "2Mundos/argus", clone_url: "https://github.com/2Mundos/argus.git?x" },
      { full_name: "2Mundos/argus", clone_url: "https://github.com/2Mundos/argus.git#x" },
    ]) {
      const f = fakeOneCli();
      const canonical = verifiedCanonicalRepositoryUrl(LOWER, answer);
      await applyRepositoryLease(
        f.onecli,
        target,
        lease,
        canonical ?? LOWER,
        [LOWER],
        [engine],
        canonical ? LOWER : undefined,
      );
      expect(f.repositoryScopes()).toEqual([
        "argus-worker-repository-git github.com/2mundos/argus.git/*",
        "argus-worker-repository-rest api.github.com/repos/2mundos/argus/*",
      ]);
      for (const request of smartHttp(answer.clone_url.replace(/[?#].*$/, "")))
        expect(f.injects(request), request).toBe(false);
    }
  });

  it("refuses an alias that is not the same github.com repository before any OneCLI request", async () => {
    for (const [primary, alias] of [
      [CANONICAL, "https://github.com/2mundos/argus-fork.git"],
      [CANONICAL, "https://github.com/evil/argus.git"],
      [CANONICAL, "https://evil.test/2mundos/argus.git"],
      [CANONICAL, "https://x:y@github.com/2mundos/argus.git"],
      [CANONICAL, "https://github.com/2mundos/argus.git?x"],
      ["https://git.example.test/Team/repo.git", "https://git.example.test/team/repo.git"],
    ]) {
      const f = fakeOneCli();
      const provider = primary.startsWith("https://github.com/") ? "github" : "gitea";
      await expect(
        applyRepositoryLease(
          f.onecli,
          target,
          { ...lease, provider, credential: { username: "u", token: TOKEN } },
          primary,
          [primary],
          [engine],
          alias,
        ),
        `${primary} + ${alias}`,
      ).rejects.toThrow(/alias/);
      expect(f.state.calls).toBe(0);
    }
  });

  it("verifies every expected scope, reconciles a stale alias away and revokes both on release", async () => {
    const f = fakeOneCli();
    // Bootstrap: the configured spelling alone, then both once GitHub's spelling is proven.
    await applyRepositoryLease(f.onecli, target, lease, LOWER, [LOWER], [engine]);
    expect(assignmentConfirmed(await verifyRepositoryAssignment(f.onecli, target, LOWER))).toBe(
      true,
    );
    await applyRepositoryLease(f.onecli, target, lease, CANONICAL, [LOWER], [engine], LOWER);
    const both = await verifyRepositoryAssignment(f.onecli, target, CANONICAL, LOWER);
    expect(both).toMatchObject({
      rest: { state: "assigned", scope: "match" },
      git: { state: "assigned", scope: "match" },
      alias: {
        rest: { state: "assigned", scope: "match" },
        git: { state: "assigned", scope: "match" },
      },
      scopes: {
        rest: { host: "api.github.com", path: "/repos/2Mundos/argus/*" },
        git: { host: "github.com", path: "/2Mundos/argus.git/*" },
        alias: {
          rest: { host: "api.github.com", path: "/repos/2mundos/argus/*" },
          git: { host: "github.com", path: "/2mundos/argus.git/*" },
        },
      },
    });
    expect(assignmentConfirmed(both)).toBe(true);
    // Without the alias expected, an assigned alias is a scope this lease did not ask for.
    expect(assignmentConfirmed(await verifyRepositoryAssignment(f.onecli, target, CANONICAL))).toBe(
      false,
    );
    // An alias the lease expects but OneCLI does not hold assigned is not confirmed.
    const held = f.state.assigned;
    f.state.assigned = held.filter((id) => id !== "sec-repository-git-alias");
    expect(
      assignmentConfirmed(await verifyRepositoryAssignment(f.onecli, target, CANONICAL, LOWER)),
    ).toBe(false);
    f.state.assigned = held;

    // A later lease of one spelling only leaves no alias of the earlier one assigned.
    await applyRepositoryLease(f.onecli, target, lease, CANONICAL, [LOWER], [engine]);
    expect(f.repositoryScopes()).toEqual([
      "argus-worker-repository-git github.com/2Mundos/argus.git/*",
      "argus-worker-repository-rest api.github.com/repos/2Mundos/argus/*",
    ]);
    expect(f.injects("https://github.com/2mundos/argus.git/git-receive-pack")).toBe(false);

    await applyRepositoryLease(f.onecli, target, lease, CANONICAL, [LOWER], [engine], LOWER);
    await releaseRepositoryLease(f.onecli, target, [engine]);
    expect(f.state.assigned).toEqual(["sec-engine"]);
    expect(f.repositoryScopes()).toEqual([]);
    for (const request of [
      ...smartHttp(CANONICAL),
      ...smartHttp(LOWER),
      ...rest("2Mundos", "argus"),
      ...rest("2mundos", "argus"),
    ])
      expect(f.injects(request), request).toBe(false);
  });
});
