/**
 * Grounds task-resumability-preflight.spec.md availability classes against the real
 * materializeTaskInputs consumer (not a mock). Expected preflight classes come from
 * integrations/nanoclaw/fixtures/resumability/availability.json, produced by
 * apps/api/tests/test_resumability_preflight.py via real classify_artifact.
 *
 * An `available` preflight claim must be confirmed by byte count and SHA-256 in the
 * group dir; anything else must not be reported as available. Does not change
 * argus-inputs.ts behaviour.
 *
 * Not covered here (see the spec's Verification boundary): a real Docker session
 * container reading the mounted directory as its non-root user, and a real forge read.
 * WIP classification is owned by the Python preflight tests.
 */
import { createHash, randomUUID } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import http from "node:http";
import type { AddressInfo } from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { type InputsSource, materializeTaskInputs, TASK_INPUTS_DIR } from "./argus-inputs.js";

const sha = (b: Uint8Array) => createHash("sha256").update(b).digest("hex");

const FIXTURE = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  "fixtures/resumability/availability.json",
);

interface FixtureCase {
  case: string;
  availability: "available" | "missing" | "truncated" | "corrupted" | "inaccessible" | "unknown";
  evidence_class: string;
  cause: string | null;
  recorded_size: number;
  recorded_sha256: string;
  observed_size: number | null;
  observed_sha256: string | null;
  error: string | null;
}

interface FixtureFile {
  cases: FixtureCase[];
}

const fixture = JSON.parse(readFileSync(FIXTURE, "utf8")) as FixtureFile;

/** Map a classify_artifact case to how the local HTTP stand-in serves content. */
type ContentBehaviour =
  | { kind: "available" }
  | { kind: "missing" }
  | { kind: "truncated" }
  | { kind: "corrupted" }
  | { kind: "inaccessible"; status: 403 | 500 };

function behaviourFor(c: FixtureCase): ContentBehaviour {
  switch (c.availability) {
    case "available":
      return { kind: "available" };
    case "missing":
      return { kind: "missing" };
    case "truncated":
      return { kind: "truncated" };
    case "corrupted":
      return { kind: "corrupted" };
    case "inaccessible":
    case "unknown":
      return { kind: "inaccessible", status: 403 };
  }
}

interface Served {
  bytes: Buffer;
  behaviour: ContentBehaviour;
}

/** Local Argus stand-in: inputs list + content, matching preflight cases. */
class FakeArgus {
  server = http.createServer((req, res) => this.handle(req, res));
  artifacts = new Map<string, Served>();
  items: unknown[] = [];
  base = "";

  async start() {
    await new Promise<void>((r) => this.server.listen(0, "127.0.0.1", r));
    this.base = `http://127.0.0.1:${(this.server.address() as AddressInfo).port}`;
  }

  stop() {
    this.server.closeAllConnections();
    return new Promise<void>((r) => this.server.close(() => r()));
  }

  add(taskId: string, name: string, bytes: Buffer, behaviour: ContentBehaviour) {
    const id = randomUUID();
    this.artifacts.set(id, { bytes, behaviour });
    const item = {
      id,
      task_id: taskId,
      name,
      media_type: "text/plain",
      size: bytes.length,
      sha256: sha(bytes),
      attached_by: "orchestrator:o1",
      created_at: "2026-10-31T00:00:00Z",
      receipts: [
        { attempt: 1, state: "verified", at: "2026-10-31T00:00:00Z", reporter: "agent:a" },
      ],
    };
    this.items.push(item);
    return item;
  }

  source(): InputsSource {
    return {
      list: async (taskId) => {
        const res = await fetch(`${this.base}/api/tasks/${taskId}/inputs`);
        if (res.status >= 400) throw Object.assign(new Error("http"), { status: res.status });
        return res.json();
      },
      content: (id, range, signal) =>
        fetch(`${this.base}/api/artifacts/${id}/content`, {
          headers: range ? { Range: range } : {},
          signal,
        }),
    };
  }

