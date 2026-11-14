import { beforeEach, describe, expect, it } from "bun:test";

import {
  CODEX_GUARD_HOOK_COMMAND,
  CodexHookTrustError,
  type CodexRpc,
  establishCodexHookTrust,
  registerExpectedCodexHook,
  resetExpectedCodexHooksForTest,
  withCodexGuardHook,
} from "./codex-hook-trust.js";

const HOOKS_PATH = "/home/node/.codex/hooks.json";
const MEMORY = "bun /app/src/memory/hook.ts";
const HASH_A = `sha256:${"a".repeat(64)}`;
const HASH_B = `sha256:${"b".repeat(64)}`;

interface FakeHook {
  key: string;
  eventName: string;
  command: string;
  matcher: string | null;
  enabled: boolean;
  source: string;
  sourcePath: string;
  currentHash: string;
  trustStatus: string;
}

function hook(overrides: Partial<FakeHook>): FakeHook {
  return {
    key: `${HOOKS_PATH}:pre_tool_use:0:0`,
    eventName: "preToolUse",
    command: CODEX_GUARD_HOOK_COMMAND,
    matcher: "Bash",
    enabled: true,
    source: "user",
    sourcePath: HOOKS_PATH,
    currentHash: HASH_A,
    trustStatus: "untrusted",
    ...overrides,
  };
}

const guard = () => hook({});
const memory = () =>
  hook({
    key: `${HOOKS_PATH}:session_start:0:0`,
    eventName: "sessionStart",
    command: MEMORY,
    matcher: "startup|clear|compact",
    currentHash: HASH_B,
  });

/** A fake app-server: config/batchWrite applies `hooks.state` the way Codex 0.162.1 does. */
function fakeCodex(hooks: FakeHook[], opts: { trustSticks?: boolean } = {}) {
  const calls: { method: string; params: Record<string, unknown> }[] = [];
  const rpc: CodexRpc = async (method, params) => {
    calls.push({ method, params });
    if (method === "hooks/list") {
      return { result: { data: [{ cwd: "/workspace/agent", hooks, warnings: [], errors: [] }] } };
    }
    if (method === "config/batchWrite") {
      const [edit] = params.edits as {
        keyPath: string;
        value: Record<string, { trusted_hash: string }>;
      }[];
      if (opts.trustSticks !== false) {
        for (const h of hooks)
          if (edit.value[h.key]?.trusted_hash === h.currentHash) h.trustStatus = "trusted";
      }
      return { result: { status: "ok" } };
    }
    return { error: { message: `unexpected ${method}` } };
  };
  return { rpc, calls };
}

const options = { cwd: "/workspace/agent", hooksPath: HOOKS_PATH, guardScriptExists: () => true };

beforeEach(() => {
  resetExpectedCodexHooksForTest();
  registerExpectedCodexHook({ eventName: "sessionStart", command: MEMORY });
});

