/**
 * Quota resume decisions, host observation and the continuation seed
 * (docs/specs/quota-resume.spec.md 3.2-3.4, 3.9). The session mailbox is a temp
 * outbound.db made from NanoClaw's own schema; NanoClaw's session modules are faked.
 */
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import Database from "better-sqlite3";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OUTBOUND_SCHEMA } from "../mailbox/sqlite/schema.js";
import {
  parseFramedSteer,
  quotaResumeFromEnv,
  renderQuotaResume,
  resumeClaudeSessionFromEnv,
} from "./argus.js";
import {
  claudeSessionReadable,
  decideQuotaResume,
  observeClaudeSession,
  type QuotaResumeOffer,
  seedClaudeContinuation,
} from "./argus-quota-resume.js";

const nanoclaw = vi.hoisted(() => ({
  wiring: { agent_group_id: "group-1", session_mode: "per-thread", threads: 1 } as Record<
    string,
    unknown
  >,
  sqlite: true,
  sessions: new Map<string, { id: string; thread_id: string | null }>(),
  outbound: (_group: string, _session: string): string => "",
}));
vi.mock("../db/messaging-groups.js", () => ({
  getMessagingGroupByPlatform: async () => ({ id: "mg-1" }),
  getMessagingGroupAgents: async () => [nanoclaw.wiring],
}));
vi.mock("../mailbox/sqlite/index.js", async (original) => ({
  ...(await original<typeof import("../mailbox/sqlite/index.js")>()),
  outboundDbPath: (group: string, session: string) => nanoclaw.outbound(group, session),
}));
vi.mock("../mailbox/index.js", async (original) => {
  const { SqliteAgentMailbox } = await import("../mailbox/sqlite/index.js");
  return {
    ...(await original<typeof import("../mailbox/index.js")>()),
    getAgentMailbox: () => (nanoclaw.sqlite ? Object.create(SqliteAgentMailbox.prototype) : {}),
  };
});
vi.mock("../session-manager.js", () => ({
  // NanoClaw's resolveSession: one session per thread, created on first use.
  resolveSession: async (_group: string, _mg: string, thread: string | null) => {
    const key = thread ?? "";
    const existing = nanoclaw.sessions.get(key);
    if (existing) return { session: existing, created: false };
    const session = { id: `sess-${nanoclaw.sessions.size + 1}`, thread_id: thread };
    nanoclaw.sessions.set(key, session);
    return { session, created: true };
  },
}));

const offer: QuotaResumeOffer = {
  session_id: "5b1c0f3e-aaaa-bbbb-cccc-0123456789ab",
  engine: "anthropic",
  node: "agent-a",
  from_attempt: 1,
};
const here = { node: "agent-a", engine: "anthropic", canResume: true, readable: true };

describe("decideQuotaResume", () => {
  it.each([
    ["same node, engine, readable", {}, { action: "resume" }],
    ["another node", { node: "agent-b" }, { action: "fallback", reason: "other_node" }],
    ["another engine", { engine: "openai" }, { action: "fallback", reason: "other_engine" }],
    ["host cannot resume", { canResume: false }, { action: "fallback", reason: "not_available" }],
    ["transcript gone", { readable: false }, { action: "fallback", reason: "unreadable" }],
  ])("%s", (_name, override, expected) => {
    expect(decideQuotaResume(offer, { ...here, ...override })).toEqual(expected);
  });

  it("never resumes an engine other than Claude", () => {
    const codex = { ...offer, engine: "openai" };
    expect(decideQuotaResume(codex, { ...here, engine: "openai" })).toEqual({
      action: "fallback",
      reason: "engine_unsupported",
    });
  });
});

