import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { createServer as createHttpServer } from "node:http";
import { createServer as createHttpsServer } from "node:https";
import { connect } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import { ProxyAgent } from "undici";
import { createGatewayFetch, GiteaApi } from "./gitea-api.mjs";

describe("credential-free Gitea API helper", () => {
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

  it("rejects hosts/repos outside the exact HTTPS allowlist and has no merge operation", async () => {
    assert.throws(
      () =>
        new GiteaApi("https://git.example.test/other/project", [
          "https://git.example.test/team/project",
        ]),
      /not configured/,
    );
    assert.throws(
      () =>
        new GiteaApi("http://git.example.test/team/project", [
          "https://git.example.test/team/project",
        ]),
      /HTTPS/,
    );
    const api = new GiteaApi("https://git.example.test/team/project", [
      "https://git.example.test/team/project",
    ]);
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

  it("sends an actual Node fetch through an HTTPS CONNECT proxy", async () => {
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
      const gatewayFetch = createGatewayFetch({
        proxy: `http://127.0.0.1:${proxy.address().port}`,
        requestTls: { rejectUnauthorized: false },
      });
      const api = new GiteaApi(
        "https://git.example.test/team/project",
        ["https://git.example.test/team/project"],
        gatewayFetch,
      );
      assert.deepEqual(await api.request("/issues"), { throughProxy: true });
      assert.equal(connectTarget, "git.example.test:443");
    } finally {
      await close(proxy);
      await close(origin);
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
