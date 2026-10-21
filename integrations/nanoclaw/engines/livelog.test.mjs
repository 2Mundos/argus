import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  claudeEventLines,
  createBatcher,
  createFileRelay,
  createStreamParser,
  envSecrets,
  redactText,
  startLiveLog,
} from "./livelog.mjs";

const ev = (o) => `${JSON.stringify(o)}\n`;
const assistant = (...content) => ({ type: "assistant", message: { content } });

test("claude stream events become short readable lines", () => {
  assert.deepEqual(claudeEventLines({ type: "system", subtype: "init", model: "m-1" }), [
    "claude started (m-1)",
  ]);
  assert.deepEqual(
    claudeEventLines(
      assistant(
        { type: "text", text: "Looking at it.\n\nThen fixing." },
        { type: "tool_use", name: "Bash", input: { command: "ls  -la\n/tmp", description: "x" } },
        { type: "tool_use", name: "Weird", input: { a: 1 } },
        { type: "tool_use", name: "Noargs", input: {} },
      ),
    ),
    ["Looking at it.", "Then fixing.", "▸ Bash ls -la /tmp", '▸ Weird {"a":1}', "▸ Noargs"],
  );
  const long = "x".repeat(500);
  const [res] = claudeEventLines({
    type: "user",
    message: { content: [{ type: "tool_result", content: [{ type: "text", text: long }] }] },
  });
  assert.ok(res.startsWith("  ↳ x") && res.length < 210 && res.endsWith("…"));
  assert.deepEqual(
    claudeEventLines({
      type: "user",
      message: { content: [{ type: "tool_result", is_error: true, content: "boom" }] },
    }),
    ["  ✗ error: boom"],
  );
  assert.deepEqual(claudeEventLines({ type: "result", is_error: false, result: "done" }), [
    "claude finished",
  ]);
  assert.match(
    claudeEventLines({ type: "result", is_error: true, result: "limit" })[0],
    /error: limit/,
  );
  assert.deepEqual(claudeEventLines({ type: "nope" }), []);
  assert.deepEqual(claudeEventLines(null), []);
});

test("the parser handles split chunks, noise and a final line without newline", () => {
  const got = [];
  const p = createStreamParser({ executor: "claude", onLines: (l) => got.push(...l) });
  const a = ev(assistant({ type: "text", text: "one" }));
  p.push(a.slice(0, 10));
  assert.deepEqual(got, []);
  p.push(`${a.slice(10)}not json\n{"result":"plain report"}\n`);
  p.push(JSON.stringify(assistant({ type: "text", text: "two" })));
  assert.deepEqual(got, ["one"]);
  p.end();
  assert.deepEqual(got, ["one", "two"]);
});

test("other executors give no lines (only keepalives)", () => {
  const got = [];
  const p = createStreamParser({ executor: "codex", onLines: (l) => got.push(...l) });
  p.push('{"type":"x"}\nraw text\n');
  assert.deepEqual(got, []);
});

test("obvious secrets are redacted before anything leaves the delegate", () => {
  const cases = [
    "Authorization: Bearer abcdef1234567890",
    "curl -H 'authorization: token abc123def456'",
    "ghp_" + "a".repeat(36),
    "sk-ant-" + "b".repeat(30),
    "AKIA" + "A".repeat(16),
    "export API_KEY=hunter2hunter2",
    '{"password": "hunter2hunter2"}',
    "git clone https://bob:hunter2hunter2@example.com/r.git",
    "run --token hunter2hunter2",
    "eyJhbGciOi.eyJzdWIiOi.sig123",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----",
  ];
  for (const c of cases) {
    const out = redactText(c);
    assert.match(out, /\[redacted\]/, c);
    assert.doesNotMatch(out, /hunter2|abcdef123456|MIIE|a{20}|b{20}|A{16}|abc123def/, c);
  }
  assert.equal(redactText("plain build output 42"), "plain build output 42");
  assert.equal(
    redactText("the key is zzz-exact-value ok", ["zzz-exact-value"]),
    "the key is [redacted] ok",
  );
  // the parser redacts, including exact secrets
  const got = [];
  const p = createStreamParser({
    executor: "claude",
    secrets: ["exact-secret-value"],
    onLines: (l) => got.push(...l),
  });
  p.push(ev(assistant({ type: "text", text: "using exact-secret-value and TOKEN=abcdef123456" })));
  assert.deepEqual(got, ["using [redacted] and TOKEN=[redacted]"]);
});

