// `argus-task attach` (task-helpers spec 3.9): the real CLI against the real host bridge
// (argus-attach-relay.ts) in a temp directory, with a fake store that keeps the bytes.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  appendFileSync,
  chmodSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, it } from "node:test";
import {
  ATTACH_RECEIPTS_FILE,
  ATTACH_REQUESTS_FILE,
  ATTACH_ROLES,
  ATTACH_SPOOL_DIR,
  ATTACH_STATUS_FILE,
  createAttachRelay,
  parseAttachRequest,
  resetAttachSpool,
} from "./argus-attach-relay.ts";
import { artifactName, EXIT, main } from "./argus-task.mjs";

const scratch = mkdtempSync(join(tmpdir(), "argus-attach-"));
after(() => rmSync(scratch, { recursive: true, force: true }));

const T0 = Date.parse("2026-11-06T10:00:00Z");
const sha = (data) => createHash("sha256").update(data).digest("hex");
const lines = (file) =>
  readFileSync(file, "utf8")
    .split("\n")
    .filter(Boolean)
    .map((l) => JSON.parse(l));

let count = 0;
/**
 * A bridge directory (what the container sees as /workspace/agent), a workspace root, a
 * relay for task t1 attempt 2, and a fake store holding the bytes by artifact id.
 */
async function bridge({
  maxBytes,
  store,
  task = { id: "t1", attempt: 2 },
  active = () => true,
} = {}) {
  const base = join(scratch, `b${count++}`);
  const dir = join(base, "agent");
  const root = join(base, "workspace");
  mkdirSync(dir, { recursive: true });
  mkdirSync(root);
  for (const f of [ATTACH_REQUESTS_FILE, ATTACH_RECEIPTS_FILE]) {
    writeFileSync(join(dir, f), "", { mode: 0o666 });
    chmodSync(join(dir, f), 0o666);
  }
  await resetAttachSpool(dir);
  const bytes = new Map(); // artifact id -> { name, data }
  const calls = [];
  const fake = async (req) => {
    calls.push(req);
    const taken = [...bytes.entries()].find(([, a]) => a.name === req.name);
    if (taken && sha(taken[1].data) === req.sha256)
      return {
        ok: true,
        artifact_id: taken[0],
        name: req.name,
        role: req.role,
        bytes: req.bytes,
        sha256: req.sha256,
        duplicate: true,
        renamed: false,
      };
    const name = taken ? req.name.replace(/(\.[^.]*)?$/, `-${req.sha256.slice(0, 8)}$1`) : req.name;
    const id = `art${bytes.size + 1}`;
    bytes.set(id, { name, data: Buffer.from(req.data) });
    return {
      ok: true,
      artifact_id: id,
      name,
      role: req.role,
      bytes: req.bytes,
      sha256: req.sha256,
      duplicate: false,
      renamed: name !== req.name,
    };
  };
  const relay = createAttachRelay({
    dir,
    task,
    active,
    maxBytes,
    store: store ?? fake,
    now: () => T0,
    warn: () => {},
  });
  await relay.open();
  const env = {
    ARGUS_TASK_LOG_FILE: join(dir, "argus-task.log"),
    ARGUS_TASK_ATTACH_ROOTS: root,
  };
  return { dir, root, relay, bytes, calls, env };
}

/** Run the CLI; `host` simulates the channel's poll on every injected sleep tick. */
async function attach(b, argv, { host = true, env = {}, onSleep } = {}) {
  let t = T0;
  const out = [];
  const err = [];
  const code = await main(
    ["attach", ...argv],
    { ...b.env, ...env },
    {
      now: () => t,
      sleep: async (ms) => {
        t += ms;
        if (host) await b.relay.poll();
        onSleep?.(t);
      },
      stdout: { write: (s) => out.push(s) },
      stderr: { write: (s) => err.push(s) },
    },
  );
  return {
    code,
    out: out.length ? JSON.parse(out.join("")) : null,
    err: err.length ? JSON.parse(err.join("")).error : null,
    waited: (t - T0) / 1000,
  };
}

const file = (b, name, data) => {
  const path = join(b.root, name);
  writeFileSync(path, data);
  return path;
};

