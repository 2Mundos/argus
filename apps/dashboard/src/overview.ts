// Rules behind the Dashboard view (docs/specs/home.spec.md).
import type { Overview, SystemSummary } from "./api";
import type { StreamStatus } from "./sse";

export type { Overview };
export type Counts = Record<string, number>;

/** One data source of the page. Absent data with no error means "still loading". */
export interface Source<T> {
  data?: T;
  error?: string;
  /** Epoch ms of the last successful load. */
  updatedAt?: number;
}

export type SourceStatus = "loading" | "ok" | "stale" | "error";

/** `stale`: we hold data, but the latest refresh failed or the stream is down. */
export function sourceStatus<T>(source: Source<T>, live: boolean): SourceStatus {
  if (source.data === undefined) return source.error === undefined ? "loading" : "error";
  return source.error !== undefined || !live ? "stale" : "ok";
}

/** A counter is a number only when it was measured. */
export function count(counts: Counts | undefined, key: string): number | undefined {
  return counts?.[key];
}

export function sum(counts: Counts | undefined, keys: string[]): number | undefined {
  if (!counts) return undefined;
  let total = 0;
  for (const key of keys) {
    const n = counts[key];
    if (n === undefined) return undefined; // a missing state is unknown, never 0
    total += n;
  }
  return total;
}

// -- attention -----------------------------------------------------------------------

export type Severity = "critical" | "attention" | "info";

export interface AttentionItem {
  id: string;
  severity: Severity;
  /** Needs a person (approval, answer, review), as opposed to a system fault. */
  human: boolean;
  text: string;
  /** How many things the item stands for; used to order equals. */
  count: number;
  href?: string;
}

export interface Attention {
  items: AttentionItem[];
  /** Sources that failed to load: attention cannot be fully assessed without them. */
  unavailable: string[];
  /** Sources still loading for the first time. */
  pending: string[];
}

const RANK: Record<Severity, number> = { critical: 0, attention: 1, info: 2 };

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

