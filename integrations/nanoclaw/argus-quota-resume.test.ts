/** Quota resume decisions and host observation (docs/specs/quota-resume.spec.md 3.2-3.4). */
import { describe, expect, it } from "vitest";
import { parseFramedSteer, renderQuotaResume } from "./argus.js";
import {
  claudeSessionReadable,
  decideQuotaResume,
  observeClaudeSession,
  type QuotaResumeOffer,
} from "./argus-quota-resume.js";

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