test("redaction stays linear on adversarial input (no ReDoS)", () => {
  const worst = [
    "a.".repeat(32_000),
    "a-".repeat(32_000),
    `--${"a-".repeat(32_000)}`,
    `${"a".repeat(64_000)}://`,
    "x://a:".repeat(10_000),
    `x://a:${"b".repeat(64_000)}`,
    `-----BEGIN ${"A ".repeat(32_000)}`,
    "token=".repeat(10_000),
    `eyJ${"a".repeat(64_000)}`,
    `authorization:${" ".repeat(64_000)}`,
    "-eyJ".repeat(30_000),
    `eyJ${"a.".repeat(60_000)}`,
    "eyJaaaaaaaa.".repeat(12_000),
    `${"a".repeat(120_000)}token`,
    "token".repeat(24_000),
    `--${"token".repeat(24_000)}`,
    "-".repeat(120_000),
    `x://${"a".repeat(120_000)}`,
  ];
  for (const text of worst) {
    const t0 = performance.now();
    redactText(text, ["exact-secret-value"]);
    const ms = performance.now() - t0;
    assert.ok(ms < 1000, `${ms.toFixed(0)} ms for ${text.slice(0, 20)}…`);
  }
});

test("long names and URL passwords are masked; a JWT where a run starts", () => {
  const name = `MY_${"X".repeat(300)}_TOKEN`;
  assert.equal(redactText(`${name}=s3cr3tvalue`), `${name}=[redacted]`);
  const flag = `--gitea-${"x".repeat(300)}-access-token`;
  assert.equal(redactText(`tool ${flag} s3cr3tvalue`), `tool ${flag} [redacted]`);
  const [user, password] = ["u".repeat(300), "p".repeat(300)];
  assert.equal(
    redactText(`https://${user}:${password}@gitea.local/x.git`),
    `https://${user}:[redacted]@gitea.local/x.git`,
  );
  const jwt = "eyJhbGciOi.eyJzdWIiOi.sig123";
  for (const text of [jwt, `jwt=${jwt}`, `'${jwt}'`, `x.${jwt}`])
    assert.doesNotMatch(redactText(text), /eyJzdWIi/, text);
});

test("a bare carriage return splits text into lines", () => {
  assert.deepEqual(claudeEventLines(assistant({ type: "text", text: "50%\r100%\r\ndone" })), [
    "50%",
    "100%",
    "done",
  ]);
});

test("secret-looking env values are masked exactly", () => {
  const env = {
    GITEA_TOKEN: " plainvalue-xyz123 ",
    DB_PASSWORD: "opensesame-2026",
    ANTHROPIC_API_KEY: "short",
    my_client_secret: "lowercase-name-ok",
    PATH: "/usr/bin:/bin:/usr/local/bin",
    NUMBER: undefined,
  };
  assert.deepEqual(envSecrets(env).sort(), [
    "lowercase-name-ok",
    "opensesame-2026",
    "plainvalue-xyz123",
  ]);
  assert.equal(
    redactText("got plainvalue-xyz123 and opensesame-2026 in /usr/bin", envSecrets(env)),
    "got [redacted] and [redacted] in /usr/bin",
  );
});

test("batching: lines wait for the flush interval, in batches of at most 200", () => {
  let t = 0;
  const sent = [];
  const b = createBatcher({
    send: (l) => sent.push(l),
    now: () => t,
    flushMs: 3000,
    keepaliveMs: 30_000,
  });
  b.add(["a", "b"]);
  t = 1000;
  b.tick();
  assert.equal(sent.length, 0);
  b.add(Array.from({ length: 450 }, (_, i) => `l${i}`));
  t = 3000;
  b.tick();
  assert.deepEqual(
    sent.map((s) => s.length),
    [200, 200, 52],
  );
  b.add(["last"]);
  b.close();
  assert.deepEqual(sent.at(-1), ["last"]);
});

