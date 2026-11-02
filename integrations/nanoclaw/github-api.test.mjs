import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { GiteaApi, preflightEvidence } from "./gitea-api.mjs";
import { GitHubApi, main, repositoryClient, repositoryProvider } from "./github-api.mjs";

const REPO = "https://github.com/acme/widget";
const API = "https://api.github.com/repos/acme/widget";
const ENV = { ARGUS_REPOSITORIES_JSON: JSON.stringify({ widget: REPO }) };

/** A fetch that answers from `routes` (`"METHOD path"` → body or Response) and records calls. */
function forge(routes) {
  const calls = [];
  const fetch = async (url, init = {}) => {
    const method = init.method ?? "GET";
    calls.push({ url, method, headers: init.headers ?? {}, body: init.body });
    const path = url.startsWith(API) ? url.slice(API.length) : url;
    const key = `${method} ${path.split("?")[0]}`;
    const answer = routes[key];
    if (answer === undefined) return new Response("{}", { status: 404 });
    const value = typeof answer === "function" ? answer(url, init) : answer;
    return value instanceof Response ? value : Response.json(value);
  };
  return { fetch, calls };
}

async function run(argv, routes) {
  const { fetch, calls } = forge(routes);
  let out = "";
  let err = "";
  const code = await main([...argv, "--repository", REPO], ENV, {
    fetch,
    sleep: async () => {},
    stdout: { write: (c) => (out += c) },
    stderr: { write: (c) => (err += c) },
  });
  return { code, out, err, calls, json: () => JSON.parse(out) };
}

const client = (routes) => {
  const { fetch, calls } = forge(routes);
  return { api: new GitHubApi(REPO, [REPO], fetch, { sleep: async () => {} }), calls };
};

describe("repo-preflight (offline logic; production proof is the session child)", () => {
  const repo = (extra = {}) => ({
    "GET ": { full_name: "Acme/Widget", permissions: { pull: true, push: true }, ...extra },
  });

  it("passes only for a credential with write access and checks the allowlist denials", async () => {
    const r = await run(["repo-preflight"], repo());
    assert.equal(r.code, 0, r.err);
    assert.deepEqual(r.json(), {
      ok: true,
      provider: "github",
      repository: REPO,
      authenticated: true,
      write: true,
      read: true,
      allowlist_refuses_other_host: true,
      allowlist_refuses_sibling: true,
      evidence: {
        http_status: 200,
        permissions: "present",
        push: true,
        visibility: "unknown",
        authentication: "proven",
      },
    });
    assert.deepEqual(
      r.calls.map((c) => c.url),
      [API],
    );
  });

  it("refuses a response an anonymous caller could get (no push permission)", async () => {
    for (const permissions of [{ pull: true, push: false }, { pull: true }, undefined]) {
      const r = await run(["repo-preflight"], repo({ permissions }));
      assert.equal(r.code, 13, r.out);
      assert.match(JSON.parse(r.err).error.message, /does not show write access/);
    }
  });

  it("read mode accepts pull without push and does not call it authenticated on a public repo", async () => {
    const r = await run(
      ["repo-preflight", "--access", "read"],
      repo({ private: false, permissions: { pull: true, push: false } }),
    );
    assert.equal(r.code, 0, r.err);
    assert.equal(r.json().write, false);
    assert.equal(r.json().read, true);
    assert.equal(r.json().authenticated, false);
    assert.equal(r.json().evidence.authentication, "unknown");
  });

  it("records closed evidence; only push or a private answer proves authentication", () => {
    const cases = [
      [
        { private: false, permissions: { pull: true, push: false } },
        "present",
        false,
        "public",
        "unknown",
      ],
      [{ private: false }, "absent", null, "public", "unknown"],
      [{ full_name: "acme/widget" }, "absent", null, "unknown", "unknown"],
      [
        { private: true, permissions: { pull: true, push: false } },
        "present",
        false,
        "private",
        "proven",
      ],
      [{ private: false, permissions: { push: true } }, "present", true, "public", "proven"],
      [{ permissions: { push: "yes" } }, "present", null, "unknown", "unknown"],
    ];
    for (const [data, permissions, push, visibility, authentication] of cases)
      assert.deepEqual(preflightEvidence(data), {
        http_status: 200,
        permissions,
        push,
        visibility,
        authentication,
      });
    assert.equal(preflightEvidence(null, 401).http_status, 401);
    assert.equal(preflightEvidence(null, null).http_status, null);
  });

  it("read mode refuses when the credential cannot read", async () => {
    const r = await run(["repo-preflight", "--access", "read"], repo({ permissions: {} }));
    assert.equal(r.code, 13);
    assert.match(JSON.parse(r.err).error.message, /read access/);
  });

  it("uses only permissions.push on the repository GET as the write signal (not scope headers)", async () => {
    const r = await run(["repo-preflight"], {
      "GET ": { full_name: "Acme/Widget", permissions: { pull: true, push: false } },
    });
    assert.equal(r.code, 13);
    assert.equal(r.calls.length, 1);
  });

  it("refuses a different repository, an HTTP error and a result file outside /preflight", async () => {
    assert.equal((await run(["repo-preflight"], repo({ full_name: "evil/other" }))).code, 15);
    assert.equal(
      (await run(["repo-preflight"], { "GET ": new Response("{}", { status: 401 }) })).code,
      3,
    );
    const bad = await run(["repo-preflight", "--result-file", "/tmp/x"], repo());
    assert.equal(bad.code, 2);
    assert.equal(bad.calls.length, 1);
  });

  it("is refused on another host by the GitHub client", async () => {
    const other = "https://git.example.test/acme/widget";
    let err = "";
    const code = await main(
      ["repo-preflight", "--repository", other],
      {
        ARGUS_REPOSITORIES_JSON: JSON.stringify({ widget: other }),
      },
      {
        fetch: async () => Response.json({}),
        stdout: { write() {} },
        stderr: { write: (c) => (err += c) },
      },
    );
    assert.equal(code, 13, err);
  });
});

