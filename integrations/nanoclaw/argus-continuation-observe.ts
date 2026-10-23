/**
 * Read-only Gitea preflight for the task continuation contract (spec 3.3).
 * Uses the same allowlisted GiteaApi + OneCLI HTTPS proxy as argus-gitea-api in the
 * agent image; runs on the NanoClaw host after the repository lease is applied.
 */
import type { ArgusTask, Observation } from "./argus.js";

export type GiteaObserveContext = {
  /** The allowlisted clone URL for this task (must match task.repository_url). */
  repositoryUrl: string;
  /** Project allowlist entries from Argus pool config (same scope as the lease). */
  allowlist: readonly string[];
  /** Test seam: inject fetch instead of the OneCLI gateway. */
  fetchImpl?: typeof fetch;
};

type PrState = Observation["artifacts"] extends Array<infer A> | undefined
  ? A extends { state: infer S }
    ? S
    : never
  : never;

function parsePrNumber(ref: string): number | null {
  const trimmed = ref.trim();
  const m = /^pr[/#]?(\d+)$/i.exec(trimmed) ?? /^(\d+)$/.exec(trimmed);
  if (!m) return null;
  const n = Number(m[1]);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}

function giteaPrState(pr: { merged?: boolean; state?: string }): PrState {
  if (pr.merged) return "merged";
  if (pr.state === "closed") return "closed";
  if (pr.state === "open") return "open";
  return "unknown";
}

/** Host-side default used by the channel when `ArgusConfig.observe` is omitted. */
export async function defaultContinuationObserve(
  task: ArgusTask,
  ctx: {
    repositoryUrl: string | null;
    allowlist: readonly string[];
    fetchImpl?: typeof fetch;
  },
): Promise<Observation | null> {
  if (!ctx.repositoryUrl || ctx.allowlist.length === 0) return null;
  if (!task.repository_url || task.repository_url !== ctx.repositoryUrl) return null;
  return observeContinuationViaGitea(task, {
    repositoryUrl: ctx.repositoryUrl,
    allowlist: ctx.allowlist,
    fetchImpl: ctx.fetchImpl,
  });
}

/** Observe PR artifacts already recorded on the contract; never invent missing PRs. */
export async function observeContinuationViaGitea(
  task: ArgusTask,
  ctx: GiteaObserveContext,
): Promise<Observation | null> {
  const c = task.continuation;
  if (!c || !task.repository_url) return null;
  if (task.repository_url !== ctx.repositoryUrl) return null;

  const prArtifacts = c.artifacts.filter((a) => a.kind === "pr");
  if (prArtifacts.length === 0) return null;

  const { GiteaApi, createGatewayFetch } = await import("./gitea-api.mjs");
  const fetchImpl = ctx.fetchImpl ?? createGatewayFetch();
  const api = new GiteaApi(task.repository_url, [...ctx.allowlist], fetchImpl);

  const artifacts: NonNullable<Observation["artifacts"]> = [];
  for (const recorded of prArtifacts) {
    const number = parsePrNumber(recorded.ref);
    if (number === null) continue;
    try {
      const pr = await api.getPullRequest(number);
      const state = giteaPrState(pr);
      const headSha =
        typeof pr.head?.sha === "string" && pr.head.sha.length ? pr.head.sha : undefined;
      const url = typeof pr.html_url === "string" ? pr.html_url : (recorded.url ?? undefined);
      artifacts.push({
        kind: "pr",
        ref: recorded.ref,
        url,
        head_sha: headSha,
        state,
      });
    } catch {
      // A failed read is not evidence the PR is absent; leave the recorded contract.
    }
  }
  return artifacts.length ? { artifacts } : null;
}
