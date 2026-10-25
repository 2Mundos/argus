import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  lstatSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { createServer as createHttpServer } from "node:http";
import { createServer as createHttpsServer } from "node:https";
import { connect } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import { ProxyAgent } from "undici";
import {
  allowlistedRepository,
  bodyFromOptions,
  createGatewayFetch,
  EXIT_CODES,
  GiteaApi,
  GiteaError,
  main,
  parseArgs,
  parseReviewComment,
  sanitizeMessage,
} from "./gitea-api.mjs";

describe("credential-free Gitea API helper", () => {
  it("isolates REST token and Git Basic injection to exact host and path rules in a local proxy", async () => {
    const token = "dummy-token";
    const basic = Buffer.from(`service-user:${token}`).toString("base64");
    const received = [];
    const proxy = createHttpServer((request, response) => {
      const path = request.url;
      const host = request.headers["x-target-host"];
      const authorization =
        host === "git.example.test" && path.startsWith("/api/v1/repos/team/project/")
          ? `token ${token}`
          : host === "git.example.test" && path.startsWith("/team/project.git/")
            ? `Basic ${basic}`
            : undefined;
      received.push({ host, path, authorization });
      response.end("ok");
    });
    await new Promise((resolve) => proxy.listen(0, "127.0.0.1", resolve));
    const port = proxy.address().port;
    try {
      for (const [host, path] of [
        ["git.example.test", "/api/v1/repos/team/project/issues"],
        ["git.example.test", "/team/project.git/git-receive-pack"],
        ["git.example.test", "/api/v1/repos/team/other/issues"],
        ["elsewhere.test", "/team/project.git/git-receive-pack"],
      ]) {
        await fetch(`http://127.0.0.1:${port}${path}`, {
          headers: { "X-Target-Host": host },
        });
      }
      assert.deepEqual(
        received.map(({ authorization }) => authorization),
        [`token ${token}`, `Basic ${basic}`, undefined, undefined],
      );
    } finally {
      await new Promise((resolve) => proxy.close(resolve));
    }
  });

  it("creates and reads PRs/status through the mocked gateway without caller credentials", async () => {
    const calls = [];
    let pullRequest;
    const fetch = async (url, init) => {
      calls.push({ url, init });
      if (url.includes("/pulls?")) return Response.json(pullRequest ? [pullRequest] : []);
      if (url.endsWith("/pulls") && init.method === "POST") {
        pullRequest = {
          number: 7,
          head: { ref: "topic", sha: "head-abc", repo: { full_name: "team/project" } },
          base: { ref: "main" },
        };
        return Response.json(pullRequest, { status: 201 });
      }
      if (url.endsWith("/pulls/7")) return Response.json(pullRequest);
      return Response.json([{ state: "success", sha: "head-abc" }]);
    };
    const api = new GiteaApi(
      "https://git.example.test/team/project",
      ["https://git.example.test/team/project"],
      fetch,
    );
    const opened = await api.createPullRequest({ title: "Docs", head: "topic", base: "main" });
    assert.equal(opened.number, 7);
    assert.equal(
      (await api.createPullRequest({ title: "Docs retry", head: "topic", base: "main" })).number,
      7,
    );
    assert.equal((await api.getPullRequest(7)).head.sha, "head-abc");
    assert.deepEqual((await api.getPullRequestStatus(7)).statuses[0].state, "success");
    assert.equal(calls.filter(({ init }) => init.method === "POST").length, 1);
    assert.ok(calls.some(({ url }) => url.endsWith("/statuses/head-abc")));
    assert.ok(calls.every(({ init }) => !("Authorization" in init.headers)));
    assert.ok(calls.every(({ init }) => init.redirect === "error"));
  });

  it("reads issues and lists pull requests on distinct REST paths", async () => {
    const calls = [];
    const fetch = async (url, init) => {
      calls.push({ url, init });
      if (url.endsWith("/issues/1043")) {
        return Response.json({
          number: 1043,
          title: "Tracker item",
          state: "open",
          pull_request: null,
        });
      }
      if (url.includes("/pulls?state=open&page=1&limit=2")) {
        return Response.json([{ number: 926, title: "PR", state: "open" }]);
      }
      if (url.endsWith("/pulls/926")) {
        return Response.json({
          number: 926,
          head: { ref: "topic", sha: "abc", repo: { full_name: "team/project" } },
          base: { ref: "main" },
        });
      }
      return Response.json(null, { status: 404 });
    };
    const api = new GiteaApi(
      "https://git.example.test/team/project",
      ["https://git.example.test/team/project"],
      fetch,
    );
    const issue = await api.getIssue(1043);
    assert.equal(issue.number, 1043);
    assert.equal(issue.pull_request, null);
    const prs = await api.listPullRequests({ state: "open", page: 1, limit: 2 });
    assert.equal(prs.length, 1);
    assert.equal(prs[0].number, 926);
    await api.getPullRequest(926);
    assert.ok(calls.some(({ url }) => url.endsWith("/issues/1043")));
    assert.ok(calls.some(({ url }) => url.includes("/pulls?state=open&page=1&limit=2")));
    assert.ok(calls.some(({ url }) => url.endsWith("/pulls/926")));
    assert.ok(calls.every(({ init }) => init.redirect === "error"));
  });

  it("rejects invalid list-prs pagination and state", () => {
    const api = new GiteaApi(
      "https://git.example.test/team/project",
      ["https://git.example.test/team/project"],
      async () => Response.json([]),
    );
    assert.throws(
      () => api.listPullRequests({ state: "merged", page: 1, limit: 10 }),
      /state must/,
    );
    assert.throws(
      () => api.listPullRequests({ state: "open", page: 0, limit: 10 }),
      /positive integer/,
    );
    assert.throws(
      () => api.listPullRequests({ state: "open", page: 1, limit: 51 }),
      /limit must be 1\.\.50/,
    );
  });

  it("fails closed when the worker repository allowlist is empty", () => {
    assert.throws(
      () => allowlistedRepository("https://git.example.test/team/project", []),
      /repository is not configured for this worker/,
    );
  });

  it("normalizes configured and allowlisted .git URL forms to one REST identity", async () => {
    for (const configuredSuffix of ["", ".git"]) {
      for (const allowSuffix of ["", ".git"]) {
        const calls = [];
        let pullRequest;
        const fetch = async (url, init) => {
          calls.push({ url, init });
          if (url.includes("/pulls?")) return Response.json(pullRequest ? [pullRequest] : []);
          if (url.endsWith("/pulls") && init.method === "POST") {
            pullRequest = {
              number: 17,
              head: { ref: "feature", sha: "exact-head", repo: { full_name: "team/project" } },
              base: { ref: "main" },
            };
            return Response.json(pullRequest, { status: 201 });
          }
          return Response.json(pullRequest);
        };
        const configured = `https://git.example.test/team/project${configuredSuffix}`;
        const allowed = `https://git.example.test/team/project${allowSuffix}`;
        const api = new GiteaApi(configured, [allowed], fetch);
        assert.deepEqual(api.repository, {
          origin: "https://git.example.test",
          owner: "team",
          repo: "project",
        });
        assert.equal(
          await api
            .createPullRequest({ title: "PR", head: "feature", base: "main" })
            .then((pr) => pr.number),
          17,
        );
        assert.equal(
          await api
            .createPullRequest({ title: "retry", head: "feature", base: "main" })
            .then((pr) => pr.number),
          17,
        );
        assert.equal(calls.filter(({ init }) => init.method === "POST").length, 1);
        assert.ok(calls.every(({ url }) => url.includes("/api/v1/repos/team/project/")));
        assert.ok(calls.every(({ url }) => !url.includes("project.git")));
      }
    }
  });

  it("rejects hosts/repos outside the exact HTTPS allowlist and has no merge operation", async () => {
    const noopFetch = async () => Response.json({});
    assert.throws(
      () =>
        new GiteaApi(
          "https://git.example.test/other/project",
          ["https://git.example.test/team/project"],
          noopFetch,
        ),
      /not configured/,
    );
    assert.throws(
      () =>
        new GiteaApi(
          "http://git.example.test/team/project",
          ["https://git.example.test/team/project"],
          noopFetch,
        ),
      /HTTPS/,
    );
    for (const lookalike of ["project-other", "project.git.git"]) {
      assert.throws(
        () =>
          new GiteaApi(
            `https://git.example.test/team/${lookalike}`,
            ["https://git.example.test/team/project.git"],
            noopFetch,
          ),
        /not configured/,
      );
    }
    const api = new GiteaApi(
      "https://git.example.test/team/project",
      ["https://git.example.test/team/project"],
      noopFetch,
    );
    assert.throws(() => api.updatePullRequest(7, { merge: true }), /unsupported PR update field/);
  });

  it("uses Node fetch with an Undici proxy dispatcher and fails closed without the proxy", async () => {
    let actualRequest;
    const proxiedFetch = createGatewayFetch({
      proxy: "http://127.0.0.1:8181",
      fetchImpl: async (url, init) => {
        actualRequest = { url, init };
        return Response.json({ ok: true });
      },
    });
    await proxiedFetch("https://git.example.test/api/v1/version", { method: "GET" });
    assert.ok(actualRequest.init.dispatcher instanceof ProxyAgent);
    assert.equal(actualRequest.url, "https://git.example.test/api/v1/version");
    assert.throws(() => createGatewayFetch({ proxy: "" }), /not configured/);
  });

  async function withSelfSignedConnectProxy(run) {
    const dir = mkdtempSync(join(tmpdir(), "argus-gitea-proxy-test-"));
    execFileSync(
      "openssl",
      [
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        join(dir, "key.pem"),
        "-out",
        join(dir, "cert.pem"),
        "-days",
        "1",
        "-subj",
        "/CN=git.example.test",
        "-addext",
        "subjectAltName=DNS:git.example.test",
      ],
      { stdio: "ignore" },
    );
    const origin = createHttpsServer(
      { key: readFileSync(join(dir, "key.pem")), cert: readFileSync(join(dir, "cert.pem")) },
      (_request, response) => {
        response.setHeader("content-type", "application/json");
        response.end(JSON.stringify({ throughProxy: true }));
      },
    );
    const proxy = createHttpServer();
    let connectTarget;
    proxy.on("connect", (request, client, head) => {
      connectTarget = request.url;
      const upstream = connect(origin.address().port, "127.0.0.1", () => {
        client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
        if (head.length) upstream.write(head);
        upstream.pipe(client);
        client.pipe(upstream);
      });
    });
    const listen = (server) => new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const close = (server) => new Promise((resolve) => server.close(resolve));
    try {
      await listen(origin);
      await listen(proxy);
      return await run({
        proxyUrl: `http://127.0.0.1:${proxy.address().port}`,
        connectTarget: () => connectTarget,
      });
    } finally {
      await close(proxy);
      await close(origin);
      rmSync(dir, { recursive: true, force: true });
    }
  }

  it("sends an actual Node fetch through an HTTPS CONNECT proxy", async () => {
    await withSelfSignedConnectProxy(async ({ proxyUrl, connectTarget }) => {
      const gatewayFetch = createGatewayFetch({
        proxy: proxyUrl,
        // Localhost fixture only: the origin cert is self-signed.
        requestTls: { rejectUnauthorized: false },
      });
      const api = new GiteaApi(
        "https://git.example.test/team/project",
        ["https://git.example.test/team/project"],
        gatewayFetch,
      );
      assert.deepEqual(await api.request("/issues"), { throughProxy: true });
      assert.equal(connectTarget(), "git.example.test:443");
    });
  });

  it("fails TLS verification against a self-signed origin when rejectUnauthorized is default", async () => {
    await withSelfSignedConnectProxy(async ({ proxyUrl }) => {
      const gatewayFetch = createGatewayFetch({ proxy: proxyUrl });
      const api = new GiteaApi(
        "https://git.example.test/team/project",
        ["https://git.example.test/team/project"],
        gatewayFetch,
      );
      await assert.rejects(
        () => api.request("/issues"),
        (error) => {
          assert.ok(error instanceof GiteaError);
          assert.equal(error.code, "network_error");
          return true;
        },
      );
    });
  });

  it("reads long PR bodies from --body-file and refuses ambiguous or unsafe sources", () => {
    const dir = mkdtempSync(join(tmpdir(), "gitea-body-"));
    try {
      const file = join(dir, "body.md");
      const text = `# PR\n\n${'line `with` "quotes" and $vars\n'.repeat(50)}`;
      writeFileSync(file, text);
      assert.equal(bodyFromOptions({ "body-file": file }), text);
      assert.equal(bodyFromOptions({ body: "inline" }), "inline");
      assert.equal(bodyFromOptions({}), undefined);
      assert.throws(() => bodyFromOptions({ body: "x", "body-file": file }), /either --body or/);
      assert.throws(() => bodyFromOptions({ "body-file": dir }), /regular file/);
      assert.throws(() => bodyFromOptions({ "body-file": join(dir, "missing") }), /not readable/);
      const big = join(dir, "big.md");
      writeFileSync(big, "x".repeat(256 * 1024 + 1));
      assert.throws(() => bodyFromOptions({ "body-file": big }), /larger than/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

const REPO = "https://git.example.test/team/project";
const API = "/api/v1/repos/team/project";
const CANARY = "canary-0f9e8d7c6b5a4f3e2d1c9b8a";
const FIXTURES = new URL("./fixtures/gitea-api/", import.meta.url);
const fixture = (name) => JSON.parse(readFileSync(new URL(name, FIXTURES), "utf8"));
const fixtureBytes = (name) => readFileSync(new URL(name, FIXTURES));
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");

/** Offline Gitea: `handler` gets {route, method, query, body, url} and returns a Response or undefined (404). */
function mockGitea(handler) {
  const calls = [];
  const fetch = async (url, init = {}) => {
    const parsed = new URL(url);
    const call = {
      url: parsed,
      method: init.method ?? "GET",
      headers: init.headers ?? {},
      body: init.body === undefined ? undefined : JSON.parse(init.body),
      redirect: init.redirect,
    };
    calls.push(call);
    const route =
      parsed.host === "git.example.test" && parsed.pathname.startsWith(API)
        ? parsed.pathname.slice(API.length)
        : parsed.href;
    return (
      (await handler({ ...call, route, query: parsed.searchParams })) ??
      Response.json({ message: "not found" }, { status: 404 })
    );
  };
  return { fetch, calls };
}

async function cli(argv, { fetch, env = {}, stdin = "", cwd, sleeps = [] } = {}) {
  let out = "";
  let err = "";
  const code = await main(
    argv,
    {
      ARGUS_REPOSITORY_URL: REPO,
      ARGUS_REPOSITORIES_JSON: JSON.stringify({ project: REPO }),
      ...env,
    },
    {
      fetch,
      stdout: { write: (chunk) => (out += chunk) },
      stderr: { write: (chunk) => (err += chunk) },
      readStdin: () => Buffer.from(stdin),
      sleep: async (ms) => sleeps.push(ms),
      random: () => 0.5,
      cwd,
    },
  );
  return {
    code,
    out,
    err,
    json: () => JSON.parse(out),
    error: () => JSON.parse(err).error,
  };
}

const page = (items, headers = {}) => Response.json(items, { headers });

describe("argus-gitea-api command matrix", () => {
  it("pages issue-list across more than one page and keeps issue number and id distinct", async () => {
    const issues = [1, 2, 3, 4, 5].map((n) => ({ id: 9000 + n, number: n, title: `Item ${n}` }));
    const gitea = mockGitea(({ route, query }) => {
      if (route !== "/issues") return;
      const at = (Number(query.get("page")) - 1) * Number(query.get("limit"));
      return page(issues.slice(at, at + Number(query.get("limit"))), { "x-total-count": "5" });
    });
    const all = await cli(["issue-list", "--all", "--limit", "2"], gitea);
    assert.equal(all.code, 0, all.err);
    const result = all.json();
    assert.equal(result.count, 5);
    assert.equal(result.pages_fetched, 3);
    assert.equal(result.has_more, false);
    assert.equal(result.truncated, false);
    assert.deepEqual(
      result.items.map((i) => [i.issue_number, i.issue_id, i.is_pull_request]),
      issues.map((i) => [i.number, i.id, false]),
    );
    assert.deepEqual(
      gitea.calls.map((c) => c.url.searchParams.get("page")),
      ["1", "2", "3"],
    );
    assert.ok(gitea.calls.every((c) => c.url.searchParams.get("type") === "issues"));

    const capped = await cli(["issue-list", "--all", "--limit=2", "--max-items=3"], gitea);
    assert.equal(capped.json().count, 3);
    assert.equal(capped.json().truncated, true);
    const single = await cli(["issue-list", "--limit", "2", "--page", "2"], gitea);
    assert.deepEqual(
      single.json().items.map((i) => i.issue_number),
      [3, 4],
    );
    assert.equal(single.json().next_page, 3);
  });

  it("follows rel=next Link pagination for Actions runs and stops on the last page", async () => {
    const gitea = mockGitea(({ route, query }) => {
      if (route !== "/actions/runs") return;
      const n = Number(query.get("page"));
      const runs = n === 1 ? [{ id: 88, run_number: 12 }] : [{ id: 87, run_number: 11 }];
      return Response.json(
        { total_count: 2, workflow_runs: runs },
        { headers: n === 1 ? { link: `<${REPO}/x?page=2>; rel="next"` } : {} },
      );
    });
    const run = await cli(
      [
        "run-list",
        "--all",
        "--limit",
        "1",
        "--branch",
        "feat/x",
        "--head-sha",
        "ABCDEF1",
        "--status",
        "success",
      ],
      gitea,
    );
    assert.equal(run.code, 0, run.err);
    assert.deepEqual(
      run.json().items.map((r) => [r.run_id, r.run_number]),
      [
        [88, 12],
        [87, 11],
      ],
    );
    const query = gitea.calls[0].url.searchParams;
    assert.equal(query.get("branch"), "feat/x");
    assert.equal(query.get("head_sha"), "abcdef1");
    assert.equal(query.get("status"), "success");
    // The Link URL host is never requested; pages are rebuilt from the repository base.
    assert.ok(gitea.calls.every((c) => c.url.pathname === `${API}/actions/runs`));
  });

  it("keeps get pull-request-only and issue-get issue-only", async () => {
    const gitea = mockGitea(({ route }) => {
      if (route === "/issues/1043")
        return Response.json({ id: 77, number: 1043, title: "Tracker" });
    });
    const pr = await cli(["get", "--number", "1043"], gitea);
    assert.equal(pr.code, EXIT_CODES.not_found);
    assert.equal(pr.error().code, "not_found");
    assert.equal(pr.error().path, "/pulls/1043");
    const issue = await cli(["issue-get", "--number", "1043"], gitea);
    assert.equal(issue.json().number, 1043);
  });

  it("reports the exact-head agentic review (redacted PR 1135 / comment 9208 fixture)", async () => {
    const pr = fixture("pr-1135.json");
    const comments = fixture("comments-pr-1135.json");
    const statuses = fixture("statuses-pr-1135-head.json");
    const routes = (head) =>
      mockGitea(({ route }) => {
        if (route === "/pulls/1135")
          return Response.json({ ...pr, head: { ...pr.head, sha: head } });
        if (route === "/issues/1135/comments") return page(comments);
        if (route === `/commits/${head}/statuses`) return page(statuses);
        if (route === `/commits/${head}/status`) return Response.json({ state: "success" });
      });
    const found = await cli(["review-findings", "--pr", "1135"], routes(pr.head.sha));
    assert.equal(found.code, 0, found.err);
    const result = found.json();
    assert.equal(result.head_sha, pr.head.sha);
    assert.equal(result.comment_id, 9208);
    assert.equal(result.finding_count, 2);
    assert.equal(result.matches_head, true);
    assert.equal(result.reason, null);
    assert.equal(result.status.state, "success");
    assert.equal(result.body.match(/### P2/g).length, 2);

    // Head moved past the review: a green status must not read as "no findings".
    const moved = (await cli(["review-findings", "--pr", "1135"], routes("b".repeat(40)))).json();
    assert.equal(moved.matches_head, false);
    assert.equal(moved.finding_count, null);
    assert.equal(moved.comment_id, 9208);
    assert.match(moved.reason, /different head SHA/);
    assert.match(moved.reason, /green status is not evidence/);

    // The older comment declared zero findings for an older head; it never counts for the new head.
    const old = (await cli(["review-findings", "--pr", "1135"], routes(pr.base.sha))).json();
    assert.equal(old.comment_id, 9100);
    assert.equal(old.finding_count, 0);
  });

  it("parses finding counts and head SHAs tolerantly", () => {
    assert.deepEqual(parseReviewComment("Agentic review: **no findings** on head abcdef1"), {
      head_sha: "abcdef1",
      finding_count: 0,
    });
    assert.equal(parseReviewComment("Findings (3)\nsha=ABCDEF12").finding_count, 3);
    assert.equal(parseReviewComment("Findings (3)\nsha=ABCDEF12").head_sha, "abcdef12");
    assert.equal(parseReviewComment("1 P1 finding").finding_count, 1);
    assert.deepEqual(parseReviewComment("Agentic review passed."), {
      head_sha: null,
      finding_count: null,
    });
  });

  it("reads Actions jobs, binary-safe job logs and artifacts with limits and sha256", async () => {
    const log = fixtureBytes("job-log.bin");
    const zip = fixtureBytes("artifact.bin");
    const gitea = mockGitea(({ route, headers }) => {
      if (route === "/actions/runs/88")
        return Response.json({ id: 88, run_number: 12, status: "completed" });
      if (route === "/actions/runs/88/jobs")
        return Response.json({ total_count: 1, jobs: [{ id: 301, run_id: 88, name: "test" }] });
      if (route === "/actions/runs/88/artifacts")
        return Response.json({ total_count: 1, artifacts: [{ id: 41, name: "report" }] });
      if (route === "/actions/jobs/301/logs") return new Response(log);
      if (route === "/actions/artifacts/41/zip")
        return new Response(null, {
          status: 302,
          // Artifact evidence path: same-origin /storage/…/{artifactId}.zip only.
          headers: { location: "https://git.example.test/storage/a/41.zip?sig=x" },
        });
      if (route === "https://git.example.test/storage/a/41.zip?sig=x") {
        assert.deepEqual(Object.keys(headers), ["Accept"]);
        return new Response(zip, { headers: { "content-type": "application/zip" } });
      }
    });
    assert.equal((await cli(["run-get", "--run-id", "88"], gitea)).json().run_id, 88);
    const jobs = (await cli(["job-list", "--run-id=88"], gitea)).json();
    assert.deepEqual([jobs.run_id, jobs.items[0].job_id], [88, 301]);
    assert.equal(
      (await cli(["artifact-list", "--run-id", "88"], gitea)).json().items[0].artifact_id,
      41,
    );

    const text = await cli(["job-logs", "--job-id", "301"], gitea);
    assert.equal(text.code, 0, text.err);
    assert.equal(text.out, log.toString("utf8"));
    assert.ok(text.out.includes("�"));

    const dir = mkdtempSync(join(tmpdir(), "gitea-dl-"));
    try {
      const saved = await cli(["job-logs", "--job-id", "301", "--out", "job.log"], {
        ...gitea,
        cwd: dir,
      });
      assert.equal(saved.code, 0, saved.err);
      assert.deepEqual(readFileSync(join(dir, "job.log")), log);
      assert.equal(saved.json().sha256, sha256(log));

      const art = await cli(["artifact-download", "--artifact-id", "41", "--out", "a.zip"], {
        ...gitea,
        cwd: dir,
      });
      assert.equal(art.code, 0, art.err);
      assert.deepEqual(
        { ...art.json(), path: undefined },
        {
          artifact_id: 41,
          path: undefined,
          bytes: zip.length,
          sha256: sha256(zip),
          content_type: "application/zip",
        },
      );
      assert.deepEqual(readFileSync(join(dir, "a.zip")), zip);

      const again = await cli(["artifact-download", "--artifact-id", "41", "--out", "a.zip"], {
        ...gitea,
        cwd: dir,
      });
      assert.equal(again.code, EXIT_CODES.policy_denied);
      assert.match(again.error().message, /refusing to overwrite/);

      symlinkSync(join(dir, "a.zip"), join(dir, "link.zip"));
      const link = await cli(["artifact-download", "--artifact-id", "41", "--out", "link.zip"], {
        ...gitea,
        cwd: dir,
      });
      assert.equal(link.code, EXIT_CODES.policy_denied);
      assert.match(link.error().message, /symlink/);
      assert.ok(lstatSync(join(dir, "link.zip")).isSymbolicLink());

      const traversal = await cli(
        ["artifact-download", "--artifact-id", "41", "--out", "../x.zip"],
        {
          ...gitea,
          cwd: dir,
        },
      );
      assert.equal(traversal.code, EXIT_CODES.policy_denied);

      const big = await cli(
        ["artifact-download", "--artifact-id", "41", "--out", "big.zip", "--max-bytes", "100"],
        { ...gitea, cwd: dir },
      );
      assert.equal(big.code, EXIT_CODES.limit_exceeded);
      assert.deepEqual(readdirSync(dir).sort(), ["a.zip", "job.log", "link.zip"]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("returns pr-diff as raw text and enforces its size limit", async () => {
    const diff = "diff --git a/x b/x\n+✓ line\n";
    const gitea = mockGitea(({ route }) =>
      route === "/pulls/7.diff" ? new Response(diff) : undefined,
    );
    assert.equal((await cli(["pr-diff", "--number", "7"], gitea)).out, diff);
    assert.equal(
      (await cli(["pr-diff", "--number", "7", "--max-bytes", "5"], gitea)).code,
      EXIT_CODES.limit_exceeded,
    );
  });

  it("lists statuses for a SHA with the combined state", async () => {
    const gitea = mockGitea(({ route }) => {
      const sha = "a".repeat(40);
      if (route === `/commits/${sha}/status`) return Response.json({ state: "failure" });
      if (route === `/commits/${sha}/statuses`)
        return page([{ id: 5, status: "failure", context: "ci" }]);
    });
    const result = (await cli(["statuses", "--sha", "a".repeat(40)], gitea)).json();
    assert.equal(result.combined_state, "failure");
    assert.equal(result.items[0].status_id, 5);
  });

  it("preserves Unicode, newlines, commas and equals signs in bodies", async () => {
    const posted = [];
    const gitea = mockGitea(({ route, method, body }) => {
      if (route === "/issues/9/comments" && method === "POST") {
        posted.push(body.body);
        return Response.json({ id: 500 + posted.length, body: body.body }, { status: 201 });
      }
      const match = /^\/issues\/comments\/(\d+)$/.exec(route);
      if (match)
        return Response.json({ id: Number(match[1]), body: posted[Number(match[1]) - 501] });
    });
    const text = "Line one ✓ — “quoted” 🚀\n\nkey=value, a,b=c\n\ttabbed";
    const viaStdin = await cli(["comment-create", "--number", "9", "--body-stdin"], {
      ...gitea,
      stdin: text,
    });
    assert.equal(viaStdin.code, 0, viaStdin.err);
    assert.equal(viaStdin.json().comment_id, 501);
    const viaEquals = await cli(["comment-create", "--number=9", `--body=${text}`], gitea);
    assert.equal(viaEquals.json().comment_id, 502);
    const viaArg = await cli(["comment-create", "--pr", "9", "--body", text], gitea);
    assert.equal(viaArg.json().comment.body, text);
    assert.deepEqual(posted, [text, text, text]);
  });

  it("makes comment-create idempotent with an argus-key marker", async () => {
    const comments = [];
    const gitea = mockGitea(({ route, method, body }) => {
      if (route === "/issues/9/comments" && method === "POST") {
        comments.push({
          id: 600 + comments.length,
          body: body.body,
          created_at: "2026-10-24T10:00:00Z",
        });
        return Response.json(comments.at(-1), { status: 201 });
      }
      if (route === "/issues/9/comments") return page(comments);
      if (route === "/issues/comments/600") return Response.json(comments[0]);
    });
    const first = await cli(
      ["comment-create", "--number", "9", "--body", "hi", "--idempotency-key", "run-1"],
      gitea,
    );
    assert.equal(first.json().created, true);
    assert.equal(comments[0].body, "hi\n\n<!-- argus-key:run-1 -->");
    const second = await cli(
      ["comment-create", "--number", "9", "--body", "hi", "--idempotency-key", "run-1"],
      gitea,
    );
    assert.equal(second.json().created, false);
    assert.equal(second.json().comment_id, 600);
    assert.equal(gitea.calls.filter((c) => c.method === "POST").length, 1);
  });

  it("retries GET on 429 honoring Retry-After, then succeeds", async () => {
    let attempts = 0;
    const gitea = mockGitea(({ route }) => {
      if (route !== "/pulls/7") return;
      attempts++;
      return attempts === 1
        ? Response.json({ message: "slow down" }, { status: 429, headers: { "retry-after": "2" } })
        : Response.json({ number: 7 });
    });
    const sleeps = [];
    const result = await cli(["get", "--number", "7"], { ...gitea, sleeps });
    assert.equal(result.code, 0, result.err);
    assert.equal(attempts, 2);
    assert.deepEqual(sleeps, [2000]);
  });

  it("bounds GET retries on 5xx and classifies the final failure", async () => {
    const gitea = mockGitea(({ route }) =>
      route === "/pulls/7" ? Response.json({ message: "boom" }, { status: 503 }) : undefined,
    );
    const sleeps = [];
    const result = await cli(["get", "--number", "7"], { ...gitea, sleeps });
    assert.equal(result.code, EXIT_CODES.server_error);
    assert.equal(gitea.calls.length, 4);
    assert.equal(sleeps.length, 3);
    assert.ok(sleeps.every((ms, i) => ms <= 10_000 && ms >= 250 * 2 ** i));
    assert.deepEqual(Object.keys(result.error()).sort(), [
      "code",
      "message",
      "method",
      "path",
      "status",
    ]);
  });

  it("never retries a POST on 5xx and reconciles the outcome by readback", async () => {
    const comments = [];
    const gitea = mockGitea(({ route, method, body }) => {
      if (route === "/issues/9/comments" && method === "POST") {
        comments.push({ id: 700, body: body.body, created_at: "2026-10-24T10:00:00Z" });
        return Response.json({ message: "gateway timeout" }, { status: 502 });
      }
      if (route === "/issues/9/comments") return page(comments);
      if (route === "/issues/comments/700") return Response.json(comments[0]);
    });
    const result = await cli(["comment-create", "--number", "9", "--body", "report"], gitea);
    assert.equal(result.code, 0, result.err);
    assert.equal(result.json().comment_id, 700);
    assert.equal(result.json().reconciled, true);
    assert.equal(gitea.calls.filter((c) => c.method === "POST").length, 1);

    const lost = mockGitea(({ route, method }) => {
      if (route === "/issues/9/comments" && method === "POST") throw new TypeError("fetch failed");
      if (route === "/issues/9/comments") return page([]);
    });
    const failed = await cli(["comment-create", "--number", "9", "--body", "report"], lost);
    assert.equal(failed.code, EXIT_CODES.network_error);
    assert.match(failed.error().message, /not retried/);
    assert.equal(lost.calls.filter((c) => c.method === "POST").length, 1);
  });

  it("never retries a PATCH on 5xx and reconciles PR updates by readback", async () => {
    let pr = {
      number: 7,
      title: "Old",
      body: "old body",
      state: "open",
      html_url: "https://git.example.test/team/project/pulls/7",
      head: { sha: "head-abc", ref: "topic" },
      base: { ref: "main" },
    };
    const gitea = mockGitea(({ route, method, body }) => {
      if (route === "/pulls/7" && method === "PATCH") {
        pr = {
          ...pr,
          ...(body.title !== undefined ? { title: body.title } : {}),
          ...(body.body !== undefined ? { body: body.body } : {}),
          ...(body.state !== undefined ? { state: body.state } : {}),
          ...(body.base !== undefined ? { base: { ...pr.base, ref: body.base } } : {}),
        };
        return Response.json({ message: "gateway timeout" }, { status: 502 });
      }
      if (route === "/pulls/7") return Response.json(pr);
    });
    const api = new GiteaApi(REPO, [REPO], gitea.fetch);
    // Library keeps the legacy PR object return (not {pull, reconciled}).
    const updated = await api.updatePullRequest(7, {
      title: "New title",
      body: "new body",
      base: "develop",
    });
    assert.equal(updated.title, "New title");
    assert.equal(updated.body, "new body");
    assert.equal(updated.base.ref, "develop");
    assert.equal(updated.number, 7);
    assert.equal(updated.reconciled, undefined);
    assert.equal(updated.pull, undefined);
    assert.equal(gitea.calls.filter((c) => c.method === "PATCH").length, 1);

    // CLI keeps post-success get and historical PR shape (no reconciled wrapper).
    const viaCli = await cli(["update", "--number", "7", "--title", "Newer"], gitea);
    assert.equal(viaCli.code, 0, viaCli.err);
    assert.equal(viaCli.json().number, 7);
    assert.equal(viaCli.json().title, "Newer");
    assert.equal(viaCli.json().html_url, pr.html_url);
    assert.equal(viaCli.json().head.sha, "head-abc");
    assert.equal(viaCli.json().reconciled, undefined);
    assert.equal(gitea.calls.filter((c) => c.method === "PATCH").length, 2);

    const lost = mockGitea(({ route, method }) => {
      if (route === "/pulls/7" && method === "PATCH") throw new TypeError("fetch failed");
      if (route === "/pulls/7")
        return Response.json({
          number: 7,
          title: "stale",
          body: "",
          state: "open",
          base: { ref: "main" },
          head: { sha: "x" },
        });
    });
    const failed = await cli(["update", "--number", "7", "--title", "nope"], lost);
    assert.equal(failed.code, EXIT_CODES.network_error);
    assert.match(failed.error().message, /not retried/);
    assert.equal(lost.calls.filter((c) => c.method === "PATCH").length, 1);
  });

  it("preserves historical CLI shapes for create, list-prs, update, and status", async () => {
    let pr = null;
    const gitea = mockGitea(({ route, method, body }) => {
      if (route === "/pulls" && method === "POST") {
        pr = {
          number: 42,
          html_url: "https://git.example.test/team/project/pulls/42",
          title: body.title,
          body: body.body ?? "",
          state: "open",
          head: { ref: body.head, sha: "deadbeef", repo: { full_name: "team/project" } },
          base: { ref: body.base },
        };
        return Response.json(pr, { status: 201 });
      }
      // create idempotency probe and list-prs both GET /pulls?... (pathname route is /pulls).
      if (route === "/pulls" && method === "GET") return Response.json(pr ? [pr] : []);
      if (route === "/pulls/42" && method === "PATCH") {
        pr = {
          ...pr,
          ...(body.title !== undefined ? { title: body.title } : {}),
          ...(body.body !== undefined ? { body: body.body } : {}),
          ...(body.state !== undefined ? { state: body.state } : {}),
          ...(body.base !== undefined ? { base: { ...pr.base, ref: body.base } } : {}),
        };
        return Response.json(pr);
      }
      if (route === "/pulls/42") return Response.json(pr);
      if (route === "/statuses/deadbeef")
        return Response.json([{ state: "success", context: "ci", sha: "deadbeef" }]);
    });

    const created = await cli(
      ["create", "--title", "Docs", "--head", "topic", "--base", "main", "--body", "hello"],
      gitea,
    );
    assert.equal(created.code, 0, created.err);
    const createdPr = created.json();
    assert.equal(createdPr.number, 42);
    assert.equal(createdPr.html_url, "https://git.example.test/team/project/pulls/42");
    assert.equal(createdPr.head.sha, "deadbeef");

    const listed = await cli(["list-prs", "--state", "open"], gitea);
    assert.equal(listed.code, 0, listed.err);
    assert.ok(Array.isArray(listed.json()));
    assert.equal(listed.json()[0].number, 42);

    const updated = await cli(["update", "--number", "42", "--title", "Docs v2"], gitea);
    assert.equal(updated.code, 0, updated.err);
    assert.equal(updated.json().number, 42);
    assert.equal(updated.json().title, "Docs v2");
    assert.equal(updated.json().html_url, createdPr.html_url);
    assert.equal(updated.json().head.sha, "deadbeef");

    const status = await cli(["status", "--number", "42"], gitea);
    assert.equal(status.code, 0, status.err);
    assert.deepEqual(Object.keys(status.json()).sort(), ["head_sha", "number", "statuses"]);
    assert.equal(status.json().number, 42);
    assert.equal(status.json().head_sha, "deadbeef");
    assert.equal(status.json().statuses[0].state, "success");
  });

  it("lists labels and comments and gets a comment by id", async () => {
    const labels = [
      { id: 1, name: "bug", color: "ff0000" },
      { id: 2, name: "docs", color: "0000ff" },
    ];
    const comments = [
      {
        id: 900,
        body: "first",
        html_url: "https://git.example.test/team/project/issues/5#issuecomment-900",
      },
      {
        id: 901,
        body: "second",
        html_url: "https://git.example.test/team/project/issues/5#issuecomment-901",
      },
    ];
    const gitea = mockGitea(({ route }) => {
      if (route === "/labels") return page(labels);
      if (route === "/issues/5/labels") return Response.json([labels[0]]);
      if (route === "/issues/5/comments") return page(comments);
      if (route === "/issues/comments/900") return Response.json(comments[0]);
    });

    const repoLabels = await cli(["label-list"], gitea);
    assert.equal(repoLabels.code, 0, repoLabels.err);
    assert.equal(repoLabels.json().count, 2);
    assert.deepEqual(
      repoLabels.json().items.map((l) => [l.label_id, l.name]),
      [
        [1, "bug"],
        [2, "docs"],
      ],
    );

    const onIssue = await cli(["label-list", "--number", "5"], gitea);
    assert.equal(onIssue.code, 0, onIssue.err);
    assert.equal(onIssue.json().issue_number, 5);
    assert.equal(onIssue.json().items[0].label_id, 1);

    const listed = await cli(["comment-list", "--number", "5"], gitea);
    assert.equal(listed.code, 0, listed.err);
    assert.equal(listed.json().issue_number, 5);
    assert.equal(listed.json().count, 2);
    assert.deepEqual(
      listed.json().items.map((c) => c.comment_id),
      [900, 901],
    );

    const one = await cli(["comment-get", "--comment-id", "900"], gitea);
    assert.equal(one.code, 0, one.err);
    assert.equal(one.json().comment_id, 900);
    assert.equal(one.json().html_url, comments[0].html_url);
    assert.equal(one.json().comment.body, "first");
  });

  it("reads back issue state changes and verifies comment deletion", async () => {
    let issue = { id: 31, number: 3, state: "open", title: "T" };
    let comment = { id: 800, body: "x" };
    const gitea = mockGitea(({ route, method, body }) => {
      if (route === "/issues/3" && method === "PATCH") issue = { ...issue, ...body };
      if (route === "/issues/3") return Response.json(issue);
      if (route === "/issues/comments/800" && method === "DELETE") {
        comment = null;
        return new Response(null, { status: 204 });
      }
      if (route === "/issues/comments/800" && comment) return Response.json(comment);
    });
    const closed = (await cli(["issue-close", "--number", "3"], gitea)).json();
    assert.deepEqual([closed.issue_number, closed.issue_id, closed.issue.state], [3, 31, "closed"]);
    assert.equal((await cli(["issue-reopen", "--number", "3"], gitea)).json().issue.state, "open");
    assert.deepEqual((await cli(["comment-delete", "--comment-id", "800"], gitea)).json(), {
      comment_id: 800,
      deleted: true,
      verified: true,
      reconciled: false,
    });

    const sticky = mockGitea(({ route, method }) => {
      if (route === "/issues/comments/801" && method === "DELETE")
        return new Response(null, { status: 204 });
      if (route === "/issues/comments/801") return Response.json({ id: 801 });
    });
    assert.equal(
      (await cli(["comment-delete", "--comment-id", "801"], sticky)).code,
      EXIT_CODES.ambiguous,
    );
  });

  it("adds and removes labels by name and reads them back", async () => {
    const repoLabels = [
      { id: 1, name: "bug" },
      { id: 2, name: "needs review" },
    ];
    let onIssue = [];
    const gitea = mockGitea(({ route, method, body }) => {
      if (route === "/labels") return page(repoLabels);
      if (route === "/issues/4/labels" && method === "POST")
        onIssue = repoLabels.filter((l) => body.labels.includes(l.id));
      const del = /^\/issues\/4\/labels\/(\d+)$/.exec(route);
      if (del && method === "DELETE") {
        onIssue = onIssue.filter((l) => l.id !== Number(del[1]));
        return new Response(null, { status: 204 });
      }
      if (route === "/issues/4/labels") return Response.json(onIssue);
    });
    const added = await cli(
      ["label-add", "--number", "4", "--label", "bug", "--label", "needs review"],
      gitea,
    );
    assert.deepEqual(
      added.json().labels.map((l) => l.label_id),
      [1, 2],
    );
    const removed = await cli(["label-remove", "--number", "4", "--label=bug"], gitea);
    assert.deepEqual(
      removed.json().labels.map((l) => l.name),
      ["needs review"],
    );
    assert.equal(
      (await cli(["label-add", "--number", "4", "--label", "nope"], gitea)).code,
      EXIT_CODES.usage,
    );
  });

  it("creates COMMENT reviews against the head SHA and refuses approvals", async () => {
    const gitea = mockGitea(({ route, method, body }) => {
      if (route === "/pulls/7") return Response.json({ number: 7, head: { sha: "c".repeat(40) } });
      if (route === "/pulls/7/reviews" && method === "POST") {
        assert.equal(body.commit_id, "c".repeat(40));
        return Response.json({ id: 55, ...body });
      }
      if (route === "/pulls/7/reviews/55") return Response.json({ id: 55, state: "COMMENT" });
      if (route === "/pulls/7/reviews/55/comments")
        return Response.json([{ id: 90, pull_request_review_id: 55, path: "a.js", body: "nit" }]);
    });
    const dir = mkdtempSync(join(tmpdir(), "gitea-review-"));
    try {
      const file = join(dir, "c.json");
      writeFileSync(file, JSON.stringify([{ path: "a.js", body: "nit", new_position: 3 }]));
      const review = await cli(
        ["pr-review-create", "--number", "7", "--body", "Looks off", "--comments-file", file],
        gitea,
      );
      assert.equal(review.code, 0, review.err);
      assert.equal(review.json().review_id, 55);
      assert.deepEqual(review.json().comments[0].review_comment_id, 90);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
    const approve = await cli(
      ["pr-review-create", "--number", "7", "--body", "ok", "--event", "APPROVED"],
      gitea,
    );
    assert.equal(approve.code, EXIT_CODES.policy_denied);
    assert.equal(gitea.calls.filter((c) => c.method === "POST").length, 1);
  });

  it("covers repository, branch, issue, comment-edit and PR detail reads", async () => {
    const sha = "d".repeat(40);
    let issue = null;
    let comment = { id: 900, body: "old", issue_url: `${REPO}/issues/12` };
    const gitea = mockGitea(({ route, method, body }) => {
      if (route === "") return Response.json({ full_name: "team/project", default_branch: "main" });
      if (route === "/branches/main")
        return Response.json({ name: "main", protected: true, commit: { id: sha } });
      if (route === "/labels") return page([{ id: 3, name: "bug" }]);
      if (route === "/issues" && method === "POST") {
        issue = { id: 120, number: 12, ...body };
        return Response.json(issue, { status: 201 });
      }
      if (route === "/issues/12" && method === "PATCH") issue = { ...issue, ...body };
      if (route === "/issues/12") return Response.json(issue);
      if (route === "/issues/comments/900" && method === "PATCH") comment = { ...comment, ...body };
      if (route === "/issues/comments/900") return Response.json(comment);
      if (route === "/pulls/7/commits") return page([{ sha }]);
      if (route === "/pulls/7/files") return page([{ filename: "a.js", status: "modified" }]);
      if (route === "/pulls/7/reviews") return page([{ id: 55, state: "COMMENT", commit_id: sha }]);
      if (route === "/pulls/7/reviews/55") return Response.json({ id: 55 });
      if (route === "/pulls/7/reviews/55/comments")
        return Response.json([{ id: 91, pull_request_review_id: 55, path: "a.js", body: "x" }]);
    });
    assert.equal((await cli(["repo-get"], gitea)).json().default_branch, "main");
    assert.deepEqual((await cli(["default-branch"], gitea)).json(), {
      default_branch: "main",
      sha,
    });
    const branch = (await cli(["branch-get", "--branch", "main"], gitea)).json();
    assert.deepEqual([branch.branch, branch.sha, branch.protected], ["main", sha, true]);
    assert.equal((await cli(["branch-get", "--branch", "feat/a b"], gitea)).code, EXIT_CODES.usage);

    const created = await cli(
      ["issue-create", "--title", "Bug", "--body", "x\ny", "--label", "bug"],
      gitea,
    );
    assert.equal(created.code, 0, created.err);
    assert.deepEqual([created.json().issue_number, created.json().issue_id], [12, 120]);
    assert.deepEqual(issue.labels, [3]);
    const updated = await cli(["issue-update", "--number", "12", "--title", "Bug 2"], gitea);
    assert.equal(updated.json().issue.title, "Bug 2");

    const edited = await cli(["comment-edit", "--comment-id", "900", "--body", "new"], gitea);
    assert.deepEqual([edited.json().comment_id, edited.json().issue_number], [900, 12]);
    assert.equal(edited.json().comment.body, "new");

    assert.equal((await cli(["pr-commits", "--number", "7"], gitea)).json().items[0].sha, sha);
    assert.equal(
      (await cli(["pr-files", "--number", "7"], gitea)).json().items[0].filename,
      "a.js",
    );
    assert.equal(
      (await cli(["pr-review-list", "--number", "7"], gitea)).json().items[0].review_id,
      55,
    );
    const review = (
      await cli(["pr-review-get", "--number", "7", "--review-id", "55"], gitea)
    ).json();
    assert.deepEqual([review.review_id, review.comments[0].review_comment_id], [55, 91]);
    const threads = (await cli(["pr-review-comments", "--number", "7"], gitea)).json();
    assert.deepEqual([threads.count, threads.items[0].review_id], [1, 55]);
  });

  it("parses --name=value and --name value without mangling and rejects ambiguity", () => {
    const spec = { body: "string", label: "list", number: "string", all: "bool" };
    assert.deepEqual(
      parseArgs(["--body=a=b,c", "--label", "x,y", "--label=z", "--pr", "3", "--all"], spec),
      {
        body: "a=b,c",
        label: ["x,y", "z"],
        number: "3",
        all: true,
      },
    );
    assert.equal(parseArgs(["--body", "--looks-like-a-flag"], spec).body, "--looks-like-a-flag");
    assert.throws(() => parseArgs(["--body", "a", "--body", "b"], spec), /more than once/);
    assert.throws(() => parseArgs(["--merge"], spec), /unknown option --merge/);
    assert.throws(() => parseArgs(["--body"], spec), /requires a value/);
    assert.throws(() => parseArgs(["positional"], spec), /--name value/);
  });

  it("prints text format and help, and exposes no merge command", async () => {
    const gitea = mockGitea(({ route }) =>
      route === "/issues" ? page([{ id: 1, number: 2, title: "A\nB", state: "open" }]) : undefined,
    );
    const text = await cli(["issue-list", "--format", "text"], gitea);
    assert.equal(text.out, "issue_number=2\tnumber=2\tstate=open\ttitle=A B\n");
    const help = await cli(["--help"], gitea);
    assert.match(help.out, /review-findings/);
    assert.match(help.out, /14 {2}ambiguous/);
    for (const command of ["merge", "pr-merge", "approve", "repo-delete", "request"])
      assert.equal((await cli([command, "--number", "1"], gitea)).code, EXIT_CODES.usage);
    assert.equal(new Set(Object.values(EXIT_CODES)).size, Object.keys(EXIT_CODES).length);
  });
});

describe("argus-gitea-api security", () => {
  it("classifies HTTP failures with distinct exit codes and no echoed secrets", async () => {
    for (const [status, code] of [
      [401, "unauthorized"],
      [403, "forbidden"],
      [404, "not_found"],
      [409, "conflict"],
      [422, "validation_failed"],
      [429, "rate_limited"],
      [500, "server_error"],
      [418, "http_error"],
    ]) {
      const gitea = mockGitea(() =>
        Response.json(
          { message: `denied token ${CANARY}`, connect_url: `https://u:${CANARY}@x` },
          { status, headers: { "x-gateway-token": CANARY } },
        ),
      );
      const result = await cli(["issue-get", "--number", "5"], gitea);
      assert.equal(result.code, EXIT_CODES[code], `${status}`);
      assert.deepEqual(
        { ...result.error(), message: undefined },
        { code, status, method: "GET", path: "/issues/5", message: undefined },
      );
      assert.ok(!result.err.includes(CANARY) && !result.out.includes(CANARY), `${status} leaked`);
    }
    assert.ok(!sanitizeMessage(`bearer ${CANARY}`).includes(CANARY));
  });

  it("refuses repositories outside the allowlist and cross-repository paths", async () => {
    const gitea = mockGitea(() => Response.json({}));
    for (const repository of [
      "https://git.example.test/team/other",
      "https://git.example.test/team/project/../other",
      "https://git.example.test/team/%2e%2e/other",
      "http://git.example.test/team/project",
      "https://user:pw@git.example.test/team/project",
      "https://elsewhere.test/team/project",
    ]) {
      const result = await cli(["repo-get", "--repository", repository], gitea);
      assert.equal(result.code, EXIT_CODES.policy_denied, repository);
    }
    const bad = await cli(["repo-get"], { ...gitea, env: { ARGUS_REPOSITORIES_JSON: "{}" } });
    assert.equal(bad.code, EXIT_CODES.policy_denied);
    assert.equal(gitea.calls.length, 0);
  });

  it("rejects non-integer ids and unsafe refs before any request", async () => {
    const gitea = mockGitea(() => Response.json({}));
    for (const argv of [
      ["get", "--number", "1/../2"],
      ["get", "--number", "1e3"],
      ["get", "--number", "-1"],
      ["get", "--number", "0x10"],
      ["get", "--number", "https://git.example.test/team/other/pulls/1"],
      ["comment-get", "--comment-id", "%2e%2e"],
      ["run-get", "--run-id", "../../other/repo"],
      ["branch-get", "--branch", "../other"],
      ["branch-get", "--branch", "a/%2e%2e/b"],
      ["branch-get", "--branch", "/abs"],
      ["statuses", "--sha", "../../x"],
    ]) {
      const result = await cli(argv, gitea);
      assert.equal(result.code, EXIT_CODES.usage, argv.join(" "));
      assert.ok(!result.err.includes("other/repo"), "argv values are not echoed");
    }
    assert.equal(gitea.calls.length, 0);
    const api = new GiteaApi(REPO, [REPO], gitea.fetch);
    for (const path of ["/../../other/repo", "/%2e%2e/x", "//evil.test/x", "https://evil.test/x"])
      await assert.rejects(api.request(path), /inside/, path);
    assert.equal(gitea.calls.length, 0);
  });

  it("fails closed without the proxy grant and rejects plain HTTP", async () => {
    let out = "";
    let err = "";
    const code = await main(
      ["repo-get"],
      { ARGUS_REPOSITORY_URL: REPO, ARGUS_REPOSITORIES_JSON: JSON.stringify({ p: REPO }) },
      { stdout: { write: (s) => (out += s) }, stderr: { write: (s) => (err += s) } },
    );
    assert.equal(code, EXIT_CODES.policy_denied);
    assert.match(JSON.parse(err).error.message, /proxy is not configured/);
    assert.equal(out, "");
    const gateway = createGatewayFetch({
      proxy: "http://127.0.0.1:9",
      fetchImpl: async () => Response.json({}),
    });
    assert.throws(
      () => gateway("http://git.example.test/api/v1/repos/team/project"),
      /must use HTTPS/,
    );
  });

  it("refuses API redirects and off-origin download redirects without leaking auth", async () => {
    const api = mockGitea(({ redirect }) => {
      assert.equal(redirect, "error");
      throw new TypeError("fetch failed", { cause: new Error("unexpected redirect") });
    });
    const refused = await cli(["get", "--number", "7"], api);
    assert.equal(refused.code, EXIT_CODES.policy_denied);
    assert.equal(api.calls.length, 1, "redirect refusal is not retried");

    for (const evil of [`http://evil.test/${CANARY}`, `https://evil.test/${CANARY}`]) {
      const dl = mockGitea(({ route, headers }) => {
        if (route === "/actions/jobs/1/logs")
          return new Response(null, {
            status: 302,
            headers: { location: evil },
          });
        assert.fail(`unexpected follow request to ${route} with ${JSON.stringify(headers)}`);
      });
      const insecure = await cli(["job-logs", "--job-id", "1"], dl);
      assert.equal(insecure.code, EXIT_CODES.policy_denied, evil);
      assert.equal(dl.calls.length, 1, `must not follow ${evil}`);
      assert.ok(!insecure.err.includes(CANARY));
      assert.ok(
        dl.calls.every((c) => !Object.keys(c.headers).some((h) => /authorization|cookie/i.test(h))),
      );
    }

    // Positive: same-repo web/raw path under /{owner}/{repo}/…
    const sameRepoWeb = mockGitea(({ route, url, headers }) => {
      if (route === "/actions/jobs/2/logs")
        return new Response(null, {
          status: 302,
          headers: { location: `${REPO}/raw/job-2.log` },
        });
      if (url.href === `${REPO}/raw/job-2.log`) {
        assert.deepEqual(Object.keys(headers).sort(), ["Accept"]);
        assert.equal(headers.Accept, "*/*");
        return new Response("log-ok", { status: 200 });
      }
      assert.fail(`unexpected request ${url.href}`);
    });
    const followed = await cli(["job-logs", "--job-id", "2"], sameRepoWeb);
    assert.equal(followed.code, 0);
    assert.equal(followed.out, "log-ok");
    assert.equal(sameRepoWeb.calls.length, 2);

    // Positive: same-repo API namespace under /api/v1/repos/{owner}/{repo}/…
    const sameRepoApi = mockGitea(({ route, url, headers }) => {
      if (route === "/actions/jobs/3/logs")
        return new Response(null, {
          status: 302,
          headers: {
            location: `${new URL(REPO).origin}/api/v1/repos/team/project/actions/artifacts/9/download`,
          },
        });
      if (url.pathname === "/api/v1/repos/team/project/actions/artifacts/9/download") {
        assert.deepEqual(Object.keys(headers).sort(), ["Accept"]);
        return new Response("artifact-ok", { status: 200 });
      }
      assert.fail(`unexpected request ${url.href}`);
    });
    const apiFollow = await cli(["job-logs", "--job-id", "3"], sameRepoApi);
    assert.equal(apiFollow.code, 0);
    assert.equal(apiFollow.out, "artifact-ok");

    // Negative: same-origin but CROSS-REPO / CROSS-TENANT path must not follow.
    for (const cross of [
      `${new URL(REPO).origin}/other-org/other-repo/raw/secret.log`,
      `${new URL(REPO).origin}/api/v1/repos/other-org/other-repo/actions/jobs/1/logs`,
      `${new URL(REPO).origin}/team/evil-sibling/raw/x`,
      `${new URL(REPO).origin}/robots.txt`,
      `${new URL(REPO).origin}/api/v1/user`,
      // /storage/ is not a free pass for non-artifact downloads.
      `${new URL(REPO).origin}/storage/a/41.zip?sig=x`,
    ]) {
      const crossRepo = mockGitea(({ route, headers }) => {
        if (route === "/actions/jobs/4/logs")
          return new Response(null, { status: 302, headers: { location: cross } });
        assert.fail(`must not follow cross-repo redirect to ${cross} (${JSON.stringify(headers)})`);
      });
      const denied = await cli(["job-logs", "--job-id", "4"], crossRepo);
      assert.equal(denied.code, EXIT_CODES.policy_denied, cross);
      assert.equal(crossRepo.calls.length, 1, `must not follow ${cross}`);
      assert.match(denied.error().message, /namespace|repository/, cross);
    }

    // Artifact evidence: final segment must be `{id}.zip` or `{id}` for that artifact.
    const wrongStorage = mockGitea(({ route, headers }) => {
      if (route === "/actions/artifacts/41/zip")
        return new Response(null, {
          status: 302,
          headers: { location: `${new URL(REPO).origin}/storage/a/99.zip?sig=x` },
        });
      assert.fail(`must not follow mismatched artifact storage (${JSON.stringify(headers)})`);
    });
    const artDir = mkdtempSync(join(tmpdir(), "gitea-art-"));
    try {
      const wrongId = await cli(["artifact-download", "--artifact-id", "41", "--out", "w.zip"], {
        ...wrongStorage,
        cwd: artDir,
      });
      assert.equal(wrongId.code, EXIT_CODES.policy_denied);
      assert.equal(wrongStorage.calls.length, 1);

      // Negative: interior segment equal to the id is not enough (/storage/x/41/other.zip).
      const interiorStorage = mockGitea(({ route, headers }) => {
        if (route === "/actions/artifacts/41/zip")
          return new Response(null, {
            status: 302,
            headers: {
              location: `${new URL(REPO).origin}/storage/x/41/other.zip?sig=x`,
            },
          });
        assert.fail(`must not follow interior-id storage path (${JSON.stringify(headers)})`);
      });
      const interior = await cli(["artifact-download", "--artifact-id", "41", "--out", "i.zip"], {
        ...interiorStorage,
        cwd: artDir,
      });
      assert.equal(interior.code, EXIT_CODES.policy_denied);
      assert.equal(interiorStorage.calls.length, 1);

      // Positive: bare final segment `{id}` (not only `{id}.zip`) is authorized.
      const bareZip = Buffer.from("bare-artifact");
      const bareStorage = mockGitea(({ route, headers }) => {
        if (route === "/actions/artifacts/41/zip")
          return new Response(null, {
            status: 302,
            headers: { location: `${new URL(REPO).origin}/storage/a/41?sig=x` },
          });
        if (route === `${new URL(REPO).origin}/storage/a/41?sig=x`) {
          assert.deepEqual(Object.keys(headers), ["Accept"]);
          return new Response(bareZip, { headers: { "content-type": "application/zip" } });
        }
      });
      const bare = await cli(["artifact-download", "--artifact-id", "41", "--out", "bare.zip"], {
        ...bareStorage,
        cwd: artDir,
      });
      assert.equal(bare.code, 0, bare.err);
      assert.equal(bareStorage.calls.length, 2);
      assert.deepEqual(readFileSync(join(artDir, "bare.zip")), bareZip);
    } finally {
      rmSync(artDir, { recursive: true, force: true });
    }
  });

  it("updatePullRequest library return stays a PR object (legacy shape)", async () => {
    let pr = {
      number: 7,
      title: "Old",
      body: "",
      state: "open",
      html_url: "https://git.example.test/team/project/pulls/7",
      head: { sha: "abc", ref: "topic" },
      base: { ref: "main" },
    };
    const gitea = mockGitea(({ route, method, body }) => {
      if (route === "" && method === "GET")
        return Response.json({ full_name: "team/project", default_branch: "main" });
      if (route === "/pulls/7" && method === "PATCH") {
        pr = { ...pr, title: body.title };
        return Response.json(pr);
      }
      if (route === "/pulls/7") return Response.json(pr);
    });
    const api = new GiteaApi(REPO, [REPO], gitea.fetch);
    const result = await api.updatePullRequest(7, { title: "Legacy shape" });
    assert.equal(result.number, 7);
    assert.equal(result.title, "Legacy shape");
    assert.equal(result.head.sha, "abc");
    assert.equal(Object.hasOwn(result, "reconciled"), false);
    assert.equal(Object.hasOwn(result, "pull"), false);
    // getRepository stays a bare repo payload (not a mutator wrapper).
    const repo = await api.getRepository();
    assert.equal(repo.full_name, "team/project");
    assert.equal(repo.reconciled, undefined);
    assert.equal(repo.pull, undefined);
  });
});
