/**
 * Repository preflight in REAL containers, against a SYNTHETIC gateway and forge.
 *
 * Runs only when ARGUS_PREFLIGHT_DOCKER_IMAGE names a local image that carries the current
 * helpers (see docs/specs/failure-classes.spec.md 3.3.1). It uses NanoClaw's real
 * DockerSessionDriver and the real docker CLI, a throwaway CA and a local CONNECT proxy on
 * the docker bridge that plays both OneCLI (it "injects" a synthetic credential the container
 * never holds) and the forge. It proves: the container user can write the verdict, the exit
 * status and cleanup behave with a real daemon, and the helper only passes when the forge
 * reports write access. It does NOT prove OneCLI's host/path scoping, a real token's
 * permissions, or any real forge behaviour; no production credential is involved. The
 * "NOT real GitHub authorization" cases run the whole host preflight (lease, synthetic
 * OneCLI assignment read, session) and check its outcome and redacted evidence.
 */
import { execFile, execFileSync, spawnSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import http from "node:http";
import https from "node:https";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { composeSessionSpec } from "../container-runner.js";
import { realCli } from "../drivers/cli.js";
import { DockerSessionDriver } from "../drivers/docker-driver.js";
import type { RepositoryAssignmentCheck } from "./argus-accounts.js";
import {
  ephemeralSessionRunner,
  type GatewayControl,
  type PreflightEvidenceEvent,
  runRepositoryPreflight,
  sessionRepositoryProbe,
} from "./argus-repo-preflight.js";

vi.mock("../log.js", () => ({
  log: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn(), fatal: vi.fn() },
}));

const IMAGE = process.env.ARGUS_PREFLIGHT_DOCKER_IMAGE ?? "";
const BRIDGE = process.env.ARGUS_PREFLIGHT_BRIDGE_IP ?? "172.17.0.1";
const SYNTHETIC = "synthetic-token-not-a-credential";
const GITHUB = "https://github.com/acme/widget";
const GITEA = "https://git.example.test/acme/widget";

interface Forge {
  inject: boolean;
  fullName: string;
  delayMs: number;
  status: number;
  private: boolean | undefined;
  seen: Array<{ host: string; path: string; clientAuth: string | undefined }>;
}