describe("establishCodexHookTrust", () => {
  it("trusts exactly the guard and memory hook by their current hash", async () => {
    const { rpc, calls } = fakeCodex([guard(), memory()]);

    const report = await establishCodexHookTrust(rpc, options);

    expect(report.trusted).toEqual([guard().key, memory().key]);
    expect(calls.map((c) => c.method)).toEqual(["hooks/list", "config/batchWrite", "hooks/list"]);
    expect(calls[1].params).toEqual({
      edits: [
        {
          keyPath: "hooks.state",
          mergeStrategy: "upsert",
          value: {
            [guard().key]: { trusted_hash: HASH_A },
            [memory().key]: { trusted_hash: HASH_B },
          },
        },
      ],
      reloadUserConfig: true,
    });
  });

  it("writes nothing when every expected hook is already trusted", async () => {
    const { rpc, calls } = fakeCodex([
      hook({ trustStatus: "trusted" }),
      { ...memory(), trustStatus: "trusted" },
    ]);

    await establishCodexHookTrust(rpc, options);

    expect(calls.map((c) => c.method)).toEqual(["hooks/list", "hooks/list"]);
  });

  it("refuses when the guard hook is not registered", async () => {
    const { rpc, calls } = fakeCodex([memory()]);

    await expect(establishCodexHookTrust(rpc, options)).rejects.toThrow(
      /preToolUse hook .* is not registered/,
    );
    expect(calls.map((c) => c.method)).toEqual(["hooks/list"]);
  });

  it("refuses when the guard is registered on another matcher", async () => {
    const { rpc } = fakeCodex([hook({ matcher: "*" }), memory()]);

    await expect(establishCodexHookTrust(rpc, options)).rejects.toThrow(/is not registered/);
  });

  it("refuses when a hook is still untrusted after writing", async () => {
    const { rpc } = fakeCodex([guard(), memory()], { trustSticks: false });

    await expect(establishCodexHookTrust(rpc, options)).rejects.toThrow(
      /is untrusted after trusting/,
    );
  });

  it("refuses a disabled hook without trusting anything", async () => {
    const { rpc, calls } = fakeCodex([hook({ enabled: false }), memory()]);

    await expect(establishCodexHookTrust(rpc, options)).rejects.toThrow(/is disabled/);
    expect(calls.some((c) => c.method === "config/batchWrite")).toBe(false);
  });

  it("does not trust a foreign hook, even one running the same command", async () => {
    const project = hook({
      key: "/workspace/agent/.codex/hooks.json:pre_tool_use:0:0",
      source: "project",
      sourcePath: "/workspace/agent/.codex/hooks.json",
    });
    const other = hook({
      key: `${HOOKS_PATH}:stop:0:0`,
      eventName: "stop",
      command: "custom-stop",
    });
    const { rpc, calls } = fakeCodex([project, other, guard(), memory()]);

    const report = await establishCodexHookTrust(rpc, options);

    const written = (calls[1].params.edits as { value: Record<string, unknown> }[])[0].value;
    expect(Object.keys(written)).toEqual([guard().key, memory().key]);
    expect(report.skipped).toEqual([project.key, other.key]);
  });

  it("refuses when only a foreign copy of the guard is listed", async () => {
    const project = hook({ source: "project", sourcePath: "/workspace/agent/.codex/hooks.json" });
    const { rpc } = fakeCodex([project, memory()]);

    await expect(establishCodexHookTrust(rpc, options)).rejects.toThrow(/is not registered/);
  });

  it("refuses before any RPC when the guard script is missing from the image", async () => {
    const { rpc, calls } = fakeCodex([guard(), memory()]);

    await expect(
      establishCodexHookTrust(rpc, { ...options, guardScriptExists: () => false }),
    ).rejects.toThrow(CodexHookTrustError);
    expect(calls).toEqual([]);
  });

  it("re-trusts a modified hook only by the hash Codex now reports", async () => {
    const changed = `sha256:${"c".repeat(64)}`;
    const { rpc, calls } = fakeCodex([
      hook({ trustStatus: "modified", currentHash: changed }),
      { ...memory(), trustStatus: "trusted" },
    ]);

    await establishCodexHookTrust(rpc, options);

    const written = (calls[1].params.edits as { value: unknown }[])[0].value;
    expect(written).toEqual({ [guard().key]: { trusted_hash: changed } });
  });

  it.each([
    ["truncated", `sha256:${"a".repeat(63)}`],
    ["uppercase", `sha256:${"A".repeat(64)}`],
    ["other algorithm", `md5:${"a".repeat(32)}`],
    ["trailing data", `${HASH_A}\n`],
    ["empty", ""],
  ])("refuses a modified hook with a %s hash without writing", async (_name, currentHash) => {
    const { rpc, calls } = fakeCodex([hook({ trustStatus: "modified", currentHash }), memory()]);

    await expect(establishCodexHookTrust(rpc, options)).rejects.toThrow(/unusable hash/);
    expect(calls.map((c) => c.method)).toEqual(["hooks/list"]);
  });

  it.each(["unknown", "", "TRUSTED", "blocked"])(
    "refuses trust status %p without writing",
    async (trustStatus) => {
      const { rpc, calls } = fakeCodex([hook({ trustStatus }), memory()]);

      await expect(establishCodexHookTrust(rpc, options)).rejects.toThrow(/has trust status/);
      expect(calls.map((c) => c.method)).toEqual(["hooks/list"]);
    },
  );

  it("does not trust a guard whose command differs by one character", async () => {
    const tampered = hook({ command: CODEX_GUARD_HOOK_COMMAND.replace(/\.py$/, ".pz") });
    const { rpc, calls } = fakeCodex([tampered, memory()]);

    await expect(establishCodexHookTrust(rpc, options)).rejects.toThrow(/is not registered/);
    expect(calls.some((c) => c.method === "config/batchWrite")).toBe(false);
  });

  it("does not trust a user hook from another hooks.json running the same command", async () => {
    const elsewhere = hook({
      key: "/tmp/elsewhere/.codex/hooks.json:pre_tool_use:0:0",
      sourcePath: "/tmp/elsewhere/.codex/hooks.json",
    });
    const { rpc, calls } = fakeCodex([elsewhere, guard(), memory()]);

    const report = await establishCodexHookTrust(rpc, options);

    const written = (calls[1].params.edits as { value: Record<string, unknown> }[])[0].value;
    expect(Object.keys(written)).toEqual([guard().key, memory().key]);
    expect(report.skipped).toEqual([elsewhere.key]);
  });

  it("a worker-set environment cannot stand in for a missing guard script", async () => {
    const saved = { ...process.env };
    Object.assign(process.env, {
      HOME: "/tmp/elsewhere",
      CODEX_HOME: "/tmp/elsewhere/.codex",
      ARGUS_COMMAND_GUARD: "0",
    });
    try {
      const { rpc, calls } = fakeCodex([guard(), memory()]);

      await expect(
        establishCodexHookTrust(rpc, { ...options, guardScriptExists: () => false }),
      ).rejects.toThrow(/missing from this image/);
      expect(calls).toEqual([]);
    } finally {
      for (const key of ["HOME", "CODEX_HOME", "ARGUS_COMMAND_GUARD"]) {
        if (saved[key] === undefined) delete process.env[key];
        else process.env[key] = saved[key];
      }
    }
  });

  it("refuses when hooks/list reports errors or fails", async () => {
    const failing: CodexRpc = async () => ({ error: { message: "boom" } });
    await expect(establishCodexHookTrust(failing, options)).rejects.toThrow(
      /hooks\/list failed: boom/,
    );

    const erroring: CodexRpc = async () => ({
      result: {
        data: [
          { cwd: "/workspace/agent", hooks: [], warnings: [], errors: [{ message: "bad json" }] },
        ],
      },
    });
    await expect(establishCodexHookTrust(erroring, options)).rejects.toThrow(/reported errors/);
  });
});

describe("withCodexGuardHook", () => {
  it("adds one Bash guard entry and keeps other hooks", () => {
    const config: Record<string, unknown> = {
      hooks: {
        PreToolUse: [
          {
            matcher: "Bash",
            hooks: [{ type: "command", command: CODEX_GUARD_HOOK_COMMAND }, { command: "other" }],
          },
        ],
      },
    };

    withCodexGuardHook(config);
    withCodexGuardHook(config);

    expect((config.hooks as { PreToolUse: unknown[] }).PreToolUse).toEqual([
      { matcher: "Bash", hooks: [{ command: "other" }] },
      {
        matcher: "Bash",
        hooks: [expect.objectContaining({ type: "command", command: CODEX_GUARD_HOOK_COMMAND })],
      },
    ]);
  });

  it("rejects a PreToolUse that is not an array", () => {
    expect(() => withCodexGuardHook({ hooks: { PreToolUse: {} } })).toThrow(CodexHookTrustError);
  });
});
