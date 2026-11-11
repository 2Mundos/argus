import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const SCRIPT = path.join(path.dirname(fileURLToPath(import.meta.url)), "install-dsh.sh");
const text = fs.readFileSync(SCRIPT, "utf8");
const BYTES = "not a real tarball";
const sha512 = `sha512-${createHash("sha512").update(BYTES).digest("base64")}`;

/** Run the installer with stub curl/uname/xz first on PATH: no network, nothing unpacked. */
function install(t, env = {}, arch = "x86_64") {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "install-dsh-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const stubs = path.join(dir, "stubs");
  fs.mkdirSync(stubs);
  const stub = (name, body) =>
    fs.writeFileSync(path.join(stubs, name), `#!/bin/sh\n${body}\n`, { mode: 0o755 });
  stub("uname", `echo ${arch}`);
  stub("xz", "exit 0");
  // curl -fSL --retry 3 -o FILE URL: write the fixture bytes to FILE.
  stub("curl", `while [ "$1" != "-o" ]; do shift; done; printf '%s' '${BYTES}' > "$2"`);
  stub("tar", 'echo "tar must not run" >&2; touch "$0.ran"; exit 9');
  const prefix = path.join(dir, "opt");
  const bin = path.join(dir, "bin");
  const r = spawnSync("sh", [SCRIPT], {
    encoding: "utf8",
    env: {
      PATH: `${stubs}:${process.env.PATH}`,
      ARGUS_DSH_PREFIX: prefix,
      ARGUS_DSH_BIN_DIR: bin,
      ...env,
    },
  });
  return { ...r, prefix, bin, ran: fs.existsSync(path.join(stubs, "tar.ran")) };
}

test("install-dsh.sh pins dsh and Node for both architectures, with no placeholders", () => {
  assert.match(text, /^set -eu$/m);
  assert.match(text, /DSH_VERSION="\$\{DSH_VERSION:-0\.2\.2-alpha\.3\}"/);
  assert.match(text, /DSH_INTEGRITY="\$\{DSH_INTEGRITY:-sha512-[A-Za-z0-9+/]{86}==\}"/);
  assert.match(text, /NODE_VERSION="v24\.22\.2"/);
  assert.match(text, /NODE_SHA256_X64="\$\{DSH_NODE_SHA256_X64:-[0-9a-f]{64}\}"/);
  assert.match(text, /NODE_SHA256_ARM64="\$\{DSH_NODE_SHA256_ARM64:-[0-9a-f]{64}\}"/);
  assert.doesNotMatch(text, /TODO|PLACEHOLDER|xxxx|<[A-Za-z_-]+>/i);
});

test("install-dsh.sh fails closed on a wrong dsh integrity before unpacking anything", (t) => {
  const r = install(t); // the pinned integrity does not match the stub's bytes
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /^install-dsh: .*integrity/im);
  assert.equal(r.ran, false);
  assert.equal(fs.existsSync(r.prefix), false);
  assert.equal(fs.existsSync(r.bin), false);
});

test("install-dsh.sh fails closed on a wrong Node checksum before unpacking anything", (t) => {
  const r = install(t, { DSH_INTEGRITY: sha512, DSH_NODE_SHA256_X64: "0".repeat(64) });
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /^install-dsh: /m);
  assert.equal(r.ran, false);
  assert.equal(fs.existsSync(r.prefix), false);
});

test("install-dsh.sh fails closed on an unsupported architecture", (t) => {
  const r = install(t, {}, "riscv64");
  assert.notEqual(r.status, 0);
  assert.match(r.stderr, /^install-dsh: no pinned Node runtime for riscv64/m);
  assert.equal(fs.existsSync(r.prefix), false);
});
