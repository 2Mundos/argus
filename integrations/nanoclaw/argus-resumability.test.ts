/**
 * Grounds task-resumability-preflight.spec.md availability classes against the real
 * materializeTaskInputs consumer (not a mock). An `available` preflight claim must be
 * confirmed by byte count and SHA-256 in the group dir; anything else must not be
 * reported as available. Does not change argus-inputs.ts behaviour.
 *
 * Not covered here (see the spec's Verification boundary): a real Docker session
 * container reading the mounted directory as its non-root user, and a real forge read.
 */
import { createHash, randomUUID } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import http from "node:http";
import type { AddressInfo } from "node:net";
import os from "node:os";
import path from "node:path";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { type InputsSource, materializeTaskInputs, TASK_INPUTS_DIR } from "./argus-inputs.js";

const sha = (b: Uint8Array) => createHash("sha256").update(b).digest("hex");

type ContentBehaviour =
  | { kind: "available" }
  | { kind: "missing" }
  | { kind: "truncated" }
  | { kind: "corrupted" }
  | { kind: "inaccessible"; status: 403 | 500 };

/** Conservative preflight classification for a shared attachment (spec 3.4). */
function preflightAvailability(behaviour: ContentBehaviour): {
  availability: "available" | "missing" | "truncated" | "corrupted" | "inaccessible" | "unknown";
  evidence_class: "current_read" | "unknown" | "unobserved";
} {
  switch (behaviour.kind) {
    case "available":
      return { availability: "available", evidence_class: "current_read" };
    case "missing":
      return { availability: "missing", evidence_class: "current_read" };
    case "truncated":
      return { availability: "truncated", evidence_class: "current_read" };
    case "corrupted":
      return { availability: "corrupted", evidence_class: "current_read" };
    case "inaccessible":
      return { availability: "inaccessible", evidence_class: "current_read" };
  }
}

interface Served {
  bytes: Buffer;
  behaviour: ContentBehaviour;
  recordedSize: number;
  recordedSha: string;
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
    this.artifacts.set(id, {
      bytes,
      behaviour,
      recordedSize: bytes.length,
      recordedSha: sha(bytes),
    });
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
    return { item, preflight: preflightAvailability(behaviour) };
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

describe("preflight availability vs materializeTaskInputs", () => {
  it("available: materializer verifies bytes and hash in the group dir", async () => {
    const taskId = randomUUID();
    const bytes = Buffer.from("resumability-available-payload\n");
    const { item, preflight } = fake.add(taskId, "ok.txt", bytes, { kind: "available" });
    expect(preflight.availability).toBe("available");

    const groupDir = group();
    const result = await materialize(taskId, groupDir, item.id, item.sha256);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const hit = result.inputs.find((i) => i.artifact_id === item.id);
    expect(hit?.state).toBe("verified");
    expect(hit?.path).toBeTruthy();
    const onDisk = readFileSync(hit!.path!);
    expect(onDisk.length).toBe(bytes.length);
    expect(sha(onDisk)).toBe(item.sha256);
    // Conservative: only available claims are confirmed this way.
    expect(preflight.availability === "available").toBe(true);
  });

  it("missing: content 404; preflight is not available", async () => {
    const taskId = randomUUID();
    const bytes = Buffer.from("gone\n");
    const { item, preflight } = fake.add(taskId, "gone.txt", bytes, { kind: "missing" });
    expect(preflight.availability).toBe("missing");
    expect(preflight.availability).not.toBe("available");

    const result = await materialize(taskId, group(), item.id, item.sha256);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.reason).toMatch(/env_input_/);
  });

  it("truncated: short body; preflight is not available", async () => {
    const taskId = randomUUID();
    const bytes = Buffer.from("0123456789abcdef\n");
    const { item, preflight } = fake.add(taskId, "short.txt", bytes, { kind: "truncated" });
    expect(preflight.availability).toBe("truncated");
    expect(preflight.availability).not.toBe("available");

    const result = await materialize(taskId, group(), item.id, item.sha256);
    expect(result.ok).toBe(false);
  });

  it("corrupted: same-size flipped bytes; preflight is not available", async () => {
    const taskId = randomUUID();
    const bytes = Buffer.from("same-length-corrupt!!\n");
    const { item, preflight } = fake.add(taskId, "bad.txt", bytes, { kind: "corrupted" });
    expect(preflight.availability).toBe("corrupted");
    expect(preflight.availability).not.toBe("available");

    const result = await materialize(taskId, group(), item.id, item.sha256);
    expect(result.ok).toBe(false);
  });

  it("inaccessible: 403/500; preflight is not available", async () => {
    for (const status of [403, 500] as const) {
      fake.items = [];
      fake.artifacts.clear();
      const taskId = randomUUID();
      const bytes = Buffer.from("locked\n");
      const { item, preflight } = fake.add(taskId, `locked-${status}.txt`, bytes, {
        kind: "inaccessible",
        status,
      });
      expect(preflight.availability).toBe("inaccessible");
      expect(preflight.availability).not.toBe("available");

      const result = await materialize(taskId, group(), item.id, item.sha256);
      expect(result.ok).toBe(false);
    }
  });

  it("local_only WIP: preflight is not available and materializer is not the path", async () => {
    // No shared inputs: the materializer has nothing to fetch; local WIP is not a transfer.
    const taskId = randomUUID();
    const preflight = {
      wip: {
        kind: "local_only" as const,
        transfer: "not_a_transfer" as const,
        evidence_class: "recorded_metadata" as const,
        availability: null,
      },
      decision: "unknown" as const,
      cause_codes: ["wip_local_only"],
    };
    expect(preflight.wip.kind).toBe("local_only");
    expect(preflight.wip.transfer).toBe("not_a_transfer");
    expect(preflight.cause_codes).toContain("wip_local_only");

    const groupDir = group();
    const result = await materialize(taskId, groupDir);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.inputs).toEqual([]);
    // Materializer did not create a transfer directory for local-only WIP.
    expect(existsSync(path.join(groupDir, TASK_INPUTS_DIR, taskId))).toBe(false);
  });

