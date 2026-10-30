import { execFileSync, spawnSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import {
  chmodSync,
  existsSync,
  linkSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  readlinkSync,
  renameSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import http from "node:http";
import type { AddressInfo } from "node:net";
import os from "node:os";
import path from "node:path";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { parseCheckpointRef } from "./argus-checkpoint.js";
import {
  consumedReceipts,
  INPUT_CONSUMED,
  INPUT_MANIFEST,
  INPUT_VERIFIER,
  type InputReceipt,
  type InputsSource,
  inputLimitsFromEnv,
  materializeTaskInputs,
  parseInputRef,
  TASK_INPUTS_DIR,
  taskInputLines,
  taskInputsContainerPath,
  taskInputsPath,
  unsafeInputName,
} from "./argus-inputs.js";

const sha = (b: Uint8Array) => createHash("sha256").update(b).digest("hex");

/** A synthetic unified diff of at least `min` bytes with multi-byte UTF-8 on every hunk. */
function patchFixture(min = 62_300): Buffer {
  const parts = ["diff --git a/notes.txt b/notes.txt\n", "--- a/notes.txt\n", "+++ b/notes.txt\n"];
  let n = 0;
  while (Buffer.byteLength(parts.join("")) < min) {
    parts.push(`@@ -${n + 1},0 +${n + 1},2 @@\n`, `+line ${n} café — naïve ✓ 漢字 🚀\n`, "+\n");
    n += 1;
  }
  return Buffer.from(parts.join(""), "utf8");
}

type Behaviour = {
  /** Close the connection after this many body bytes, on the first N requests. */
  dropAfter?: number;
  drops?: number;
  ignoreRange?: boolean;
  /** Serve flipped bytes on the first N requests (Infinity: always). */
  corrupt?: number;
  extra?: number;
  status?: number;
  badContentRange?: boolean;
};

interface Served {
  bytes: Buffer;
  behaviour: Behaviour;
}

/** A real local Argus stand-in: the inputs list and the content endpoint, honouring Range. */
class FakeArgus {
  server = http.createServer((req, res) => this.handle(req, res));
  artifacts = new Map<string, Served>();
  items: unknown[] | null = [];
  listStatus = 200;
  requests: Array<{ url: string; range: string | null }> = [];
  base = "";

  async start() {
    await new Promise<void>((r) => this.server.listen(0, "127.0.0.1", r));
    this.base = `http://127.0.0.1:${(this.server.address() as AddressInfo).port}`;
  }

  stop() {
    this.server.closeAllConnections();
    return new Promise<void>((r) => this.server.close(() => r()));
  }

  add(taskId: string, name: string, bytes: Buffer, behaviour: Behaviour = {}) {
    const id = randomUUID();
    this.artifacts.set(id, { bytes, behaviour });
    const item = {
      id,
      task_id: taskId,
      name,
      media_type: "text/x-diff",
      size: bytes.length,
      sha256: sha(bytes),
      attached_by: "orchestrator:o1",
      created_at: "2026-10-30T00:00:00Z",
      receipts: [],
    };
    this.items?.push(item);
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
    const range = (req.headers.range as string | undefined) ?? null;
    this.requests.push({ url, range });
    if (url.endsWith("/inputs")) {
      res.writeHead(this.listStatus, { "Content-Type": "application/json" });
      res.end(JSON.stringify(this.listStatus < 400 ? { inputs: this.items } : { detail: "no" }));
      return;
    }
    const m = /^\/api\/artifacts\/([^/]+)\/content$/.exec(url);
    const a = m ? this.artifacts.get(m[1]) : undefined;
    if (!a) {
      res.writeHead(404).end();
      return;
    }
    const b = a.behaviour;
    if (b.status) {
      res.writeHead(b.status).end();
      return;
    }
    let body = a.bytes;
    if (b.corrupt && b.corrupt > 0) {
      b.corrupt -= 1;
      body = Buffer.from(body);
      if (body.length > 0) body[body.length >> 1] ^= 0xff;
    }
    if (b.extra) body = Buffer.concat([body, Buffer.alloc(b.extra, 0x41)]);
    const r = /^bytes=(\d+)-$/.exec(range ?? "");
    let start = 0;
    if (r && !b.ignoreRange) {
      start = Number(r[1]);
      if (start >= a.bytes.length) {
        res.writeHead(416).end();
        return;
      }
      const end = a.bytes.length - 1;
      res.writeHead(206, {
        "Content-Range": b.badContentRange
          ? `bytes 0-${end}/${a.bytes.length}`
          : `bytes ${start}-${end}/${a.bytes.length}`,
        "Content-Length": String(body.length - start),
      });
    } else {
      res.writeHead(200, { "Content-Length": String(body.length) });
    }
    const out = body.subarray(start);
    if (b.dropAfter !== undefined && (b.drops ?? 1) > 0) {
      b.drops = (b.drops ?? 1) - 1;
      res.write(out.subarray(0, b.dropAfter), () => res.socket?.destroy());
      return;
    }
    res.end(out);
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
  fake.requests = [];
  fake.listStatus = 200;
  for (const d of dirs.splice(0)) {
    // 0400 files and 0700 dirs: make removable first.
    execFileSync("chmod", ["-R", "u+rwX", d]);
    rmSync(d, { recursive: true, force: true });
  }
});

function group(): string {
  const d = mkdtempSync(path.join(os.tmpdir(), "argus-inputs-"));
  dirs.push(d);
  return d;
}

function run(
  groupDir: string,
  taskId: string,
  over: Partial<Parameters<typeof materializeTaskInputs>[0]> = {},
) {
  const receipts: InputReceipt[] = [];
  return materializeTaskInputs({
    groupDir,
    taskId,
    attempt: 1,
    source: fake.source(),
    backoffMs: 0,
    onReceipt: async (r) => void receipts.push(r),
    ...over,
  }).then((result) => ({ result, receipts }));
}

/** Every entry below `dir`: kind, mode and, for files, content. Links are not followed. */
function snapshot(dir: string): Record<string, string> {
  const out: Record<string, string> = {};
  const walk = (d: string) => {
    for (const name of readdirSync(d)) {
      const p = path.join(d, name);
      const st = lstatSync(p);
      const rel = path.relative(dir, p);
      if (st.isDirectory()) {
        out[rel] = `dir ${st.mode & 0o777}`;
        walk(p);
      } else if (st.isSymbolicLink()) out[rel] = `link ${readlinkSync(p)}`;
      else out[rel] = `file ${st.mode & 0o777} ${st.nlink} ${readFileSync(p)}`;
    }
  };
  walk(dir);
  return out;
}

const contentRequests = () => fake.requests.filter((r) => r.url.endsWith("/content"));

describe("paths and references", () => {
  it("keys the directory by a canonical task id", () => {
    const id = randomUUID();
    expect(taskInputsPath("/g", id)).toBe(`/g/${TASK_INPUTS_DIR}/${id}`);
    expect(taskInputsContainerPath(id)).toBe(`/workspace/agent/task-inputs/${id}`);
    for (const bad of ["../x", id.toUpperCase(), "t1", `${id}/..`])
      expect(() => taskInputsPath("/g", bad)).toThrow();
  });

  it("parses only the attachment reference form, and checkpoints ignore it", () => {
    const id = randomUUID();
    const h = "a".repeat(64);
    expect(parseInputRef(`attachment:${id}:${h}`)).toEqual({ artifactId: id, sha256: h });
    for (const bad of [
      null,
      "",
      `attachment:${id}:${"A".repeat(64)}`,
      `attachment:${id}`,
      `checkpoint:20261030000000-abcdefabcdef:${h}`,
      `attachment:${id}:${h}:x`,
    ])
      expect(parseInputRef(bad)).toBeNull();
    expect(parseCheckpointRef(`attachment:${id}:${h}`)).toBeNull();
  });

  it("re-checks the artifact name rule on the host", () => {
    for (const ok of ["patch.diff", "a", "A-1_b.txt", "x".repeat(200)])
      expect(unsafeInputName(ok)).toBeNull();
    for (const bad of [
      "../etc/passwd",
      "a/b",
      "a\\b",
      ".hidden",
      "..",
      "-x",
      "_x",
      "x".repeat(201),
      "CON",
      "nul.txt",
      "com1.diff",
      "LPT9",
      "manifest.json",
      "Verify-Inputs.mjs",
      "consumed.json",
      "",
      "naïve.txt",
    ])
      expect(unsafeInputName(bad), bad).not.toBeNull();
  });

  it("reads limits from the environment with the spec defaults", () => {
    expect(inputLimitsFromEnv({})).toEqual({
      maxBytes: 100 * 1024 * 1024,
      maxTotalBytes: 256 * 1024 * 1024,
      maxFiles: 20,
    });
    expect(inputLimitsFromEnv({ ARGUS_ARTIFACT_MAX_BYTES: "5" }).maxBytes).toBe(5);
    expect(
      inputLimitsFromEnv({
        ARGUS_ARTIFACT_MAX_BYTES: "5",
        ARGUS_TASK_INPUT_MAX_BYTES: "7",
        ARGUS_TASK_INPUT_MAX_TOTAL_BYTES: "9",
        ARGUS_TASK_INPUT_MAX_FILES: "2",
      }),
    ).toEqual({ maxBytes: 7, maxTotalBytes: 9, maxFiles: 2 });
    expect(inputLimitsFromEnv({ ARGUS_TASK_INPUT_MAX_FILES: "-1" }).maxFiles).toBe(20);
  });
});

describe("materializeTaskInputs", () => {
  it("materializes a 62,300-byte multi-byte patch byte-identically, private, with a manifest", async () => {
    const g = group();
    const taskId = randomUUID();
    const bytes = patchFixture();
    expect(bytes.length).toBeGreaterThanOrEqual(62_300);
    expect(bytes.toString("utf8")).toContain("漢字");
    const item = fake.add(taskId, "recovered.patch", bytes);
    const { result, receipts } = await run(g, taskId, {
      required: { artifactId: item.id, sha256: item.sha256 },
    });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const dir = taskInputsPath(g, taskId);
    const file = path.join(dir, "recovered.patch");
    expect(readFileSync(file).equals(bytes)).toBe(true);
    expect(sha(readFileSync(file))).toBe(item.sha256);
    expect(statSync(dir).mode & 0o777).toBe(0o700);
    expect(statSync(file).mode & 0o777).toBe(0o400);
    expect(statSync(path.join(g, TASK_INPUTS_DIR)).mode & 0o077).toBe(0);
    expect(statSync(file).uid).toBe(process.getuid?.());
    expect(result.inputs).toEqual([
      expect.objectContaining({
        artifact_id: item.id,
        name: "recovered.patch",
        size: bytes.length,
        sha256: item.sha256,
        state: "verified",
        required: true,
        reused: false,
        containerPath: `/workspace/agent/task-inputs/${taskId}/recovered.patch`,
      }),
    ]);
    const manifest = JSON.parse(readFileSync(path.join(dir, INPUT_MANIFEST), "utf8"));
    expect(manifest).toEqual(result.manifest);
    expect(manifest.inputs[0]).toEqual({
      artifact_id: item.id,
      name: "recovered.patch",
      size: bytes.length,
      sha256: item.sha256,
      state: "verified",
      attempt: 1,
      required: true,
    });
    expect(readFileSync(path.join(dir, INPUT_MANIFEST), "utf8")).not.toContain("café");
    expect(receipts).toEqual([
      { artifact_id: item.id, state: "materialized", sha256: item.sha256, size: bytes.length },
      { artifact_id: item.id, state: "verified", sha256: item.sha256, size: bytes.length },
    ]);
    // Nothing partial is left, and only expected files are in the directory.
    expect(readdirSync(path.join(dir, ".partial"))).toEqual([]);
    expect(readdirSync(dir).sort()).toEqual(
      [".partial", INPUT_MANIFEST, "recovered.patch", INPUT_VERIFIER].sort(),
    );
  });

  it.each([0, 1, 65_535, 65_536, 65_537, 1024 * 1024])(
    "handles %i bytes (empty, single, chunk boundaries)",
    async (size) => {
      const g = group();
      const taskId = randomUUID();
      const bytes = Buffer.alloc(size, 0);
      for (let i = 0; i < size; i++) bytes[i] = (i * 31) & 0xff;
      fake.add(taskId, "f.bin", bytes);
      const { result } = await run(g, taskId);
      expect(result.ok && result.inputs[0].state).toBe("verified");
      expect(readFileSync(path.join(taskInputsPath(g, taskId), "f.bin")).equals(bytes)).toBe(true);
    },
  );

  it("resumes an interrupted connection with Range and a matching Content-Range", async () => {
    const g = group();
    const taskId = randomUUID();
    const bytes = patchFixture();
    const item = fake.add(taskId, "p.diff", bytes, { dropAfter: 20_000, drops: 1 });
    const { result } = await run(g, taskId, {
      required: { artifactId: item.id, sha256: item.sha256 },
    });
    expect(result.ok).toBe(true);
    const reqs = contentRequests();
    expect(reqs).toHaveLength(2);
    expect(reqs[0].range).toBeNull();
    expect(reqs[1].range).toMatch(/^bytes=\d+-$/);
    expect(Number(/\d+/.exec(reqs[1].range ?? "")?.[0])).toBeGreaterThan(0);
    expect(readFileSync(path.join(taskInputsPath(g, taskId), "p.diff")).equals(bytes)).toBe(true);
  });

  it("restarts the file when the server ignores the range", async () => {
    const g = group();
    const taskId = randomUUID();
    const bytes = patchFixture();
    fake.add(taskId, "p.diff", bytes, { dropAfter: 10_000, drops: 1, ignoreRange: true });
    const { result } = await run(g, taskId);
    expect(result.ok && result.inputs[0].state).toBe("verified");
    expect(contentRequests()[1].range).not.toBeNull();
    expect(readFileSync(path.join(taskInputsPath(g, taskId), "p.diff")).equals(bytes)).toBe(true);
  });

  it("restarts when a 206 answer names another range", async () => {
    const g = group();
    const taskId = randomUUID();
    const bytes = patchFixture();
    fake.add(taskId, "p.diff", bytes, { dropAfter: 10_000, drops: 1, badContentRange: true });
    const { result } = await run(g, taskId);
    expect(result.ok && result.inputs[0].state).toBe("verified");
    const reqs = contentRequests();
    expect(reqs).toHaveLength(3);
    expect(reqs[2].range).toBeNull();
  });

  it("retries a hash mismatch once from byte 0", async () => {
    const g = group();
    const taskId = randomUUID();
    const bytes = patchFixture();
    const item = fake.add(taskId, "p.diff", bytes, { corrupt: 1 });
    const { result } = await run(g, taskId, {
      required: { artifactId: item.id, sha256: item.sha256 },
    });
    expect(result.ok).toBe(true);
    expect(contentRequests().map((r) => r.range)).toEqual([null, null]);
  });

  it("is invalid after a second mismatch and never publishes the bad bytes", async () => {
    const g = group();
    const taskId = randomUUID();
    const item = fake.add(taskId, "p.diff", patchFixture(), { corrupt: Infinity });
    const { result, receipts } = await run(g, taskId, {
      required: { artifactId: item.id, sha256: item.sha256 },
    });
    expect(result).toEqual({
      ok: false,
      reason: "env_input_invalid",
      artifactId: item.id,
      error: `input ${item.id}: the downloaded bytes do not match the hash`,
    });
    expect(contentRequests()).toHaveLength(2);
    const dir = taskInputsPath(g, taskId);
    expect(existsSync(path.join(dir, "p.diff"))).toBe(false);
    expect(readdirSync(path.join(dir, ".partial"))).toEqual([]);
    // Receipt reasons are the API's codes (artifacts/inputs.py InputFailure).
    expect(receipts).toEqual([{ artifact_id: item.id, state: "failed", reason: "corrupt" }]);
  });

  it("treats more bytes than the size as corrupt and stops at size + 1", async () => {
    const g = group();
    const taskId = randomUUID();
    const item = fake.add(taskId, "p.diff", Buffer.from("abc"), { extra: 5 });
    const { result } = await run(g, taskId, {
      required: { artifactId: item.id, sha256: item.sha256 },
    });
    expect(!result.ok && result.reason).toBe("env_input_invalid");
    expect(!result.ok && result.error).toContain("longer than its size");
    const dir = taskInputsPath(g, taskId);
    expect(existsSync(path.join(dir, "p.diff"))).toBe(false);
    expect(readdirSync(path.join(dir, ".partial"))).toEqual([]);
  });

  it.each([
    [403, 1],
    [404, 1],
    [500, 3],
    [503, 3],
  ])("a %i from the content endpoint is unavailable (%i tries)", async (status, tries) => {
    const g = group();
    const taskId = randomUUID();
    const item = fake.add(taskId, "p.diff", Buffer.from("x"), { status });
    const { result } = await run(g, taskId, {
      required: { artifactId: item.id, sha256: item.sha256 },
    });
    expect(!result.ok && result.reason).toBe("env_input_unavailable");
    expect(contentRequests()).toHaveLength(tries);
  });

  it("is unavailable when Argus is unreachable", async () => {
    const g = group();
    const taskId = randomUUID();
    const required = { artifactId: randomUUID(), sha256: "b".repeat(64) };
    const dead: InputsSource = {
      list: () => fetch("http://127.0.0.1:1/api/x").then((r) => r.json()),
      content: () => fetch("http://127.0.0.1:1/api/x"),
    };
    const { result } = await run(g, taskId, { source: dead, required });
    expect(!result.ok && result.reason).toBe("env_input_unavailable");
    // Without a required input, an unreadable list is skipped, not a block.
    const optional = await run(g, taskId, { source: dead });
    expect(optional.result).toMatchObject({
      ok: true,
      inputs: [],
      skipped: "Argus is unreachable",
    });
    // Mid-transfer unreachability uses the retry budget, then is unavailable.
    const item = fake.add(taskId, "p.diff", Buffer.from("xyz"));
    const half: InputsSource = { ...fake.source(), content: dead.content };
    const third = await run(g, taskId, {
      source: half,
      required: { artifactId: item.id, sha256: item.sha256 },
    });
    expect(!third.result.ok && third.result.reason).toBe("env_input_unavailable");
  });

  it("skips an old server's 404 listing, and blocks when an input is required", async () => {
    const g = group();
    const taskId = randomUUID();
    fake.listStatus = 404;
    expect((await run(g, taskId)).result).toEqual({
      ok: true,
      inputs: [],
      manifest: null,
      skipped: "the inputs list answered 404",
    });
    expect(existsSync(path.join(g, TASK_INPUTS_DIR))).toBe(false);
    const required = { artifactId: randomUUID(), sha256: "c".repeat(64) };
    const { result } = await run(g, taskId, { required });
    expect(!result.ok && result.reason).toBe("env_input_unavailable");
  });

  it("accepts a bare array listing", async () => {
    const g = group();
    const taskId = randomUUID();
    const item = fake.add(taskId, "a.txt", Buffer.from("hi"));
    const list = fake.items;
    const source: InputsSource = { ...fake.source(), list: async () => list };
    const { result } = await run(g, taskId, { source });
    expect(result.ok && result.inputs[0].artifact_id).toBe(item.id);
  });

  it("checks the required reference against the authoritative list", async () => {
    const g = group();
    const taskId = randomUUID();
    const item = fake.add(taskId, "p.diff", Buffer.from("data"));
    const differs = await run(g, taskId, {
      required: { artifactId: item.id, sha256: "d".repeat(64) },
    });
    expect(differs.result).toMatchObject({ ok: false, reason: "env_input_invalid" });
    const missing = await run(g, taskId, {
      required: { artifactId: randomUUID(), sha256: item.sha256 },
    });
    expect(missing.result).toMatchObject({ ok: false, reason: "env_input_unavailable" });
    expect(contentRequests()).toHaveLength(0);
  });

  it("ignores entries of another task or not shared", async () => {
    const g = group();
    const taskId = randomUUID();
    const other = fake.add(randomUUID(), "o.txt", Buffer.from("other"));
    const unshared = fake.add(taskId, "u.txt", Buffer.from("u"));
    (unshared as { worker_readable?: boolean }).worker_readable = false;
    const { result } = await run(g, taskId);
    expect(result).toEqual({ ok: true, inputs: [], manifest: null });
    const required = await run(g, taskId, {
      required: { artifactId: other.id, sha256: other.sha256 },
    });
    expect(required.result).toMatchObject({ ok: false, reason: "env_input_invalid" });
  });

  it.each(["../escape.txt", "a/b.txt", "..", ".hidden", "CON", "manifest.json"])(
    "rejects the unsafe name %s and never shows it",
    async (name) => {
      const g = group();
      const taskId = randomUUID();
      const bad = fake.add(taskId, name, Buffer.from("x"));
      const good = fake.add(taskId, "ok.txt", Buffer.from("ok"));
      const { result, receipts } = await run(g, taskId);
      expect(result.ok).toBe(true);
      if (!result.ok) return;
      expect(result.inputs.find((i) => i.artifact_id === bad.id)).toMatchObject({
        state: "failed",
        reason: "env_input_invalid",
        name: null,
      });
      expect(result.inputs.find((i) => i.artifact_id === good.id)?.state).toBe("verified");
      expect(receipts).toContainEqual({ artifact_id: bad.id, state: "failed", reason: "invalid" });
      expect(existsSync(path.join(g, "escape.txt"))).toBe(false);
      const prompt = taskInputLines(taskId, result.inputs).join("\n");
      if (name !== "manifest.json") expect(prompt).not.toContain(name);
      expect(prompt).toContain(`input ${bad.id} could not be materialized (env_input_invalid)`);
      const required = await run(g, taskId, {
        required: { artifactId: bad.id, sha256: bad.sha256 },
      });
      expect(required.result).toMatchObject({ ok: false, reason: "env_input_invalid" });
    },
  );

  it("rejects names that are duplicates after case folding", async () => {
    const g = group();
    const taskId = randomUUID();
    const a = fake.add(taskId, "Patch.diff", Buffer.from("a"));
    const b = fake.add(taskId, "patch.DIFF", Buffer.from("b"));
    const { result } = await run(g, taskId);
    expect(result.ok && result.inputs.map((i) => i.state)).toEqual(["failed", "failed"]);
    expect(existsSync(path.join(taskInputsPath(g, taskId), "Patch.diff"))).toBe(false);
    const required = await run(g, taskId, { required: { artifactId: b.id, sha256: b.sha256 } });
    expect(required.result).toMatchObject({ ok: false, reason: "env_input_invalid" });
    expect(a.id).not.toBe(b.id);
  });

  it("enforces the per-input, total and file-count limits", async () => {
    const g = group();
    const taskId = randomUUID();
    const big = fake.add(taskId, "big.bin", Buffer.alloc(11, 1));
    fake.add(taskId, "small.bin", Buffer.alloc(10, 2));
    const limits = { maxBytes: 10, maxTotalBytes: 100, maxFiles: 5 };
    const per = await run(g, taskId, { limits });
    expect(per.result.ok && per.result.inputs.map((i) => [i.state, i.reason])).toEqual([
      ["failed", "env_input_limit"],
      ["verified", undefined],
    ]);
    const blocked = await run(g, taskId, {
      limits,
      required: { artifactId: big.id, sha256: big.sha256 },
    });
    expect(blocked.result).toMatchObject({ ok: false, reason: "env_input_limit" });
    // Exactly at the limit is fine.
    const at = await run(g, taskId, { limits: { maxBytes: 11, maxTotalBytes: 21, maxFiles: 2 } });
    expect(at.result.ok && at.result.inputs.every((i) => i.state === "verified")).toBe(true);
    const total = await run(g, taskId, {
      limits: { maxBytes: 11, maxTotalBytes: 20, maxFiles: 2 },
    });
    expect(total.result.ok && total.result.inputs.map((i) => i.reason)).toEqual([
      "env_input_limit",
      "env_input_limit",
    ]);
    const files = await run(g, taskId, {
      limits: { maxBytes: 11, maxTotalBytes: 21, maxFiles: 1 },
      required: { artifactId: big.id, sha256: big.sha256 },
    });
    expect(files.result).toMatchObject({ ok: false, reason: "env_input_limit" });
    if (!files.result.ok) expect(files.result.error).toContain("ARGUS_TASK_INPUT_MAX_FILES");
  });

  it("refuses a symlinked root or task directory", async () => {
    const outside = group();
    const taskId = randomUUID();
    const item = fake.add(taskId, "p.diff", Buffer.from("payload"));
    const required = { artifactId: item.id, sha256: item.sha256 };

    const g1 = group();
    symlinkSync(outside, path.join(g1, TASK_INPUTS_DIR));
    expect((await run(g1, taskId, { required })).result).toMatchObject({
      ok: false,
      reason: "env_input_invalid",
    });

    const g2 = group();
    mkdirSync(path.join(g2, TASK_INPUTS_DIR), { mode: 0o700 });
    symlinkSync(outside, path.join(g2, TASK_INPUTS_DIR, taskId));
    expect((await run(g2, taskId, { required })).result).toMatchObject({
      ok: false,
      reason: "env_input_invalid",
    });
    expect(readdirSync(outside)).toEqual([]);
    expect(contentRequests()).toHaveLength(0);
  });

  it.each([
    ["a symlink at the final name", (d: string, _p: string, o: string) => symlinkSync(o, d)],
    ["a symlink at the partial name", (_d: string, p: string, o: string) => symlinkSync(o, p)],
    ["a hard link at the final name", (d: string, _p: string, o: string) => linkSync(o, d)],
    ["a hard link at the partial name", (_d: string, p: string, o: string) => linkSync(o, p)],
  ])("recovers from %s without touching its target", async (_w, plant) => {
    const g = group();
    const outside = group();
    const taskId = randomUUID();
    const bytes = Buffer.from("payload");
    const item = fake.add(taskId, "p.diff", bytes);
    const dir = taskInputsPath(g, taskId);
    mkdirSync(path.join(dir, ".partial"), { recursive: true, mode: 0o700 });
    const target = path.join(outside, "target");
    writeFileSync(target, "victim");
    plant(path.join(dir, "p.diff"), path.join(dir, ".partial", `${item.id}.part`), target);
    // Unlinking a hard link only drops its name here: the target keeps its bytes and mode.
    chmodSync(target, 0o644);
    const before = { ...snapshot(outside), target: `file ${0o644} 1 victim` };
    const { result } = await run(g, taskId, {
      required: { artifactId: item.id, sha256: item.sha256 },
    });
    expect(result.ok && result.inputs[0].state).toBe("verified");
    expect(snapshot(outside)).toEqual(before);
    const st = lstatSync(path.join(dir, "p.diff"));
    expect(st.isFile() && st.nlink).toBe(1);
    expect(readFileSync(path.join(dir, "p.diff")).equals(bytes)).toBe(true);
    // A second run is not blocked either: the entry is gone for good.
    expect((await run(g, taskId, { attempt: 2 })).result.ok).toBe(true);
  });

  it("moves a planted directory at the final name aside, contents intact", async () => {
    const g = group();
    const taskId = randomUUID();
    const item = fake.add(taskId, "p.diff", Buffer.from("payload"));
    const dir = taskInputsPath(g, taskId);
    mkdirSync(path.join(dir, "p.diff"), { recursive: true, mode: 0o700 });
    writeFileSync(path.join(dir, "p.diff", "kept"), "kept");
    const { result } = await run(g, taskId, {
      required: { artifactId: item.id, sha256: item.sha256 },
    });
    expect(result.ok && result.inputs[0].state).toBe("verified");
    expect(readFileSync(path.join(dir, "p.diff"), "utf8")).toBe("payload");
    const aside = readdirSync(path.join(dir, ".partial"));
    expect(aside).toHaveLength(1);
    expect(aside[0]).toMatch(/^p\.diff\.displaced\./);
    expect(readFileSync(path.join(dir, ".partial", aside[0], "kept"), "utf8")).toBe("kept");
  });

  it("verifies a partial file that is already complete without a request", async () => {
    const g = group();
    const taskId = randomUUID();
    const bytes = patchFixture();
    const item = fake.add(taskId, "p.diff", bytes);
    const partial = path.join(taskInputsPath(g, taskId), ".partial", `${item.id}.part`);
    mkdirSync(path.dirname(partial), { recursive: true, mode: 0o700 });
    writeFileSync(partial, bytes);
    const { result } = await run(g, taskId, {
      required: { artifactId: item.id, sha256: item.sha256 },
    });
    expect(result.ok).toBe(true);
    expect(contentRequests()).toHaveLength(0);
  });

  it("restarts from byte 0 when a complete partial file holds other bytes", async () => {
    const g = group();
    const taskId = randomUUID();
    const bytes = patchFixture();
    const item = fake.add(taskId, "p.diff", bytes);
    const partial = path.join(taskInputsPath(g, taskId), ".partial", `${item.id}.part`);
    mkdirSync(path.dirname(partial), { recursive: true, mode: 0o700 });
    const wrong = Buffer.from(bytes);
    wrong[0] ^= 1;
    writeFileSync(partial, wrong);
    const { result } = await run(g, taskId, {
      required: { artifactId: item.id, sha256: item.sha256 },
    });
    expect(result.ok).toBe(true);
    expect(contentRequests().map((r) => r.range)).toEqual([null]);
    expect(readFileSync(path.join(taskInputsPath(g, taskId), "p.diff")).equals(bytes)).toBe(true);
  });

  it("sends only the API's receipt reason codes", async () => {
    const g = group();
    const taskId = randomUUID();
    const gone = fake.add(taskId, "gone.bin", Buffer.from("x"), { status: 404 });
    const big = fake.add(taskId, "big.bin", Buffer.alloc(11, 1));
    const bad = fake.add(taskId, "bad.bin", Buffer.from("abc"), { corrupt: Infinity });
    const odd = fake.add(taskId, ".odd", Buffer.from("y"));
    const { receipts } = await run(g, taskId, {
      limits: { maxBytes: 10, maxTotalBytes: 100, maxFiles: 5 },
    });
    const reasons = Object.fromEntries(
      receipts.filter((r) => r.state === "failed").map((r) => [r.artifact_id, r.reason]),
    );
    expect(reasons).toEqual({
      [gone.id]: "unavailable",
      [big.id]: "limit",
      [bad.id]: "corrupt",
      [odd.id]: "invalid",
    });
  });

  it.each([
    ["the task inputs root", (g: string, _t: string) => path.join(g, TASK_INPUTS_DIR)],
    ["the task directory", (g: string, t: string) => taskInputsPath(g, t)],
  ])("never writes outside when %s is swapped for a link after the checks", async (_w, swapped) => {
    const g = group();
    const outside = group();
    const taskId = randomUUID();
    const item = fake.add(taskId, "p.diff", patchFixture());
    // What the swapped link leads to: a look-alike tree outside the group dir, with victims
    // under the names the channel writes, unlinks or replaces.
    const root = path.join(outside, TASK_INPUTS_DIR);
    const lookAlike = path.join(root, taskId);
    mkdirSync(path.join(lookAlike, ".partial"), { recursive: true, mode: 0o700 });
    chmodSync(root, 0o700);
    for (const name of ["p.diff", INPUT_CONSUMED, `.${INPUT_MANIFEST}.${process.pid}.tmp`])
      writeFileSync(path.join(lookAlike, name), "victim");
    writeFileSync(path.join(lookAlike, ".partial", `${item.id}.part`), "victim");
    const before = snapshot(outside);
    const target = swapped(g, taskId);
    const lookAlikeOf = target === taskInputsPath(g, taskId) ? lookAlike : root;
    const { result } = await run(g, taskId, {
      required: { artifactId: item.id, sha256: item.sha256 },
      afterDirsChecked: async () => {
        renameSync(target, `${target}.moved`);
        symlinkSync(lookAlikeOf, target);
      },
    });
    expect(snapshot(outside)).toEqual(before);
    // The path no longer names the directory the channel holds: the attempt is refused.
    expect(result).toMatchObject({ ok: false, reason: "env_input_invalid", artifactId: item.id });
  });

  it("refuses a task directory that is not private", async () => {
    const g = group();
    const taskId = randomUUID();
    const item = fake.add(taskId, "p.diff", Buffer.from("payload"));
    const d = taskInputsPath(g, taskId);
    mkdirSync(d, { recursive: true, mode: 0o700 });
    chmodSync(d, 0o755);
    const { result } = await run(g, taskId, {
      required: { artifactId: item.id, sha256: item.sha256 },
    });
    expect(result).toMatchObject({ ok: false, reason: "env_input_invalid" });
    if (!result.ok) expect(result.error).not.toContain(g);
  });

  it("reuses a verified file without a download and replaces a tampered one", async () => {
    const g = group();
    const taskId = randomUUID();
    const bytes = patchFixture();
    const item = fake.add(taskId, "p.diff", bytes);
    await run(g, taskId);
    expect(contentRequests()).toHaveLength(1);
    const again = await run(g, taskId, { attempt: 2 });
    expect(contentRequests()).toHaveLength(1);
    expect(again.result.ok && again.result.inputs[0]).toMatchObject({ reused: true, attempt: 2 });
    expect(again.receipts).toEqual([
      { artifact_id: item.id, state: "verified", sha256: item.sha256, size: bytes.length },
    ]);
    const file = path.join(taskInputsPath(g, taskId), "p.diff");
    chmodSync(file, 0o600);
    writeFileSync(file, "tampered");
    const third = await run(g, taskId, { attempt: 3 });
    expect(contentRequests()).toHaveLength(2);
    expect(third.result.ok && third.result.inputs[0].reused).toBe(false);
    expect(readFileSync(file).equals(bytes)).toBe(true);
    expect(statSync(file).mode & 0o777).toBe(0o400);
  });

  it("removes a file an earlier run published once it is no longer listed", async () => {
    const g = group();
    const taskId = randomUUID();
    fake.add(taskId, "old.txt", Buffer.from("old"));
    fake.add(taskId, "keep.txt", Buffer.from("keep"));
    await run(g, taskId);
    fake.items = fake.items?.filter((i) => (i as { name: string }).name === "keep.txt") ?? [];
    await run(g, taskId, { attempt: 2 });
    const dir = taskInputsPath(g, taskId);
    expect(existsSync(path.join(dir, "old.txt"))).toBe(false);
    expect(existsSync(path.join(dir, "keep.txt"))).toBe(true);
  });

  it("never reads a partial file of another task", async () => {
    const g = group();
    const taskId = randomUUID();
    const other = randomUUID();
    const item = fake.add(taskId, "p.diff", Buffer.from("payload"));
    const od = taskInputsPath(g, other);
    mkdirSync(path.join(od, ".partial"), { recursive: true, mode: 0o700 });
    writeFileSync(path.join(od, ".partial", `${item.id}.part`), "pay");
    const { result } = await run(g, taskId);
    expect(result.ok).toBe(true);
    expect(contentRequests()[0].range).toBeNull();
  });

  it("reports nothing for a task without shared inputs", async () => {
    const g = group();
    const { result, receipts } = await run(g, randomUUID());
    expect(result).toEqual({ ok: true, inputs: [], manifest: null });
    expect(receipts).toEqual([]);
    expect(existsSync(path.join(g, TASK_INPUTS_DIR))).toBe(false);
    expect(taskInputLines(randomUUID(), [])).toEqual([]);
  });

  it("keeps content, paths outside the task dir and tokens out of errors and receipts", async () => {
    const g = group();
    const taskId = randomUUID();
    const secret = Buffer.from("SECRET-CONTENT-argus_agent_tok");
    const item = fake.add(taskId, "p.diff", secret, { corrupt: Infinity });
    const { result, receipts } = await run(g, taskId, {
      required: { artifactId: item.id, sha256: item.sha256 },
    });
    const text = JSON.stringify({ result, receipts });
    expect(text).not.toContain("SECRET");
    expect(text).not.toContain(g);
    expect(text).not.toContain(fake.base);
  });
});

describe("the prompt block", () => {
  it("holds trusted metadata and rules, never content", async () => {
    const g = group();
    const taskId = randomUUID();
    const bytes = patchFixture();
    const item = fake.add(taskId, "recovered.patch", bytes);
    const failed = fake.add(taskId, "gone.txt", Buffer.from("x"), { status: 404 });
    const { result } = await run(g, taskId, {
      required: { artifactId: item.id, sha256: item.sha256 },
    });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const lines = taskInputLines(taskId, result.inputs);
    const text = lines.join("\n");
    expect(text).toContain(
      `- /workspace/agent/task-inputs/${taskId}/recovered.patch · ${bytes.length} bytes · sha256 ${item.sha256} · required`,
    );
    expect(text).toContain(`node /workspace/agent/task-inputs/${taskId}/verify-inputs.mjs`);
    expect(text).toContain("Read these files in place");
    expect(text).toContain("Do not re-download");
    expect(text).toContain("git apply --check");
    expect(text).toContain("ASSIST");
    expect(text).toContain(`input ${failed.id} could not be materialized (env_input_unavailable)`);
    expect(text).not.toContain("café");
    expect(text).not.toContain("diff --git");
    expect(text).not.toContain(fake.base);
    expect(text).not.toContain(g);
  });
});

describe("the verifier and the consumed relay", () => {
  async function materialized() {
    const g = group();
    const taskId = randomUUID();
    const bytes = patchFixture();
    const item = fake.add(taskId, "recovered.patch", bytes);
    const { result } = await run(g, taskId, { attempt: 2 });
    expect(result.ok).toBe(true);
    return { g, taskId, item, bytes, dir: taskInputsPath(g, taskId) };
  }

  it("is plain node: node:fs and node:crypto only", async () => {
    const { dir } = await materialized();
    const src = readFileSync(path.join(dir, INPUT_VERIFIER), "utf8");
    const imports = [...src.matchAll(/from "([^"]+)"/g)].map((m) => m[1]);
    expect(imports.sort()).toEqual(["node:crypto", "node:fs"]);
    expect(lstatSync(path.join(dir, INPUT_VERIFIER)).mode & 0o777).toBe(0o400);
  });

  it("passes, writes consumed.json, and the relay turns it into consumed receipts", async () => {
    const { dir, item, bytes } = await materialized();
    expect(await consumedReceipts(dir)).toEqual([]);
    const out = spawnSync(process.execPath, [path.join(dir, INPUT_VERIFIER)], {
      cwd: os.tmpdir(),
      encoding: "utf8",
    });
    expect(out.status).toBe(0);
    expect(out.stdout.trim()).toBe(`recovered.patch ${bytes.length} ${item.sha256} ok`);
    const consumed = JSON.parse(readFileSync(path.join(dir, INPUT_CONSUMED), "utf8"));
    expect(consumed.attempt).toBe(2);
    expect(await consumedReceipts(dir)).toEqual([
      { artifact_id: item.id, state: "consumed", sha256: item.sha256, size: bytes.length },
    ]);
  });

  it("fails on a corrupted file and writes no consumed.json", async () => {
    const { dir } = await materialized();
    const file = path.join(dir, "recovered.patch");
    chmodSync(file, 0o600);
    const b = readFileSync(file);
    b[100] ^= 1;
    writeFileSync(file, b);
    const out = spawnSync(process.execPath, [path.join(dir, INPUT_VERIFIER)], { encoding: "utf8" });
    expect(out.status).not.toBe(0);
    expect(out.stdout).toMatch(/recovered\.patch \d+ [0-9a-f]{64} FAIL/);
    expect(existsSync(path.join(dir, INPUT_CONSUMED))).toBe(false);
    expect(await consumedReceipts(dir)).toEqual([]);
  });

  it("fails on a symlink in place of an input", async () => {
    const { dir } = await materialized();
    const file = path.join(dir, "recovered.patch");
    const copy = path.join(group(), "copy");
    writeFileSync(copy, readFileSync(file));
    rmSync(file);
    symlinkSync(copy, file);
    const out = spawnSync(process.execPath, [path.join(dir, INPUT_VERIFIER)], { encoding: "utf8" });
    expect(out.status).not.toBe(0);
  });

  it("relays only entries the manifest of the same attempt verified", async () => {
    const { dir, item, taskId, bytes } = await materialized();
    const forge = (body: unknown) => {
      rmSync(path.join(dir, INPUT_CONSUMED), { force: true });
      writeFileSync(path.join(dir, INPUT_CONSUMED), JSON.stringify(body));
    };
    const entry = { artifact_id: item.id, size: bytes.length, sha256: item.sha256 };
    forge({ task_id: taskId, attempt: 1, inputs: [entry] });
    expect(await consumedReceipts(dir)).toEqual([]);
    forge({ task_id: randomUUID(), attempt: 2, inputs: [entry] });
    expect(await consumedReceipts(dir)).toEqual([]);
    forge({ task_id: taskId, attempt: 2, inputs: [{ ...entry, sha256: "e".repeat(64) }] });
    expect(await consumedReceipts(dir)).toEqual([]);
    forge({ task_id: taskId, attempt: 2, inputs: [{ ...entry, artifact_id: randomUUID() }] });
    expect(await consumedReceipts(dir)).toEqual([]);
    forge({ task_id: taskId, attempt: 2, inputs: [entry, entry] });
    expect(await consumedReceipts(dir)).toHaveLength(1);
    forge("not json at all {");
    expect(await consumedReceipts(dir)).toEqual([]);
  });

  it("ignores a consumed.json or manifest that is oversized, a FIFO or a link", async () => {
    const { dir, item, bytes, taskId } = await materialized();
    const entry = { artifact_id: item.id, size: bytes.length, sha256: item.sha256 };
    const good = JSON.stringify({ task_id: taskId, attempt: 2, inputs: [entry] });
    const file = path.join(dir, INPUT_CONSUMED);
    writeFileSync(file, good + " ".repeat(1024 * 1024));
    expect(await consumedReceipts(dir)).toEqual([]);
    rmSync(file);
    execFileSync("mkfifo", [file]);
    expect(await consumedReceipts(dir)).toEqual([]);
    rmSync(file);
    const elsewhere = path.join(group(), "consumed.json");
    writeFileSync(elsewhere, good);
    symlinkSync(elsewhere, file);
    expect(await consumedReceipts(dir)).toEqual([]);
    rmSync(file);
    writeFileSync(file, good);
    expect(await consumedReceipts(dir)).toHaveLength(1);
    // A manifest that is not a plain file of ours leaves nothing to relay.
    const manifest = path.join(dir, INPUT_MANIFEST);
    rmSync(manifest);
    execFileSync("mkfifo", [manifest]);
    expect(await consumedReceipts(dir)).toEqual([]);
  });

  it("relays nothing when the task directory path is a link", async () => {
    const { dir, item, bytes, taskId } = await materialized();
    const entry = { artifact_id: item.id, size: bytes.length, sha256: item.sha256 };
    writeFileSync(
      path.join(dir, INPUT_CONSUMED),
      JSON.stringify({ task_id: taskId, attempt: 2, inputs: [entry] }),
    );
    renameSync(dir, `${dir}.moved`);
    symlinkSync(`${dir}.moved`, dir);
    expect(await consumedReceipts(dir)).toEqual([]);
  });

  it("drops a stale consumed.json when inputs are materialized again", async () => {
    const { g, taskId, dir } = await materialized();
    spawnSync(process.execPath, [path.join(dir, INPUT_VERIFIER)]);
    expect(existsSync(path.join(dir, INPUT_CONSUMED))).toBe(true);
    await run(g, taskId, { attempt: 3 });
    expect(existsSync(path.join(dir, INPUT_CONSUMED))).toBe(false);
  });
});