  private handle(req: http.IncomingMessage, res: http.ServerResponse) {
    const url = req.url ?? "";
    if (url.endsWith("/inputs")) {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ inputs: this.items }));
      return;
    }
    const m = /^\/api\/artifacts\/([^/]+)\/content$/.exec(url);
    const a = m ? this.artifacts.get(m[1]) : undefined;
    if (!a) {
      res.writeHead(404).end();
      return;
    }
    const b = a.behaviour;
    if (b.kind === "missing") {
      res.writeHead(404).end();
      return;
    }
    if (b.kind === "inaccessible") {
      res.writeHead(b.status).end();
      return;
    }
    let body = a.bytes;
    if (b.kind === "truncated") body = body.subarray(0, Math.max(0, body.length - 5));
    if (b.kind === "corrupted") {
      body = Buffer.from(body);
      if (body.length > 0) body[0] ^= 0xff;
    }
    res.writeHead(200, { "Content-Length": String(body.length) });
    res.end(body);
  }
}

let fake: FakeArgus;
const dirs: string[] = [];

beforeAll(async () => {
  fake = new FakeArgus();
  await fake.start();
});
afterAll(async () => {
  await fake.stop();
});
afterEach(() => {
  fake.items = [];
  fake.artifacts.clear();
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
});

function group(): string {
  const d = mkdtempSync(path.join(os.tmpdir(), "argus-resumability-"));
  dirs.push(d);
  return d;
}

async function materialize(taskId: string, groupDir: string, artifactId?: string, hash?: string) {
  return materializeTaskInputs({
    groupDir,
    taskId,
    attempt: 2,
    source: fake.source(),
    backoffMs: 0,
    required: artifactId && hash ? { artifactId, sha256: hash } : null,
  });
}

describe("preflight availability fixture vs materializeTaskInputs", () => {
  it("fixture is present and sorted", () => {
    expect(fixture.cases.length).toBeGreaterThan(0);
    const names = fixture.cases.map((c) => c.case);
    expect(names).toEqual([...names].sort());
  });

  for (const c of fixture.cases) {
    it(`${c.case}: materializer confirms bytes/hash only when fixture says available`, async () => {
      const taskId = randomUUID();
      const bytes = Buffer.from("0123456789abcdef");
      expect(bytes.length).toBe(c.recorded_size);
      expect(sha(bytes)).toBe(c.recorded_sha256);

      const item = fake.add(taskId, `${c.case}.txt`, bytes, behaviourFor(c));
      const groupDir = group();
      const result = await materialize(taskId, groupDir, item.id, item.sha256);

      if (c.availability === "available") {
        expect(result.ok).toBe(true);
        if (!result.ok) return;
        const hit = result.inputs.find((i) => i.artifact_id === item.id);
        expect(hit?.state).toBe("verified");
        expect(hit?.path).toBeTruthy();
        const onDisk = readFileSync(hit!.path!);
        expect(onDisk.length).toBe(bytes.length);
        expect(sha(onDisk)).toBe(item.sha256);
      } else {
        expect(c.availability).not.toBe("available");
        expect(result.ok).toBe(false);
      }
    });
  }

  it("a verified receipt alone does not make the materializer succeed when bytes are gone", async () => {
    const missing = fixture.cases.find((c) => c.case === "missing");
    expect(missing?.availability).toBe("missing");
    expect(missing?.evidence_class).toBe("current_read");

    const taskId = randomUUID();
    const bytes = Buffer.from("0123456789abcdef");
    const item = fake.add(taskId, "hist.txt", bytes, { kind: "missing" });
    expect(item.receipts?.[0]).toMatchObject({ state: "verified" });

    const result = await materialize(taskId, group(), item.id, item.sha256);
    expect(result.ok).toBe(false);
  });

  it("never treats a non-available fixture class as confirmed on disk", async () => {
    for (const c of fixture.cases.filter((x) => x.availability !== "available")) {
      fake.items = [];
      fake.artifacts.clear();
      const taskId = randomUUID();
      const bytes = Buffer.from("0123456789abcdef");
      const item = fake.add(taskId, `${c.case}.txt`, bytes, behaviourFor(c));
      const groupDir = group();
      const result = await materialize(taskId, groupDir, item.id, item.sha256);
      const dir = path.join(groupDir, TASK_INPUTS_DIR, taskId);
      if (existsSync(dir)) {
        const marker = path.join(dir, "manifest.json");
        if (existsSync(marker)) {
          const manifest = JSON.parse(readFileSync(marker, "utf8")) as {
            inputs: Array<{ state: string; sha256: string }>;
          };
          for (const entry of manifest.inputs) {
            if (entry.state === "verified") {
              expect(entry.sha256).not.toBe(item.sha256);
            }
          }
        }
      }
      expect(result.ok).toBe(false);
    }
  });
});
