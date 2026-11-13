// Disposable proof against the real `codex app-server` and a local dummy Responses provider.
// Run: bun tests/e2e/codex-hook-trust-e2e.ts   (no credentials, loopback only, temp CODEX_HOME)
import { spawn, spawnSync } from "child_process";
// `any` below: untyped JSON-RPC frames from the real CLI in a throwaway proof script.
import fs from "fs";
import os from "os";
import path from "path";
import { createInterface } from "readline";
import {
  type CodexRpc,
  codexHooksPath,
  establishCodexHookTrust,
  withCodexGuardHook,
} from "../../overlays/codex-hook-trust";

const root = fs.mkdtempSync(path.join(os.tmpdir(), "codex-trust-e2e-"));
const home = path.join(root, ".codex");
const ws = path.join(root, "ws");
fs.mkdirSync(home, { recursive: true });
fs.mkdirSync(ws);
fs.writeFileSync(
  path.join(home, "config.toml"),
  `model="mock"
model_provider="mock"
sandbox_mode="danger-full-access"
approval_policy="never"
[model_providers.mock]
name="mock"
base_url="http://127.0.0.1:18080/v1"
wire_api="responses"
`,
);
const hooks: Record<string, unknown> = {};
withCodexGuardHook(hooks);
const hooksPath = codexHooksPath({ HOME: root });
fs.writeFileSync(hooksPath, JSON.stringify(hooks));

const cmds = [
  `echo hello > ${ws}/ok.txt`,
  `dd if=/dev/null of=/dev/sdz; touch ${ws}/NEGATIVE_MARKER`,
];
const here = path.dirname(new URL(import.meta.url).pathname);
const env = {
  ...process.env,
  CODEX_HOME: home,
  HOME: root,
  NO_PROXY: "127.0.0.1",
  no_proxy: "127.0.0.1",
};
const mock = spawn(
  "python3",
  [path.join(here, "mock_responses_provider.py"), JSON.stringify(cmds), "2", "exec_command"],
  { stdio: "ignore" },
);
await new Promise((r) => setTimeout(r, 1000));

const codex = spawn("codex", ["app-server", "--listen", "stdio://"], {
  env,
  stdio: ["pipe", "pipe", "ignore"],
});
const pending = new Map<number, (m: any) => void>();
const events: any[] = [];
let completed: () => void = () => {};
const done = new Promise<void>((r) => (completed = r));
createInterface({ input: codex.stdout }).on("line", (l) => {
  const m = JSON.parse(l);
  if (m.id !== undefined && !m.method) pending.get(m.id)?.(m);
  else if (m.method) {
    events.push(m.method);
    if (m.id !== undefined) codex.stdin.write(JSON.stringify({ id: m.id, result: {} }) + "\n");
    if (m.method === "turn/completed") completed();
  }
});
let n = 0;
const rpc: CodexRpc = (method, params) =>
  new Promise((res) => {
    const id = ++n;
    pending.set(id, res);
    codex.stdin.write(JSON.stringify({ id, method, params }) + "\n");
  });
const out: Record<string, unknown> = {};
try {
  await rpc("initialize", { clientInfo: { name: "e2e", version: "0" } });
  codex.stdin.write(JSON.stringify({ method: "initialized" }) + "\n");
  const before = ((await rpc("hooks/list", { cwds: [ws] })).result as any).data[0].hooks.map(
    (h: any) => h.trustStatus,
  );
  const report = await establishCodexHookTrust(rpc, { cwd: ws, hooksPath });
  const thread = (await rpc("thread/start", {
    cwd: ws,
    approvalPolicy: "never",
    sandbox: "danger-full-access",
    model: "mock",
  })) as any;
  await rpc("turn/start", {
    threadId: thread.result.thread.id,
    input: [{ type: "text", text: "go" }],
  });
  await Promise.race([done, new Promise((r) => setTimeout(r, 60000))]);
  out.trustBefore = before;
  out.report = report;
  out.threadSandbox = thread.result.sandbox ?? thread.result.thread?.sandbox;
  out.approval = thread.result.approvalPolicy;
  out.hookEvents = events.filter((e) => e.includes("hook"));
  out.okTxt = fs.existsSync(path.join(ws, "ok.txt"));
  out.negativeMarker = fs.existsSync(path.join(ws, "NEGATIVE_MARKER"));
  out.uid = process.getuid?.();
  out.codexVersion = spawnSync("codex", ["--version"], { encoding: "utf8" }).stdout.trim();
  out.image = process.env.ARGUS_IMAGE ?? "unknown";
} finally {
  codex.kill();
  mock.kill();
}
console.log(JSON.stringify(out, null, 2));
fs.rmSync(root, { recursive: true, force: true });
if (!out.okTxt || out.negativeMarker) process.exit(1);