describe("argus-task attach: receipts", () => {
  it("prints a receipt only after the host stored the bytes", async () => {
    const b = await bridge();
    const path = file(b, "report.md", "# done\n");
    const r = await attach(b, ["--file", path, "--name", "report.md", "--role", "report"]);
    assert.equal(r.code, EXIT.ok);
    assert.deepEqual(r.out, {
      stored: true,
      task_id: "t1",
      attempt: 2,
      artifact_id: "art1",
      name: "report.md",
      role: "report",
      bytes: 7,
      sha256: sha("# done\n"),
      duplicate: false,
      renamed: false,
      request_id: r.out.request_id,
    });
    assert.equal(b.bytes.get("art1").data.toString(), "# done\n");
    assert.equal(b.calls.length, 1);
  });

  it("returns the existing receipt for the same bytes and name", async () => {
    const b = await bridge();
    const path = file(b, "r.md", "same");
    const first = await attach(b, ["--file", path, "--role", "report"]);
    const second = await attach(b, ["--file", path, "--role", "report"]);
    assert.equal(second.code, EXIT.ok);
    assert.equal(second.out.artifact_id, first.out.artifact_id);
    assert.equal(b.calls.length, 1);
    assert.equal(lines(join(b.dir, ATTACH_REQUESTS_FILE)).length, 1);
  });

  it("stores the same name with other bytes under a hashed name", async () => {
    const b = await bridge();
    const path = file(b, "r.md", "one");
    const first = await attach(b, ["--file", path]);
    writeFileSync(path, "two!");
    const second = await attach(b, ["--file", path]);
    assert.equal(second.code, EXIT.ok);
    assert.notEqual(second.out.artifact_id, first.out.artifact_id);
    assert.equal(second.out.renamed, true);
    assert.equal(second.out.name, `r-${sha("two!").slice(0, 8)}.md`);
    assert.equal(b.bytes.get(first.out.artifact_id).data.toString(), "one");
  });

  it("sanitizes names exactly as artifactName in argus.ts does (same rows as argus.test.ts)", () => {
    for (const [input, want] of [
      ["../../etc/.passwd", "passwd"],
      ["___", "attachment"],
      ["a b.md", "a-b.md"],
    ])
      assert.equal(artifactName(input), want, input);
    assert.equal(artifactName(`${"n".repeat(300)}.md`).length, 200);
    assert.ok(artifactName(`${"n".repeat(300)}.md`).endsWith(".md"));
  });

  it("sanitizes the name with the artifactName rules", async () => {
    const b = await bridge();
    const r = await attach(b, [
      "--file",
      file(b, "x.md", "x"),
      "--name",
      "../My Report (final).md",
    ]);
    assert.equal(r.out.name, "My-Report-final-.md");
  });

  it("exits 10 with no receipt when the host does not answer, and a rerun adopts the request", async () => {
    const b = await bridge();
    const path = file(b, "r.md", "late");
    const r = await attach(b, ["--file", path, "--max", "5"], { host: false });
    assert.equal(r.code, EXIT.pending);
    assert.equal(r.out.stored, false);
    assert.equal(r.out.pending, true);
    assert.equal(r.waited, 5);
    assert.equal(b.calls.length, 0);
    const again = await attach(b, ["--file", path]);
    assert.equal(again.code, EXIT.ok);
    assert.equal(lines(join(b.dir, ATTACH_REQUESTS_FILE)).length, 1);
    assert.equal(b.calls.length, 1);
  });

  it("clamps --max to ARGUS_TASK_WAIT_MAX_S", async () => {
    const b = await bridge();
    const r = await attach(b, ["--file", file(b, "r.md", "x"), "--max", "900"], {
      host: false,
      env: { ARGUS_TASK_WAIT_MAX_S: "3" },
    });
    assert.equal(r.code, EXIT.pending);
    assert.equal(r.waited, 3);
  });

  it("does not trust a receipt whose hash differs from the file", async () => {
    const b = await bridge({
      store: async (req) => ({
        ok: true,
        artifact_id: "a1",
        name: req.name,
        role: null,
        bytes: req.bytes,
        sha256: "0".repeat(64),
        duplicate: false,
        renamed: false,
      }),
    });
    const r = await attach(b, ["--file", file(b, "r.md", "abc")]);
    assert.equal(r.code, EXIT.error);
    assert.equal(r.err.code, "invalid_receipt");
    assert.equal(r.out, null);
  });

  it("fails with exit 12 when the attempt ends before a receipt", async () => {
    const b = await bridge();
    const r = await attach(b, ["--file", file(b, "r.md", "x")], {
      host: false,
      onSleep: () => b.relay.stop(),
    });
    assert.equal(r.code, EXIT.not_ready);
    assert.equal(r.err.code, "attempt_ended");
  });
});