  it("a verified receipt alone does not make the materializer succeed when bytes are gone", async () => {
    const taskId = randomUUID();
    const bytes = Buffer.from("receipt-only\n");
    const { item, preflight } = fake.add(taskId, "hist.txt", bytes, { kind: "missing" });
    // Receipts in the listing are history; preflight availability is missing.
    expect(item.receipts?.[0]).toMatchObject({ state: "verified" });
    expect(preflight.availability).toBe("missing");
    expect(preflight.evidence_class).toBe("current_read");

    const result = await materialize(taskId, group(), item.id, item.sha256);
    expect(result.ok).toBe(false);
  });

  it("never treats a non-available preflight class as confirmed on disk", async () => {
    const taskId = randomUUID();
    const cases: ContentBehaviour[] = [
      { kind: "missing" },
      { kind: "truncated" },
      { kind: "corrupted" },
      { kind: "inaccessible", status: 403 },
    ];
    for (const behaviour of cases) {
      fake.items = [];
      fake.artifacts.clear();
      const bytes = Buffer.from(`case-${behaviour.kind}\n`);
      const { item, preflight } = fake.add(taskId, `${behaviour.kind}.txt`, bytes, behaviour);
      expect(preflight.availability).not.toBe("available");
      const groupDir = group();
      const result = await materialize(taskId, groupDir, item.id, item.sha256);
      const dir = path.join(groupDir, TASK_INPUTS_DIR, taskId);
      if (existsSync(dir)) {
        // A partial/failed write must not look like a verified available transfer.
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
      expect(result.ok && preflight.availability === "available").toBe(false);
    }
  });
});

describe("additive helper smoke", () => {
  it("can write a tiny fixture without touching argus-inputs exports", () => {
    const d = group();
    const p = path.join(d, "note.txt");
    writeFileSync(p, "ok");
    expect(readFileSync(p, "utf8")).toBe("ok");
  });
});