describe("credential-free GitHub API helper", () => {
  it("serves github.com only, and callers pick the client by host", () => {
    assert.equal(repositoryProvider(REPO), "github");
    assert.equal(repositoryProvider("https://git.example.test/a/b"), "gitea");
    assert.equal(repositoryProvider("not a url"), "gitea");
    const noop = async () => Response.json({});
    assert.ok(repositoryClient(REPO, [REPO], noop) instanceof GitHubApi);
    const gitea = "https://git.example.test/a/b";
    const other = repositoryClient(gitea, [gitea], noop);
    assert.ok(other instanceof GiteaApi && !(other instanceof GitHubApi));
    assert.throws(() => new GitHubApi(gitea, [gitea], noop), { code: "policy_denied" });
  });

  it("opens a PR against api.github.com with the API version header and no credential", async () => {
    let created;
    const pr = { number: 5, head: { ref: "feat/x", sha: "a".repeat(40) }, base: { ref: "main" } };
    const r = await run(["create", "--title", "T", "--head", "feat/x", "--base", "main"], {
      "GET /pulls": () => (created ? [created] : []),
      "POST /pulls": (_, init) => {
        created = { ...pr, title: JSON.parse(init.body).title };
        return created;
      },
      "GET /pulls/5": () => created,
    });
    assert.equal(r.code, 0, r.err);
    assert.equal(r.json().number, 5);
    const list = new URL(r.calls[0].url);
    assert.equal(list.searchParams.get("head"), "acme:feat/x");
    assert.equal(r.calls[1].method, "POST");
    for (const call of r.calls) {
      assert.equal(call.headers["X-GitHub-Api-Version"], "2022-11-28");
      assert.equal(call.headers.Authorization, undefined);
    }
  });

  it("pages with per_page", async () => {
    const r = await run(["list-prs", "--state", "open", "--limit", "5"], { "GET /pulls": [] });
    assert.equal(r.code, 0, r.err);
    assert.equal(new URL(r.calls[0].url).searchParams.get("per_page"), "5");
    const issues = await run(["issue-list", "--limit", "7"], { "GET /issues": [] });
    assert.equal(new URL(issues.calls[0].url).searchParams.get("per_page"), "7");
  });

  for (const [name, statuses, runs, want] of [
    ["no CI at all", [], [], null],
    [
      "a green status and a green check run",
      [{ id: 1, context: "ci", state: "success" }],
      [{ id: 2, name: "test", status: "completed", conclusion: "success" }],
      "success",
    ],
    [
      "a skipped check run",
      [],
      [{ id: 2, name: "lint", status: "completed", conclusion: "skipped" }],
      "success",
    ],
    [
      "a running check run",
      [{ id: 1, context: "ci", state: "success" }],
      [{ id: 2, name: "test", status: "in_progress", conclusion: null }],
      "pending",
    ],
    [
      "a failed check run behind a green status",
      [{ id: 1, context: "ci", state: "success" }],
      [{ id: 2, name: "test", status: "completed", conclusion: "failure" }],
      "failure",
    ],
    [
      "a timed-out check run",
      [],
      [{ id: 2, name: "test", status: "completed", conclusion: "timed_out" }],
      "failure",
    ],
    ["an error status", [{ id: 1, context: "ci", state: "error" }], [], "failure"],
  ])
    it(`status combines statuses and check runs: ${name}`, async () => {
      const sha = "b".repeat(40);
      const r = await run(["status", "--pr", "3"], {
        "GET /pulls/3": { number: 3, head: { sha } },
        [`GET /commits/${sha}/status`]: { state: "pending", statuses },
        [`GET /commits/${sha}/check-runs`]: { total_count: runs.length, check_runs: runs },
      });
      assert.equal(r.code, 0, r.err);
      const out = r.json();
      assert.equal(out.head_sha, sha);
      assert.equal(out.combined_state, want);
      assert.equal(out.check_runs.length, runs.length);
    });

  it("gives argus-delegate status-shaped entries for statuses and check runs", async () => {
    const sha = "c".repeat(40);
    const { api } = client({
      [`GET /commits/${sha}/status`]: { statuses: [{ id: 1, context: "ci", state: "success" }] },
      [`GET /commits/${sha}/check-runs`]: {
        total_count: 1,
        check_runs: [
          { id: 9, name: "quality / python", status: "completed", conclusion: "failure" },
        ],
      },
    });
    assert.deepEqual(await api.ciStatuses(sha), [
      { id: 1, context: "ci", state: "success" },
      { id: 9, context: "quality / python", state: "failure" },
    ]);
  });

  it("sends labels by name, refuses unknown ones, and removes by replacing the set", async () => {
    let labels = [
      { id: 1, name: "type/bug" },
      { id: 2, name: "p1" },
    ];
    const repoLabels = [...labels, { id: 3, name: "docs" }];
    const routes = {
      "GET /labels": repoLabels,
      "GET /issues/4/labels": () => labels,
      "POST /issues/4/labels": (_, init) => {
        const names = JSON.parse(init.body).labels;
        labels = [...labels, ...repoLabels.filter((l) => names.includes(l.name))];
        return labels;
      },
      "PUT /issues/4/labels": (_, init) => {
        const names = JSON.parse(init.body).labels;
        labels = repoLabels.filter((l) => names.includes(l.name));
        return labels;
      },
    };
    const added = await run(["label-add", "--number", "4", "--label", "docs"], routes);
    assert.equal(added.code, 0, added.err);
    assert.deepEqual(JSON.parse(added.calls.find((c) => c.method === "POST").body), {
      labels: ["docs"],
    });
    const removed = await run(["label-remove", "--number", "4", "--label", "type/bug"], routes);
    assert.equal(removed.code, 0, removed.err);
    const put = removed.calls.find((c) => c.method === "PUT");
    assert.deepEqual(JSON.parse(put.body), { labels: ["p1", "docs"] });
    assert.ok(!removed.calls.some((c) => c.method === "DELETE"));
    assert.deepEqual(
      removed.json().labels.map((l) => l.name),
      ["p1", "docs"],
    );
    const unknown = await run(["label-add", "--number", "4", "--label", "nope"], routes);
    assert.equal(unknown.code, 2);
  });

  it("maps inline review comments to line and side, and never approves", async () => {
    const sha = "d".repeat(40);
    let review;
    const routes = {
      "GET /pulls/8": { number: 8, head: { sha } },
      "POST /pulls/8/reviews": (_, init) => {
        review = { id: 70, ...JSON.parse(init.body) };
        return review;
      },
      "GET /pulls/8/reviews/70": () => review,
      "GET /pulls/8/reviews/70/comments": [],
    };
    const { api, calls } = client(routes);
    await api.createReview(8, {
      body: "two findings",
      comments: [
        { path: "a.py", body: "x", new_position: 12 },
        { path: "b.py", body: "y", old_position: 3 },
      ],
    });
    const sent = JSON.parse(calls.find((c) => c.method === "POST").body);
    assert.equal(sent.commit_id, sha);
    assert.deepEqual(sent.comments, [
      { path: "a.py", body: "x", line: 12, side: "RIGHT" },
      { path: "b.py", body: "y", line: 3, side: "LEFT" },
    ]);
    await assert.rejects(api.createReview(8, { body: "ok", event: "APPROVE" }), {
      code: "policy_denied",
    });
  });

  it("reads the diff through the diff media type", async () => {
    const r = await run(["pr-diff", "--pr", "2"], {
      "GET /pulls/2": () => new Response("diff --git a/x b/x\n"),
    });
    assert.equal(r.code, 0, r.err);
    assert.equal(r.out, "diff --git a/x b/x\n");
    assert.equal(r.calls[0].headers.Accept, "application/vnd.github.diff");
  });

  it("follows log redirects only to signed GitHub storage, without API headers", async () => {
    const signed = "https://results.blob.core.windows.net/logs/1.txt?sig=abc";
    const ok = await run(["job-logs", "--job-id", "11"], {
      "GET /actions/jobs/11/logs": () =>
        new Response(null, { status: 302, headers: { location: signed } }),
      [`GET ${signed.split("?")[0]}`]: () => new Response("log line\n"),
    });
    assert.equal(ok.code, 0, ok.err);
    assert.equal(ok.out, "log line\n");
    assert.deepEqual(Object.keys(ok.calls[1].headers), ["Accept"]);
    for (const location of [
      "https://evil.example/logs/1.txt?sig=abc",
      "https://results.blob.core.windows.net/logs/1.txt",
      "http://results.blob.core.windows.net/logs/1.txt?sig=abc",
    ]) {
      const refused = await run(["job-logs", "--job-id", "11"], {
        "GET /actions/jobs/11/logs": () =>
          new Response(null, { status: 302, headers: { location } }),
      });
      assert.equal(refused.code, 13, location);
      assert.equal(refused.calls.length, 1);
    }
  });

  it("filters issue types client-side and refuses search-only filters", async () => {
    const items = [
      { id: 1, number: 1, title: "bug" },
      { id: 2, number: 2, title: "pr", pull_request: {} },
    ];
    const issues = await run(["issue-list"], { "GET /issues": items });
    assert.deepEqual(
      issues.json().items.map((i) => i.number),
      [1],
    );
    const pulls = await run(["issue-list", "--type", "pulls"], { "GET /issues": items });
    assert.deepEqual(
      pulls.json().items.map((i) => i.number),
      [2],
    );
    for (const argv of [
      ["issue-list", "--q", "x"],
      ["issue-list", "--before", "2026-01-01"],
      ["comment-list", "--number", "1", "--before", "2026-01-01"],
      ["statuses", "--sha", "e".repeat(40), "--state", "success"],
    ]) {
      const r = await run(argv, {});
      assert.equal(r.code, 2, argv.join(" "));
      assert.equal(r.calls.length, 0);
    }
  });

  it("classifies an exhausted rate limit and names GitHub in errors", async () => {
    const r = await run(["get", "--pr", "1"], {
      "GET /pulls/1": () =>
        new Response("{}", { status: 403, headers: { "x-ratelimit-remaining": "0" } }),
    });
    assert.equal(r.code, 8);
    const error = JSON.parse(r.err).error;
    assert.equal(error.code, "rate_limited");
    assert.match(error.message, /^GitHub API GET/);
  });

  it("finds the agentic review in a check run on the head", async () => {
    const sha = "f".repeat(40);
    const r = await run(["review-findings", "--pr", "6"], {
      "GET /pulls/6": { number: 6, head: { sha } },
      "GET /issues/6/comments": [
        {
          id: 40,
          body: `Agentic review — head ${sha.slice(0, 12)}, 0 findings`,
          created_at: "2026-10-27T10:00:00Z",
        },
      ],
      [`GET /commits/${sha}/status`]: { statuses: [] },
      [`GET /commits/${sha}/check-runs`]: {
        total_count: 1,
        check_runs: [
          {
            id: 5,
            name: "Agentic review",
            status: "completed",
            conclusion: "success",
            html_url: "https://github.com/x",
          },
        ],
      },
    });
    assert.equal(r.code, 0, r.err);
    const out = r.json();
    assert.equal(out.matches_head, true);
    assert.equal(out.finding_count, 0);
    assert.equal(out.status.state, "success");
    assert.equal(out.status.context, "Agentic review");
  });

  it("documents itself as argus-github-api", async () => {
    let out = "";
    assert.equal(await main(["--help"], ENV, { stdout: { write: (c) => (out += c) } }), 0);
    assert.match(out, /^usage: argus-github-api /);
    assert.match(out, /Credential-free GitHub client/);
    assert.match(out, /check runs/);
    out = "";
    await main(["create", "--help"], ENV, { stdout: { write: (c) => (out += c) } });
    assert.match(out, /^usage: argus-github-api create /);
  });
});