export function attention(input: {
  stream: StreamStatus;
  overview: Source<Overview>;
  system: Source<SystemSummary>;
}): Attention {
  const items: AttentionItem[] = [];
  const unavailable: string[] = [];
  const pending: string[] = [];
  const add = (item: AttentionItem) => {
    if (item.count > 0) items.push(item);
  };

  if (input.stream === "offline") {
    add({
      id: "stream",
      severity: "attention",
      human: false,
      count: 1,
      text: "Live updates are disconnected; the numbers below may be out of date.",
    });
  }

  const sys = input.system;
  if (sys.data === undefined)
    (sys.error === undefined ? pending : unavailable).push("System health");
  else {
    for (const [name, check] of Object.entries(sys.data.readiness.checks)) {
      if (check.status === "ok") continue;
      add({
        id: `check-${name}`,
        severity: check.status === "failing" ? "critical" : "attention",
        human: false,
        count: 1,
        text: `Check "${name}" is ${check.status}${check.detail ? `: ${check.detail}` : ""}.`,
        href: "#dashboard",
      });
    }
  }

  const ov = input.overview;
  if (ov.data === undefined) {
    (ov.error === undefined ? pending : unavailable).push("Totals");
  } else {
    const o = ov.data;
    for (const pool of o.pools.items) {
      if (pool.status !== "ok") {
        add({
          id: `pool-${pool.name}`,
          severity: "critical",
          human: false,
          count: 1,
          text: `Pool "${pool.name}" is ${pool.status}.`,
          href: "#workers/pools",
        });
      } else if (pool.desired > 0 && pool.running < pool.desired) {
        add({
          id: `pool-below-${pool.name}`,
          severity: "attention",
          human: false,
          count: 1,
          text: `Pool "${pool.name}" has ${pool.running} of ${pool.desired} desired instances running.`,
          href: "#workers/pools",
        });
      }
    }
    for (const [provider, c] of Object.entries(o.accounts.by_provider)) {
      const total = Object.values(c).reduce((a, b) => a + b, 0);
      if (total > 0 && c.available === 0) {
        add({
          id: `provider-${provider}`,
          severity: "critical",
          human: false,
          count: 1,
          text: `No available account for provider "${provider}".`,
          href: "#setup/accounts",
        });
      } else if ((c.exhausted ?? 0) > 0) {
        const exhausted = c.exhausted ?? 0;
        add({
          id: `exhausted-${provider}`,
          severity: "attention",
          human: false,
          count: exhausted,
          text: `${plural(exhausted, "account")} exhausted for provider "${provider}".`,
          href: "#setup/accounts",
        });
      }
    }
    add({
      id: "lost-unfinished",
      severity: "attention",
      human: false,
      count: o.agents.lost_on_unfinished_instance,
      text: `${plural(o.agents.lost_on_unfinished_instance, "lost agent")} on instances not yet replaced.`,
      href: "#workers/agents",
    });
    add({
      id: "lost-holding-claims",
      severity: "critical",
      human: false,
      count: o.agents.lost_holding_claims,
      text: `${plural(o.agents.lost_holding_claims, "lost agent")} still hold a claimed task.`,
      href: "#workers/agents",
    });
    {
      const spike = o.agents.lost_spike;
      add({
        id: "lost-spike",
        severity: "attention",
        human: false,
        count: spike.triggered ? spike.current : 0,
        text: `${spike.current} agents lost in the last ${spike.window_hours}h, up from ${spike.previous} in the window before.`,
        href: "#workers/agents",
      });
    }
    add({
      id: "blocked",
      severity: "attention",
      human: false,
      count: o.tasks.blocked ?? 0,
      text: `${plural(o.tasks.blocked ?? 0, "blocked task")}.`,
      href: "#tasks",
    });
    add({
      id: "awaiting-approval",
      severity: "attention",
      human: true,
      count: o.tasks.awaiting_approval ?? 0,
      text: `${plural(o.tasks.awaiting_approval ?? 0, "task")} awaiting approval.`,
      href: "#tasks",
    });
    add({
      id: "decisions-open",
      severity: "attention",
      human: true,
      count: o.decisions.by_state.open ?? 0,
      text: `${plural(o.decisions.by_state.open ?? 0, "open decision")} waiting in the Inbox.`,
      href: "#inbox",
    });
    add({
      id: "in-review",
      severity: "info",
      human: true,
      count: o.tasks.in_review ?? 0,
      text: `${plural(o.tasks.in_review ?? 0, "task")} in review.`,
      href: "#tasks",
    });
    // Failed tasks are history: nothing says whether anyone has dealt with them,
    // and no time window is defined, so they inform and never page.
    add({
      id: "failed",
      severity: "info",
      human: false,
      count: o.tasks.failed ?? 0,
      text: `${plural(o.tasks.failed ?? 0, "failed task")} on record.`,
      href: "#tasks",
    });
    add({
      id: "terminating",
      severity: "info",
      human: false,
      count: o.instances.by_state.terminating ?? 0,
      text: `${plural(o.instances.by_state.terminating ?? 0, "instance")} terminating.`,
      href: "#workers/pools",
    });
    add({
      id: "proposals-open",
      severity: "info",
      human: true,
      count: o.knowledge.proposals_by_state.open ?? 0,
      text: `${plural(o.knowledge.proposals_by_state.open ?? 0, "open knowledge proposal")}.`,
      href: "#knowledge",
    });
  }

  for (const source of unavailable) {
    add({
      id: `unavailable-${source}`,
      severity: "attention",
      human: false,
      count: 1,
      text: `${source} could not be loaded.`,
    });
  }

  items.sort(
    (a, b) =>
      RANK[a.severity] - RANK[b.severity] ||
      Number(b.human) - Number(a.human) ||
      b.count - a.count ||
      a.id.localeCompare(b.id),
  );
  return { items, unavailable, pending };
}

// -- health verdict --------------------------------------------------------------------

export type Verdict = "ok" | "degraded" | "failing" | "unknown";

/** The worst check wins; no data is "unknown", never "ok". */
export function verdict(system: SystemSummary | undefined): Verdict {
  if (!system) return "unknown";
  const statuses = Object.values(system.readiness.checks).map((c) => c.status);
  if (statuses.includes("failing")) return "failing";
  if (statuses.length === 0) return "unknown";
  return statuses.every((s) => s === "ok") ? "ok" : "degraded";
}
