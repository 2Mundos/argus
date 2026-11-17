import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import fs from "fs";
import os from "os";
import path from "path";

import { sendCodexRequest, spawnCodexAppServer } from "./codex-app-server.js";
import {
  CODEX_CREDENTIAL_MISSING,
  CodexCredentialMissingError,
  requireCodexCredential,
} from "./codex-credential.js";

// Stands in for the CLI: like the real one it exits 0 when CODEX_HOME holds no auth.json,
// otherwise it stays up and answers each request; a turn/start also completes the turn.
const FAKE_CODEX = `#!/usr/bin/env node
const fs = require("fs");
const path = require("path");
if (!fs.existsSync(path.join(process.env.CODEX_HOME, "auth.json"))) process.exit(0);
require("readline").createInterface({ input: process.stdin }).on("line", (line) => {
  const msg = JSON.parse(line);
  if (msg.id === undefined) return;
  console.log(JSON.stringify({ id: msg.id, result: { ok: msg.method } }));
  if (msg.method === "turn/start") {
    console.log(JSON.stringify({ method: "turn/completed", params: { turn: { status: "completed" } } }));
  }
});
`;

let dir: string;
let saved: NodeJS.ProcessEnv;

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "codex-credential-"));
  fs.mkdirSync(path.join(dir, "bin"));
  fs.writeFileSync(path.join(dir, "bin", "codex"), FAKE_CODEX, { mode: 0o755 });
  saved = { ...process.env };
  process.env.PATH = `${path.join(dir, "bin")}:${process.env.PATH}`;
  process.env.HOME = dir;
  delete process.env.CODEX_HOME;
});

afterEach(() => {
  process.env = saved;
  fs.rmSync(dir, { recursive: true, force: true });
});

const authPath = () => path.join(dir, ".codex", "auth.json");
const putSentinel = () => {
  fs.mkdirSync(path.dirname(authPath()), { recursive: true });
  fs.writeFileSync(authPath(), '{"auth_mode":"chatgpt"}');
};

describe("spawnCodexAppServer credential", () => {
  it("stays up and completes a turn when the gateway sentinel is in CODEX_HOME", async () => {
    putSentinel();
    const server = spawnCodexAppServer();
    try {
      const completed = new Promise((resolve) =>
        server.notificationHandlers.push((n) => n.method === "turn/completed" && resolve(n)),
      );
      expect((await sendCodexRequest(server, "initialize", {})).result).toEqual({
        ok: "initialize",
      });
      await sendCodexRequest(server, "turn/start", {});
      await completed;
    } finally {
      server.process.kill();
    }
  });

  it("refuses by name, before spawning, when no credential is there", () => {
    let error: unknown;
    try {
      spawnCodexAppServer();
    } catch (err) {
      error = err;
    }
    expect(error).toBeInstanceOf(CodexCredentialMissingError);
    expect((error as Error).message.startsWith(`${CODEX_CREDENTIAL_MISSING}:`)).toBe(true);
    expect((error as Error).message).toContain(authPath());
    // CODEX_HOME is created even on refusal, so the stub has somewhere to land.
    expect(fs.statSync(path.dirname(authPath())).isDirectory()).toBe(true);
  });
});

describe("requireCodexCredential", () => {
  it("honours CODEX_HOME and refuses an empty auth.json", () => {
    const home = path.join(dir, "elsewhere");
    expect(() => requireCodexCredential({ CODEX_HOME: home })).toThrow(CodexCredentialMissingError);
    fs.writeFileSync(path.join(home, "auth.json"), "");
    expect(() => requireCodexCredential({ CODEX_HOME: home })).toThrow(CodexCredentialMissingError);
    fs.writeFileSync(path.join(home, "auth.json"), "{}");
    expect(() => requireCodexCredential({ CODEX_HOME: home })).not.toThrow();
  });
});