describe("argus-task attach: file safety and usage", () => {
  it("refuses a symlink, a directory, an empty file, a file outside the roots and a too-large file", async () => {
    const b = await bridge({ maxBytes: 8 });
    const real = file(b, "real.md", "x");
    symlinkSync(real, join(b.root, "link.md"));
    mkdirSync(join(b.root, "d"));
    const outside = join(scratch, "outside.md");
    writeFileSync(outside, "x");
    const cases = [
      [join(b.root, "link.md"), "symlink"],
      [join(b.root, "d"), "not_regular"],
      [file(b, "empty.md", ""), "empty_file"],
      [outside, "outside_roots"],
      [file(b, "big.md", "123456789"), "too_large"],
      [join(b.dir, ATTACH_STATUS_FILE), "outside_roots"],
    ];
    for (const [path, code] of cases) {
      const r = await attach(b, ["--file", path]);
      assert.equal(r.code, EXIT.not_ready, path);
      assert.equal(r.err.code, code, path);
    }
    assert.equal(b.calls.length, 0);
  });

  it("refuses the bridge's own files even when they are under a root", async () => {
    const b = await bridge();
    b.env.ARGUS_TASK_ATTACH_ROOTS = b.dir;
    const r = await attach(b, ["--file", join(b.dir, ATTACH_RECEIPTS_FILE)]);
    assert.equal(r.err.code, "bridge_file");
  });

  it("rejects a relative path, a missing --file and an unknown role with exit 2", async () => {
    const b = await bridge();
    for (const argv of [["--file", "rel.md"], [], ["--file", "/x/y", "--role", "summary"]]) {
      const r = await attach(b, argv);
      assert.equal(r.code, EXIT.usage, argv.join(" "));
    }
  });

  it("names the missing capability and never claims success when the channel has no support", async () => {
    const b = await bridge();
    rmSync(join(b.dir, ATTACH_STATUS_FILE));
    const r = await attach(b, ["--file", file(b, "r.md", "x")]);
    assert.equal(r.code, EXIT.error);
    assert.equal(r.err.code, "prerequisite_missing");
    assert.equal(r.err.capability, "artifact-attach");
    assert.match(r.err.message, /do not ask the orchestrator/);
    assert.equal(r.out, null);
  });

  it("is a precise error when the status file is from a protocol this helper does not speak", async () => {
    const b = await bridge();
    writeFileSync(
      join(b.dir, ATTACH_STATUS_FILE),
      JSON.stringify({
        v: 9,
        capability: "artifact-attach",
        task_id: "t1",
        attempt: 2,
        max_bytes: 5,
      }),
    );
    const r = await attach(b, ["--file", file(b, "r.md", "x")]);
    assert.equal(r.err.code, "prerequisite_missing");
  });
});

