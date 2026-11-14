// Acceptance run of an INSTALLED NanoClaw Codex provider against a dummy local Responses
// provider (docs/specs/codex-hook-trust.spec.md, Verification boundary).
// Run: ARGUS_IMAGE_DIGEST=sha256:... \
//        bun tests/e2e/installed-provider-e2e.ts <runner-root> [--out receipt.json]
//   <runner-root> is container/agent-runner of an installed NanoClaw, never the live /app.
// Measures isolation first and exits 3 (BLOCKED) before any model work when it is not as
// required. No credentials, loopback only, temp HOME/CODEX_HOME. Exit: 0 passed, 1 failed,
// 2 usage, 3 blocked.
import { type ChildProcess, spawn, spawnSync } from "child_process";
import crypto from "crypto";
import fs from "fs";
import os from "os";
import path from "path";
import { createInterface } from "readline";
import {
  baseReceipt,
  EXIT_USAGE,
  exitCodeFor,
  isolationBlockers,
  measureIsolation,
  type Probe,
  providerPath,
  providerSourceProblems,
  type Receipt,
  runFailures,
} from "./installed-provider-preflight";

const args = process.argv.slice(2);
const outIndex = args.indexOf("--out");
const outFile = outIndex >= 0 ? args[outIndex + 1] : undefined;
const runnerRoot = args.find((a, i) => !a.startsWith("--") && (outIndex < 0 || i !== outIndex + 1));
if (!runnerRoot || (outIndex >= 0 && !outFile)) {
  console.error("usage: installed-provider-e2e.ts <runner-root> [--out receipt.json]");
  process.exit(EXIT_USAGE);
}

const probe: Probe = {
  readFile: (file) => {
    try {
      return fs.readFileSync(file, "utf8");
    } catch {
      return null;
    }
  },
  which: (name) => {
    const r = spawnSync("sh", ["-c", 'command -v "$1"', "sh", name], { encoding: "utf8" });
    return r.status === 0 ? r.stdout.trim() || null : null;
  },
  run: (file, argv) => {
    const r = spawnSync(file, argv, { encoding: "utf8" });
    return r.status === 0 ? r.stdout + r.stderr : null;
  },
  env: { ARGUS_IMAGE_DIGEST: process.env.ARGUS_IMAGE_DIGEST },
};

function sha256(file: string): string | null {
  const text = probe.readFile(file);
  return text === null ? null : `sha256:${crypto.createHash("sha256").update(text).digest("hex")}`;
}

function finish(receipt: Receipt): never {
  const json = `${JSON.stringify(receipt, null, 2)}\n`;
  if (outFile) fs.writeFileSync(outFile, json);
  process.stdout.write(json);
  process.exit(exitCodeFor(receipt.status));
}

const realRoot = fs.existsSync(runnerRoot) ? fs.realpathSync(runnerRoot) : null;
const src = realRoot ? providerPath(realRoot) : null;
if (!src) {
  console.error(`${runnerRoot}: not an installed runner root (missing, or the live /app runtime)`);
  process.exit(EXIT_USAGE);
}
const provider: Receipt["provider"] = {
  path: src,
  sha256: sha256(src),
  overlaySha256: sha256(path.join(path.dirname(src), "codex-hook-trust.ts")),
};

// 1. Isolation preflight, before reading further or spawning anything.
const isolation = measureIsolation(probe);
const receipt = baseReceipt(isolation, provider);
const blockers = isolationBlockers(isolation);
if (blockers.length > 0) finish({ ...receipt, status: "blocked", reasons: blockers });

// 2. The installed provider's source.
const sourceText = probe.readFile(src);
const problems =
  sourceText === null ? ["provider source missing"] : providerSourceProblems(sourceText);
if (problems.length > 0) finish({ ...receipt, status: "failed", reasons: problems });

// 3. Drive the real provider module: spawn, initialize (+ hook trust), thread, turn.
const root = fs.mkdtempSync(path.join(os.tmpdir(), "installed-provider-e2e-"));
const ws = path.join(root, "ws");
const codexHome = path.join(root, ".codex");
fs.mkdirSync(ws);
fs.mkdirSync(codexHome);
const okFile = path.join(ws, "ok.txt");
const marker = path.join(ws, "NEGATIVE_MARKER");
// Benign command, then a guard-denied one that is harmless even if it ran: `mkfs --version`
// only prints a version; the marker it would leave is the evidence that the deny failed.
const commands = [`echo hello > ${okFile}`, `mkfs --version; touch ${marker}`];

