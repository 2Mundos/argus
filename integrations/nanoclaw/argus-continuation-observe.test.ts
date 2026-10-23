import { afterEach, describe, expect, it, vi } from "vitest";
import type { ArgusTask } from "./argus.js";
import {
  defaultContinuationObserve,
  observeContinuationViaGitea,
} from "./argus-continuation-observe.js";

const REPO = "https://git.example.test/team/repo.git";
const ALLOW = [REPO];

afterEach(() => {
  vi.restoreAllMocks();
});

function taskWithPrs(refs: string[]): ArgusTask {
  return {
    id: "t1",
    title: "t",
    spec: "s",
    project: "global",
    attempt: 1,
    acceptance: [],
    knowledge_refs: [],
    repository_url: REPO,
    continuation: {
      revision: 1,
      phase: "resume",
      objective: "continue",
      artifacts: refs.map((ref) => ({
        kind: "pr" as const,
        ref,
        state: "open" as const,
        provenance: "worker_reported",
        observed_at: "2026-01-01T00:00:00Z",
        attempt: 1,
        head_sha: null,
        note: null,
        url: null,
      })),
      checklist: [],
      checklist_state: "structured",
      pending: [],
      decisions: [],
      gaps: [],
      environment: [],
      reconciliation: [],
    },
  };
}

describe("defaultContinuationObserve (channel wiring)", () => {
  it("delegates to Gitea only with a scoped repository lease context", async () => {
    const fetchImpl = vi.fn(async () => new Response("missing", { status: 404 })) as typeof fetch;
    const seen = await defaultContinuationObserve(taskWithPrs(["1100"]), {
      repositoryUrl: REPO,
      allowlist: ALLOW,
      fetchImpl,
    });
    expect(fetchImpl).toHaveBeenCalled();
    expect(seen).toBeNull();
  });

  it("returns null without repository lease context (no invented PR state)", async () => {
    const fetchImpl = vi.fn() as typeof fetch;
    const seen = await defaultContinuationObserve(taskWithPrs(["1100"]), {
      repositoryUrl: null,
      allowlist: [],
    });
    expect(seen).toBeNull();
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe("observeContinuationViaGitea", () => {
  it("returns merged state for allowlisted PRs and never invents PRs", async () => {
    const fetchImpl = vi.fn(async (url: string) => {
      if (url.includes("/pulls/1100"))
        return new Response(
          JSON.stringify({
            number: 1100,
            state: "closed",
            merged: true,
            head: { sha: "fffdf3e8" },
            html_url: "https://git.example.test/team/repo/pulls/1100",
          }),
          { status: 200 },
        );
      return new Response("missing", { status: 404 });
    }) as typeof fetch;

    const seen = await observeContinuationViaGitea(taskWithPrs(["1100", "9999"]), {
      repositoryUrl: REPO,
      allowlist: ALLOW,
      fetchImpl,
    });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(seen?.artifacts).toEqual([
      {
        kind: "pr",
        ref: "1100",
        state: "merged",
        head_sha: "fffdf3e8",
        url: "https://git.example.test/team/repo/pulls/1100",
      },
    ]);
  });

  it("rejects cross-project repository URLs (task scoping)", async () => {
    const t = taskWithPrs(["1"]);
    t.repository_url = "https://git.example.test/other/repo.git";
    const fetchImpl = vi.fn() as typeof fetch;
    const seen = await observeContinuationViaGitea(t, {
      repositoryUrl: REPO,
      allowlist: ALLOW,
      fetchImpl,
    });
    expect(seen).toBeNull();
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("rejects repositories outside the allowlist", async () => {
    const fetchImpl = vi.fn() as typeof fetch;
    await expect(
      observeContinuationViaGitea(taskWithPrs(["1"]), {
        repositoryUrl: REPO,
        allowlist: ["https://git.example.test/team/other.git"],
        fetchImpl,
      }),
    ).rejects.toThrow(/not configured/);
  });

  it("returns null when Gitea is unreachable (unknown stays unknown)", async () => {
    const fetchImpl = vi.fn(async () => {
      throw new Error("network down");
    }) as typeof fetch;
    const seen = await observeContinuationViaGitea(taskWithPrs(["1100"]), {
      repositoryUrl: REPO,
      allowlist: ALLOW,
      fetchImpl,
    });
    expect(seen).toBeNull();
  });
});