describe("host observation", () => {
  async function projects(files: Record<string, string>) {
    const { mkdtemp, mkdir, writeFile } = await import("node:fs/promises");
    const { tmpdir } = await import("node:os");
    const path = await import("node:path");
    const root = await mkdtemp(path.join(tmpdir(), "argus-quota-resume-"));
    for (const [rel, body] of Object.entries(files)) {
      await mkdir(path.dirname(path.join(root, rel)), { recursive: true });
      await writeFile(path.join(root, rel), body);
    }
    return root;
  }

  it("takes the one transcript written since the claim", async () => {
    const root = await projects({ "-workspace/5b1c0f3e-aaaa.jsonl": '{"type":"user"}\n' });
    expect(await observeClaudeSession(root, 0)).toBe("5b1c0f3e-aaaa");
    expect(await claudeSessionReadable(root, "5b1c0f3e-aaaa")).toBe(true);
  });

  it("returns null when it is ambiguous, missing, empty or not an id", async () => {
    const two = await projects({ "p/aaaaaaaa-1.jsonl": "x", "p/bbbbbbbb-2.jsonl": "x" });
    expect(await observeClaudeSession(two, 0)).toBeNull();
    expect(await observeClaudeSession(two, Date.now() + 60_000)).toBeNull();
    expect(await observeClaudeSession("/nonexistent/argus", 0)).toBeNull();
    const empty = await projects({ "p/aaaaaaaa-1.jsonl": "" });
    expect(await observeClaudeSession(empty, 0)).toBeNull();
    expect(await claudeSessionReadable(empty, "aaaaaaaa-1")).toBe(false);
    const odd = await projects({ "p/..%2Fx.jsonl": "x" });
    expect(await observeClaudeSession(odd, 0)).toBeNull();
    expect(await claudeSessionReadable(two, "../etc")).toBe(false);
  });
});

describe("renderQuotaResume", () => {
  it("is one framed operator message", () => {
    const text = renderQuotaResume({ id: "t1", attempt: 2 });
    expect(text.startsWith("[Argus steer] From argus:quota-resume for task t1, attempt 2.")).toBe(
      true,
    );
    expect(parseFramedSteer(text).message).toContain("Continue from where you stopped");
  });
});

const SESSION = "5b1c0f3e-aaaa-bbbb-cccc-0123456789ab";
const OTHER = "9d0e1f2a-aaaa-bbbb-cccc-0123456789ab";

/** An outbound.db as NanoClaw's host creates it for a new session. */
function mailbox(): string {
  const file = path.join(mkdtempSync(path.join(os.tmpdir(), "argus-qr-mailbox-")), "outbound.db");
  const db = new Database(file);
  db.exec(OUTBOUND_SCHEMA);
  db.close();
  return file;
}
const state = (file: string) => {
  const db = new Database(file, { readonly: true });
  try {
    return db.prepare("SELECT key, value FROM session_state ORDER BY key").all();
  } finally {
    db.close();
  }
};
const exec = (file: string, sql: string) => {
  const db = new Database(file);
  db.exec(sql);
  db.close();
};

describe("seedClaudeContinuation", () => {
  it("writes the runner's continuation once and is idempotent for the same id", async () => {
    const file = mailbox();
    expect(await seedClaudeContinuation(file, SESSION)).toBe(true);
    expect(await seedClaudeContinuation(file, SESSION)).toBe(true);
    expect(state(file)).toEqual([{ key: "continuation:claude", value: SESSION }]);
  });

  it("never clobbers another continuation, legacy or current", async () => {
    const other = mailbox();
    await seedClaudeContinuation(other, OTHER);
    expect(await seedClaudeContinuation(other, SESSION)).toBe(false);
    expect(state(other)).toEqual([{ key: "continuation:claude", value: OTHER }]);
    const legacy = mailbox();
    exec(legacy, `INSERT INTO session_state VALUES ('sdk_session_id', '${OTHER}', 'x')`);
    expect(await seedClaudeContinuation(legacy, SESSION)).toBe(false);
    expect(state(legacy)).toEqual([{ key: "sdk_session_id", value: OTHER }]);
  });

  it("does not seed a session whose container already ran", async () => {
    const replied = mailbox();
    exec(
      replied,
      "INSERT INTO messages_out (id, timestamp, kind, content) VALUES ('m', 'x', 'chat', '{}')",
    );
    const acked = mailbox();
    exec(acked, "INSERT INTO processing_ack VALUES ('m', 'completed', 'x')");
    for (const file of [replied, acked]) {
      expect(await seedClaudeContinuation(file, SESSION)).toBe(false);
      expect(state(file)).toEqual([]);
    }
  });

  it("is false for a bad id, a missing file or an unexpected layout", async () => {
    const file = mailbox();
    for (const id of [
      "",
      "../etc/passwd",
      "short",
      "a".repeat(65),
      "x'; DROP TABLE session_state;--",
    ])
      expect(await seedClaudeContinuation(file, id)).toBe(false);
    expect(state(file)).toEqual([]);
    const missing = path.join(path.dirname(file), "absent", "outbound.db");
    expect(await seedClaudeContinuation(missing, SESSION)).toBe(false);
    const odd = path.join(mkdtempSync(path.join(os.tmpdir(), "argus-qr-odd-")), "outbound.db");
    exec(odd, "CREATE TABLE session_state (key TEXT PRIMARY KEY, value TEXT)");
    expect(await seedClaudeContinuation(odd, SESSION)).toBe(false);
    const notDb = path.join(path.dirname(odd), "not.db");
    writeFileSync(notDb, "not a database");
    expect(await seedClaudeContinuation(notDb, SESSION)).toBe(false);
  });
});