describe("attach host bridge: ownership and validation", () => {
  const request = (over = {}) => ({
    v: 1,
    type: "attach",
    request_id: "a".repeat(32),
    nonce: "b".repeat(32),
    task_id: "t1",
    attempt: 2,
    name: "r.md",
    role: "report",
    bytes: 3,
    sha256: sha("abc"),
    ...over,
  });
  const send = async (b, rec, spooled = "abc") => {
    if (spooled !== null)
      writeFileSync(join(b.dir, ATTACH_SPOOL_DIR, rec.nonce ?? "b".repeat(32)), spooled);
    appendFileSync(join(b.dir, ATTACH_REQUESTS_FILE), `${JSON.stringify(rec)}\n`);
    await b.relay.poll();
    return lines(join(b.dir, ATTACH_RECEIPTS_FILE)).at(-1);
  };

  it("stores the active attempt's request and rebuilds the receipt from host values", async () => {
    const b = await bridge();
    const r = await send(b, request({ extra: "ignored", artifact_id: "forged" }));
    assert.equal(r.ok, true);
    assert.equal(r.artifact_id, "art1");
    assert.equal(r.extra, undefined);
    assert.deepEqual(Object.keys(r).sort(), [
      "artifact_id",
      "at",
      "attempt",
      "bytes",
      "duplicate",
      "name",
      "nonce",
      "ok",
      "renamed",
      "request_id",
      "role",
      "sha256",
      "task_id",
      "type",
      "v",
    ]);
  });

  it("gives stale, cross-task, late and malformed requests an explicit error receipt and stores nothing", async () => {
    const closed = { on: false };
    const b = await bridge({ active: () => !closed.on });
    const refused = [
      [request({ attempt: 1 }), "stale_attempt"],
      [request({ attempt: 3 }), "stale_attempt"],
      [request({ task_id: "t2" }), "wrong_task"],
      [request({ role: "summary" }), "invalid_request"],
      [request({ sha256: "xyz" }), "invalid_request"],
      [request({ bytes: 0 }), "invalid_request"],
      [request({ v: 2 }), "unsupported_protocol"],
    ];
    for (const [rec, code] of refused) {
      const r = await send(b, rec);
      assert.equal(r.ok, false, code);
      assert.equal(r.error.code, code);
    }
    closed.on = true;
    const late = await send(b, request());
    assert.equal(late.error.code, "no_active_attempt");
    assert.equal(b.calls.length, 0);
  });

  it("checks the spooled bytes against the request and never follows a link", async () => {
    const b = await bridge();
    assert.equal((await send(b, request(), "abd")).error.code, "integrity_mismatch");
    assert.equal(
      (await send(b, request({ nonce: "c".repeat(32) }), null)).error.code,
      "spool_missing",
    );
    const secret = join(scratch, "secret.txt");
    writeFileSync(secret, "abc");
    symlinkSync(secret, join(b.dir, ATTACH_SPOOL_DIR, "d".repeat(32)));
    assert.equal(
      (await send(b, request({ nonce: "d".repeat(32) }), null)).error.code,
      "unsafe_spool",
    );
    assert.equal(b.calls.length, 0);
  });

  it("limits requests per attempt but does not count a retryable failure", async () => {
    let fail = true;
    const b = await bridge({
      store: async (req) =>
        fail
          ? { ok: false, code: "upload_failed", message: "retry", retryable: true }
          : {
              ok: true,
              artifact_id: "a1",
              name: req.name,
              role: null,
              bytes: req.bytes,
              sha256: req.sha256,
              duplicate: false,
              renamed: false,
            },
    });
    const first = await send(b, request());
    assert.equal(first.error.retryable, true);
    fail = false;
    const retry = await send(b, request({ nonce: "e".repeat(32) }), "abc");
    assert.equal(retry.ok, true);
  });

  it("keeps bytes and secrets out of receipts", async () => {
    const b = await bridge();
    await send(b, request(), "abc");
    const text = readFileSync(join(b.dir, ATTACH_RECEIPTS_FILE), "utf8");
    assert.ok(!text.includes("YWJj") && !/"data"/.test(text));
  });

  it("parses requests with the same role list the CLI accepts", () => {
    const task = { id: "t1", attempt: 2 };
    for (const role of ATTACH_ROLES)
      assert.ok("request" in parseAttachRequest(JSON.stringify(request({ role })), task), role);
    assert.equal(parseAttachRequest("not json", task), null);
  });
});

describe("argus-task attach: documented examples", () => {
  it("runs every example in --help against the bridge", async () => {
    let help = "";
    await main(["attach", "--help"], {}, { stdout: { write: (s) => (help += s) } });
    const examples = help.split("\n").filter((l) => l.startsWith("  argus-task attach "));
    assert.ok(examples.length >= 2);
    for (const example of examples) {
      const b = await bridge();
      const argv = example
        .trim()
        .split(/\s+/)
        .slice(2)
        .map((a) =>
          a.startsWith("/workspace/") ? join(b.root, a.slice("/workspace/".length)) : a,
        );
      mkdirSync(join(argv[1], ".."), { recursive: true });
      writeFileSync(argv[1], "example body");
      const r = await attach(b, argv);
      assert.equal(r.code, EXIT.ok, example);
      assert.ok(ATTACH_ROLES.includes(r.out.role), example);
    }
  });

  it("documents the exit codes and says send_file is not a receipt", async () => {
    let help = "";
    await main(["attach", "--help"], {}, { stdout: { write: (s) => (help += s) } });
    for (const needle of [
      "BEFORE you reply",
      "0 stored",
      "10 no receipt",
      "12 refused",
      "send_file is a chat attachment, not a receipt",
    ])
      assert.ok(help.includes(needle), needle);
  });
});