test("a batcher that falls far behind drops the oldest lines and says so", () => {
  const sent = [];
  const b = createBatcher({ send: (l) => sent.push(l), now: () => 0 });
  b.add(Array.from({ length: 2500 }, (_, i) => `l${i}`));
  b.close();
  const all = sent.flat();
  assert.equal(all[0], "… 500 lines dropped");
  assert.equal(all.at(-1), "l2499");
});

test("keepalive: a silent executor still produces a sign of life", () => {
  let t = 0;
  const sent = [];
  const b = createBatcher({ send: (l) => sent.push(l), now: () => t, keepaliveMs: 30_000 });
  b.add(["out"]);
  t = 3000;
  b.tick();
  t = 20_000;
  b.tick();
  assert.deepEqual(sent, [["out"]]);
  t = 33_000;
  b.tick();
  assert.deepEqual(sent.at(-1), ["delegate alive, 33s since last output"]);
  t = 50_000;
  b.tick();
  assert.equal(sent.length, 2);
  t = 63_000;
  b.tick();
  assert.deepEqual(sent.at(-1), ["delegate alive, 63s since last output"]);
  b.add(["back"]);
  t = 70_000;
  b.tick();
  assert.deepEqual(sent.at(-1), ["back"]);
});

test("a sender that throws never reaches the caller", () => {
  const b = createBatcher({
    send: () => {
      throw new Error("relay down");
    },
    now: () => 0,
  });
  b.add(["x"]);
  assert.doesNotThrow(() => b.close());
});

test("the file relay appends records, stops at its byte cap and survives an unwritable path", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "relay-"));
  const file = path.join(dir, "sub", "log.jsonl");
  const relay = createFileRelay({ file, taskId: "t1", attempt: 2 });
  relay(["a", "b"]);
  relay([]);
  const recs = fs
    .readFileSync(file, "utf8")
    .trim()
    .split("\n")
    .map((l) => JSON.parse(l));
  assert.equal(recs.length, 1);
  assert.deepEqual(
    { v: recs[0].v, task_id: recs[0].task_id, attempt: recs[0].attempt, lines: recs[0].lines },
    { v: 1, task_id: "t1", attempt: 2, lines: ["a", "b"] },
  );
  assert.deepEqual(Object.keys(recs[0]).sort(), ["at", "attempt", "lines", "task_id", "v"]);
  const capped = createFileRelay({
    file: path.join(dir, "c.jsonl"),
    taskId: "t",
    attempt: 1,
    maxBytes: 150,
  });
  for (let i = 0; i < 10; i++) capped(["x".repeat(40)]);
  assert.ok(fs.statSync(path.join(dir, "c.jsonl")).size <= 150);
  const bad = createFileRelay({
    file: path.join(dir, "log.jsonl", "x", "y"),
    taskId: "t",
    attempt: 1,
  });
  assert.doesNotThrow(() => bad(["z"]));
  fs.rmSync(dir, { recursive: true });
});

test("startLiveLog follows a growing stdout file and flushes on stop", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "live-"));
  const out = path.join(dir, "out");
  const sent = [];
  const live = startLiveLog({
    stdoutFile: out,
    executor: "claude",
    send: (l) => sent.push(...l),
    intervalMs: 20,
    flushMs: 40,
  });
  fs.writeFileSync(out, ev(assistant({ type: "text", text: "first" })));
  await new Promise((r) => setTimeout(r, 200));
  assert.deepEqual(sent, ["first"]);
  fs.appendFileSync(out, ev(assistant({ type: "text", text: "héllo wörld" })));
  live.stop();
  assert.deepEqual(sent, ["first", "héllo wörld"]);
  fs.rmSync(dir, { recursive: true });
});

test("the live log module never touches the Argus token or the network", () => {
  const src = fs.readFileSync(
    path.join(path.dirname(fileURLToPath(import.meta.url)), "livelog.mjs"),
    "utf8",
  );
  assert.doesNotMatch(src, /ARGUS_AGENT_TOKEN|fetch\(|node:http|Authorization:/);
});