describe("resumeClaudeSessionFromEnv", () => {
  let root: string;
  let mailboxes: Map<string, string>;
  beforeEach(() => {
    root = mkdtempSync(path.join(os.tmpdir(), "argus-qr-host-"));
    vi.spyOn(process, "cwd").mockReturnValue(root);
    const project = path.join(
      root,
      "data/v2-sessions/group-1/.claude-shared/projects/-workspace-agent",
    );
    mkdirSync(project, { recursive: true });
    writeFileSync(path.join(project, `${SESSION}.jsonl`), '{"type":"user"}\n');
    mailboxes = new Map();
    nanoclaw.outbound = (_group, session) => {
      if (!mailboxes.has(session)) mailboxes.set(session, mailbox());
      return mailboxes.get(session)!;
    };
    nanoclaw.sessions.clear();
    nanoclaw.sqlite = true;
    nanoclaw.wiring = { agent_group_id: "group-1", session_mode: "per-thread", threads: 1 };
  });
  afterEach(() => vi.restoreAllMocks());

  it("creates the attempt thread's session and seeds it, once", async () => {
    expect(await resumeClaudeSessionFromEnv(SESSION, "argus:t1:2")).toBe(true);
    // a retried claim of the same attempt finds the same session, already seeded
    expect(await resumeClaudeSessionFromEnv(SESSION, "argus:t1:2")).toBe(true);
    expect([...nanoclaw.sessions.values()]).toEqual([{ id: "sess-1", thread_id: "argus:t1:2" }]);
    expect(state(mailboxes.get("sess-1")!)).toEqual([
      { key: "continuation:claude", value: SESSION },
    ]);
  });

  it("is false, seeding nothing, without the transcript or the expected wiring", async () => {
    expect(await resumeClaudeSessionFromEnv(OTHER, "argus:t1:2")).toBe(false); // no transcript
    for (const wiring of [
      { agent_group_id: "group-1", session_mode: "shared", threads: 1 },
      { agent_group_id: "group-1", session_mode: "per-thread", threads: 0 },
      { agent_group_id: "group-2", session_mode: "per-thread", threads: 1 }, // other group's dir
    ]) {
      nanoclaw.wiring = wiring;
      expect(await resumeClaudeSessionFromEnv(SESSION, "argus:t1:2")).toBe(false);
    }
    nanoclaw.wiring = { agent_group_id: "group-1", session_mode: "per-thread", threads: 1 };
    nanoclaw.sqlite = false; // another mailbox implementation
    expect(await resumeClaudeSessionFromEnv(SESSION, "argus:t1:2")).toBe(false);
    expect(nanoclaw.sessions.size).toBe(0);
  });

  it("is opt-in: only ARGUS_QUOTA_RESUME=1 installs it", () => {
    expect(quotaResumeFromEnv({ ARGUS_QUOTA_RESUME: "1" })).toBe(resumeClaudeSessionFromEnv);
    for (const value of [undefined, "", "0", "true", "yes"])
      expect(quotaResumeFromEnv({ ARGUS_QUOTA_RESUME: value })).toBeUndefined();
  });
});