describe.skipIf(!IMAGE)("preflight in real containers (synthetic gateway and forge)", () => {
  let root: string;
  let proxy: http.Server;
  let proxyUrl: string;
  const forge: Forge = {
    inject: true,
    fullName: "acme/widget",
    delayMs: 0,
    status: 200,
    private: undefined,
    seen: [],
  };
  const docker = (...args: string[]) => execFileSync("docker", args, { encoding: "utf8" }).trim();
  const leftovers = () =>
    docker("ps", "-a", "--filter", "label=argus-preflight=1", "--format", "{{.Names}}");

  beforeAll(async () => {
    root = mkdtempSync(path.join(tmpdir(), "argus-preflight-docker-"));
    const trust = path.join(root, "data", "gateway-trust");
    mkdirSync(trust, { recursive: true });
    const key = path.join(root, "key.pem");
    const crt = path.join(trust, "ca.pem");
    const made = spawnSync("openssl", [
      "req",
      "-x509",
      "-newkey",
      "rsa:2048",
      "-nodes",
      "-keyout",
      key,
      "-out",
      crt,
      "-days",
      "1",
      "-subj",
      "/CN=synthetic-gateway-ca",
      "-addext",
      "subjectAltName=DNS:api.github.com,DNS:git.example.test",
    ]);
    if (made.status !== 0) throw new Error(`openssl failed: ${made.stderr}`);
    const forgeServer = https.createServer(
      { key: readFileSync(key), cert: readFileSync(crt) },
      (req, res) => {
        forge.seen.push({
          host: String(req.headers.host),
          path: String(req.url),
          clientAuth: req.headers.authorization,
        });
        // The gateway's job, played here: add the synthetic credential the container never sees.
        const authenticated = forge.inject;
        setTimeout(() => {
          const route = [
            ["api.github.com", "/repos/acme/widget"],
            ["git.example.test", "/api/v1/repos/acme/widget"],
          ].some(([h, p]) => req.headers.host === h && req.url === p);
          const status = route ? forge.status : 404;
          res.writeHead(status, { "content-type": "application/json" });
          res.end(
            JSON.stringify(
              status === 200
                ? {
                    full_name: forge.fullName,
                    private: forge.private,
                    permissions: { pull: true, push: authenticated },
                  }
                : {
                    message:
                      status === 403 ? "Resource not accessible by integration" : "Not Found",
                  },
            ),
          );
        }, forge.delayMs);
      },
    );
    proxy = http.createServer();
    proxy.on("connect", (req, socket, head) => {
      socket.write("HTTP/1.1 200 Connection Established\r\n\r\n");
      if (head.length > 0) socket.unshift(head);
      forgeServer.emit("connection", socket);
      socket.on("error", () => undefined);
    });
    await new Promise<void>((resolve) => proxy.listen(0, BRIDGE, resolve));
    proxyUrl = `http://${BRIDGE}:${(proxy.address() as AddressInfo).port}`;
  });
  afterAll(async () => {
    proxy?.closeAllConnections();
    await new Promise((r) => proxy?.close(r));
    rmSync(root, { recursive: true, force: true });
  });
  beforeEach(() => {
    forge.inject = true;
    forge.fullName = "acme/widget";
    forge.delayMs = 0;
    forge.status = 200;
    forge.private = undefined;
    forge.seen = [];
  });

  function probe(timeoutMs = 60_000, env: Record<string, string> = {}) {
    const policy = {
      groupsRoot: path.join(root, "groups"),
      dataRoot: path.join(root, "data"),
      surfaceRoots: [],
      materialsRoot: path.join(root, "data", "session-materials"),
      gatewayTrustRoot: path.join(root, "data", "gateway-trust"),
    };
    const driver = new DockerSessionDriver({ ...policy, cli: realCli("docker") });
    const run = ephemeralSessionRunner({
      installSlug: "preflighttest",
      startupReady: async () => undefined,
      group: async () => ({ id: "group-1", name: "Argus" }),
      sessionDir: (g, s) => path.join(policy.dataRoot, "v2-sessions", g, s),
      driver: () => driver as never,
      ensureGateway: async () =>
        ({
          lease: {
            contribution: {
              env: { HTTPS_PROXY: proxyUrl, NODE_EXTRA_CA_CERTS: "/gw/ca.pem", ...env },
              mounts: [
                {
                  class: "gateway-trust",
                  hostPath: path.join(policy.gatewayTrustRoot, "ca.pem"),
                  containerPath: "/gw/ca.pem",
                  mode: "ro",
                  groupScope: "group-1",
                },
              ],
            },
          },
          controller: new AbortController(),
        }) as unknown as GatewayControl,
      releaseGateway: async () => undefined,
      compose: async ({ groupId, sessionId, containerName, gateway }) =>
        composeSessionSpec({
          agentGroup: { id: groupId, name: "Argus", folder: "argus" } as never,
          session: { id: sessionId, agent_group_id: groupId, agent_provider: null } as never,
          containerName,
          mounts: [],
          containerConfig: { imageTag: IMAGE } as never,
          contribution: {} as never,
          gateway: {
            ...gateway,
            networkAccess: { endpoint: "host.docker.internal", target: { kind: "host" } },
          } as never,
          mailboxEnvironment: {},
        }) as never,
      pollMs: 200,
    });
    return sessionRepositoryProbe(run, timeoutMs);
  }

  it("github: the container user writes the verdict; passes only through the injected path", async () => {
    await expect(probe()(GITHUB, [GITHUB], "github")).resolves.toMatchObject({
      authentication: "proven",
    });
    expect(forge.seen).toEqual([
      { host: "api.github.com", path: "/repos/acme/widget", clientAuth: undefined },
    ]);
    expect(leftovers()).toBe("");
  }, 90_000);

  it("gitea: same flow against another host", async () => {
    await expect(probe()(GITEA, [GITEA], "gitea")).resolves.toMatchObject({
      authentication: "proven",
    });
    expect(forge.seen[0]).toMatchObject({
      host: "git.example.test",
      path: "/api/v1/repos/acme/widget",
    });
    expect(leftovers()).toBe("");
  }, 90_000);

  it("an unauthenticated read (no injection) is refused by exit status and verdict", async () => {
    forge.inject = false;
    await expect(probe()(GITHUB, [GITHUB], "github")).rejects.toThrow(
      /exited with code 13 \(helper error policy_denied\)/,
    );
    expect(leftovers()).toBe("");
  }, 90_000);

  it("a forge answering for another repository is refused", async () => {
    forge.fullName = "evil/other";
    await expect(probe()(GITHUB, [GITHUB], "github")).rejects.toThrow(/exited with code 15/);
    expect(leftovers()).toBe("");
  }, 90_000);

  it("no proxy for the session: refused before a container exists", async () => {
    await expect(probe(60_000, { HTTPS_PROXY: "" })(GITHUB, [GITHUB], "github")).rejects.toThrow(
      /did not configure an HTTPS proxy/,
    );
    expect(leftovers()).toBe("");
  }, 30_000);

  it("a container killed mid-run is a non-zero exit and leaves nothing", async () => {
    forge.delayMs = 20_000;
    const pending = probe(60_000)(GITHUB, [GITHUB], "github");
    const caught = pending.catch((e: Error) => e);
    for (let i = 0; i < 60 && !leftovers(); i++) await new Promise((r) => setTimeout(r, 250));
    docker("kill", leftovers().split("\n")[0]);
    expect(String(await caught)).toMatch(/exited with code 137/);
    expect(leftovers()).toBe("");
  }, 90_000);

  it("a forge that never answers hits the one deadline and the container is removed", async () => {
    forge.delayMs = 120_000;
    const started = Date.now();
    await expect(probe(8_000)(GITHUB, [GITHUB], "github")).rejects.toThrow();
    expect(Date.now() - started).toBeLessThan(40_000);
    expect(leftovers()).toBe("");
  }, 90_000);

  describe("the host preflight end to end (synthetic forge, NOT real GitHub authorization)", () => {
    const assigned: RepositoryAssignmentCheck = {
      agent: "found",
      secretMode: "selective",
      rest: { state: "assigned", scope: "match" },
      git: { state: "assigned", scope: "match" },
      scopes: {
        rest: { host: "api.github.com", path: "/repos/acme/widget/*" },
        git: { host: "github.com", path: "/acme/widget.git/*" },
      },
    };
    const preflight = async (
      check: RepositoryAssignmentCheck,
      env: Record<string, string> = {},
    ) => {
      const events: PreflightEvidenceEvent[] = [];
      const result = await runRepositoryPreflight({
        taskId: "task-docker",
        attempt: 1,
        repositoryUrl: GITHUB,
        access: "write",
        lease: { lease_id: "lease-docker", provider: "github", role: "repository" },
        verifyAssignment: async () => check,
        probe: () => probe(60_000, env)(GITHUB, [GITHUB], "github", "write", "group-1"),
        emit: (event) => {
          events.push(structuredClone(event));
        },
      });
      return { result, events };
    };

    it("injected: the assignment is confirmed and the forge proves write access", async () => {
      const { result, events } = await preflight(assigned);
      expect(result).toMatchObject({ outcome: "ok", reason: null });
      expect(events[0].probe).toMatchObject({ push: true, authentication: "proven" });
      expect(forge.seen.map((s) => s.clientAuth)).toEqual([undefined]);
      expect(leftovers()).toBe("");
    }, 90_000);

    it("absent assignment: blocked as unavailable with no container and no forge request", async () => {
      const { result } = await preflight({
        ...assigned,
        git: { state: "unassigned", scope: "match" },
      });
      expect(result).toMatchObject({
        outcome: "assignment_missing",
        reason: "env_repository_unavailable",
      });
      expect(forge.seen).toEqual([]);
      expect(leftovers()).toBe("");
    }, 30_000);

    it("denied: a forge 403 is forge_denied, not a missing assignment", async () => {
      forge.status = 403;
      const { result, events } = await preflight(assigned);
      expect(result).toMatchObject({
        outcome: "forge_denied",
        reason: "env_repository_unreachable",
      });
      expect(events[0].probe).toMatchObject({ helper_error: "forbidden", http_status: 403 });
      expect(leftovers()).toBe("");
    }, 90_000);

    it("no injection on a public repository is unknown, never called anonymous or denied", async () => {
      forge.inject = false;
      forge.private = false;
      const { result, events } = await preflight(assigned);
      expect(result.outcome).toBe("unknown");
      expect(events[0].probe).toMatchObject({
        visibility: "public",
        push: false,
        authentication: "unknown",
      });
      expect(JSON.stringify(events)).not.toMatch(/anonymous/i);
      expect(leftovers()).toBe("");
    }, 90_000);

    it("redaction: proxy userinfo never reaches the error or the evidence", async () => {
      forge.status = 403;
      const secret = "s3cr3t-proxy-userinfo";
      const withUserinfo = proxyUrl.replace("http://", `http://argus:${secret}@`);
      const { result, events } = await preflight(assigned, { HTTPS_PROXY: withUserinfo });
      expect(result.outcome).not.toBe("ok");
      expect(result.error ?? "").not.toContain(secret);
      expect(JSON.stringify(events)).not.toContain(secret);
      expect(JSON.stringify(events)).not.toContain(SYNTHETIC);
      expect(leftovers()).toBe("");
    }, 90_000);
  });

  describe("the helper's result file inside a container", () => {
    // Async: the synthetic gateway lives in this process, so a blocking spawn would deadlock it.
    const direct = (dir: string, user: string) =>
      new Promise<{ status: number | null; stderr: string }>((resolve) =>
        execFile(
          "docker",
          [
            "run",
            "--rm",
            "--user",
            user,
            "-v",
            `${dir}:/preflight`,
            "-v",
            `${path.join(root, "data", "gateway-trust", "ca.pem")}:/gw/ca.pem:ro`,
            "-e",
            `HTTPS_PROXY=${proxyUrl}`,
            "-e",
            "NODE_EXTRA_CA_CERTS=/gw/ca.pem",
            "-e",
            `ARGUS_REPOSITORIES_JSON=${JSON.stringify({ r: GITHUB })}`,
            "--entrypoint",
            "/usr/local/bin/argus-github-api",
            IMAGE,
            "repo-preflight",
            "--repository",
            GITHUB,
            "--result-file",
            "/preflight/result.json",
          ],
          { encoding: "utf8" },
          (error, _out, stderr) =>
            resolve({ status: error ? ((error as { code?: number }).code ?? 1) : 0, stderr }),
        ),
      );

    it("is owner-only, written by the container uid, and never follows a planted symlink", async () => {
      const uid = process.getuid?.() ?? 1000;
      const dir = path.join(root, "direct");
      mkdirSync(dir, { mode: 0o700 });
      const ok = await direct(dir, `${uid}:${process.getgid?.() ?? uid}`);
      expect(ok.status, ok.stderr).toBe(0);
      const st = statSync(path.join(dir, "result.json"));
      expect([st.uid, st.mode & 0o777]).toEqual([uid, 0o600]);

      const trap = path.join(root, "trap");
      const target = path.join(root, "target.txt");
      mkdirSync(trap, { mode: 0o700 });
      writeFileSync(target, "untouched");
      symlinkSync(target, path.join(trap, "result.json"));
      const planted = await direct(trap, `${uid}:${process.getgid?.() ?? uid}`);
      expect(planted.status).not.toBe(0);
      expect(readFileSync(target, "utf8")).toBe("untouched");
    }, 60_000);

    it("a container user that cannot write the directory fails closed with a non-zero exit", async () => {
      const dir = path.join(root, "wrong-uid");
      mkdirSync(dir, { mode: 0o700 });
      const denied = await direct(dir, "4242:4242");
      expect(denied.status).not.toBe(0);
      expect(existsSync(path.join(dir, "result.json"))).toBe(false);
    }, 60_000);
  });
});