let mock: ChildProcess | undefined;
let server: { process: ChildProcess } | undefined;
// `any` here and below: the provider module and its JSON-RPC frames are loaded from a path given
// at run time, so they have no static types (a throwaway acceptance script).
let codex: any;
try {
  mock = spawn(
    "python3",
    [
      path.join(import.meta.dir, "mock_responses_provider.py"),
      JSON.stringify(commands),
      String(commands.length),
      "exec_command",
      "0",
    ],
    { stdio: ["ignore", "pipe", "ignore"] },
  );
  const port = await new Promise<string>((resolve, reject) => {
    mock?.on("exit", () => reject(new Error("mock provider exited")));
    createInterface({ input: mock?.stdout ?? process.stdin }).once("line", (l) =>
      resolve(l.replace(/^ready /, "")),
    );
  });
  fs.writeFileSync(
    path.join(codexHome, "config.toml"),
    `model="mock"
model_provider="mock"
[model_providers.mock]
name="mock"
base_url="http://127.0.0.1:${port}/v1"
wire_api="responses"
`,
  );
  Object.assign(process.env, {
    HOME: root,
    CODEX_HOME: codexHome,
    NO_PROXY: "127.0.0.1,localhost",
    no_proxy: "127.0.0.1,localhost",
  });
  process.chdir(ws);

  codex = await import(src);
  const hooksPath = path.join(codexHome, "hooks.json");
  fs.writeFileSync(
    hooksPath,
    codex.reconcileCodexHooksJson(
      "",
      { command: "true", legacyCommands: [], sources: ["startup"] },
      hooksPath,
      false,
    ),
  );

  const childPath = codex.buildCodexProcessEnv(process.env).PATH as string | undefined;
  const selected = spawnSync("sh", ["-c", "command -v codex"], {
    encoding: "utf8",
    env: { PATH: childPath ?? "" },
  }).stdout.trim();

  const hookEvents: string[] = [];
  const commandExits: Receipt["commandExits"] = [];
  const sent: { method?: string; params?: Record<string, unknown> }[] = [];
  let turnDone: () => void = () => {};
  const turnCompleted = new Promise<void>((r) => (turnDone = r));

  server = codex.spawnCodexAppServer();
  const proc = server?.process as ChildProcess;
  const write = proc.stdin?.write.bind(proc.stdin) as (chunk: string) => boolean;
  (proc.stdin as unknown as { write: (chunk: string) => boolean }).write = (chunk: string) => {
    for (const line of String(chunk).split("\n").filter(Boolean)) sent.push(JSON.parse(line));
    return write(chunk);
  };
  const s = server as unknown as {
    notificationHandlers: ((n: { method: string; params?: any }) => void)[];
  };
  s.notificationHandlers.push((n) => {
    if (/hook/i.test(n.method)) hookEvents.push(n.method);
    const item = n.params?.item;
    if (n.method === "item/completed" && item?.type === "commandExecution") {
      commandExits.push({
        command: String(item.command),
        exitCode: item.exitCode ?? null,
        status: item.status ?? null,
      });
    }
    if (n.method === "turn/completed") turnDone();
  });
  codex.attachCodexAutoApproval(server);

  await codex.initializeCodexAppServer(server);
  const listed = await codex.sendCodexRequest(server, "hooks/list", { cwds: [ws] });
  const trustStatuses: Record<string, string> = {};
  for (const h of listed.result?.data?.[0]?.hooks ?? []) {
    if (h.source === "user" && path.resolve(h.sourcePath) === hooksPath)
      trustStatuses[h.key] = h.trustStatus;
  }
  const cmdline = (probe.readFile(`/proc/${proc.pid}/cmdline`) ?? "").split("\0").filter(Boolean);
  const exe = fs.readlinkSync(`/proc/${proc.pid}/exe`);

  const threadId = await codex.startOrResumeCodexThread(server, undefined, {
    cwd: ws,
    model: "mock",
  });
  await codex.startCodexTurn(server, { threadId, inputText: "go", cwd: ws });
  let timer: ReturnType<typeof setTimeout> | undefined;
  const turnFinished = await Promise.race([
    turnCompleted.then(() => true),
    new Promise<boolean>((r) => (timer = setTimeout(() => r(false), 120_000))),
  ]);
  clearTimeout(timer);

  const threadStart = sent.find((m) => m.method === "thread/start")?.params ?? {};
  const run: Receipt = {
    ...receipt,
    spawnArgs: proc.spawnargs,
    sandbox: (threadStart.sandbox as string | undefined) ?? null,
    approvalPolicy: (threadStart.approvalPolicy as string | undefined) ?? null,
    runtime: {
      selected: selected ? fs.realpathSync(selected) : null,
      used: { exe, cmdline },
      version: selected ? (probe.run(selected, ["--version"])?.trim() ?? null) : null,
    },
    trustStatuses,
    commandExits,
    hookEvents,
    okFile: fs.existsSync(okFile),
    negativeMarker: fs.existsSync(marker),
  };
  const failures = runFailures(run);
  if (!turnFinished) failures.push("turn did not complete within 120 s");
  receipt.status = failures.length === 0 ? "passed" : "failed";
  Object.assign(receipt, run, { status: receipt.status, reasons: failures });
} catch (error) {
  receipt.status = "failed";
  receipt.reasons = [`${(error as Error).message}`];
} finally {
  if (server && codex) codex.killCodexAppServer(server);
  mock?.kill();
  fs.rmSync(root, { recursive: true, force: true });
}
finish(receipt);
