// Pure helpers for account usage and quota (docs/specs/account-usage.spec.md).
import type { components } from "@argus/api-client";

export type QuotaWindow = components["schemas"]["QuotaWindow"];
export type UsageSeries = components["schemas"]["UsageSeries"];
export type AccountUsage = components["schemas"]["AccountUsage"];
export type UsageBucket = components["schemas"]["UsageBucket"];

/** Accounts that hold repository leases, not model quota. */
export function hasModelUsage(provider: string): boolean {
  return provider !== "gitea";
}

export type WindowId = "today" | "24h" | "7d" | "30d" | "all";

export const WINDOWS: { id: WindowId; label: string }[] = [
  { id: "today", label: "Today" },
  { id: "24h", label: "Last 24h" },
  { id: "7d", label: "7 days" },
  { id: "30d", label: "30 days" },
  { id: "all", label: "All time" },
];

const HOUR = 3_600_000;
const DAY = 24 * HOUR;

/** The query for a time window: hourly buckets up to a day, daily beyond.
 * "Today" starts at the viewer's local midnight. "All time" starts at the
 * oldest account's creation (`since`), or 30 days back without one. */
export function windowQuery(
  id: WindowId,
  now: Date,
  since?: string,
): { from: string; to: string; bucket: "hour" | "day" } {
  const to = now.toISOString();
  switch (id) {
    case "today": {
      const midnight = new Date(now);
      midnight.setHours(0, 0, 0, 0);
      return { from: midnight.toISOString(), to, bucket: "hour" };
    }
    case "24h":
      return { from: new Date(now.getTime() - DAY).toISOString(), to, bucket: "hour" };
    case "7d":
      return { from: new Date(now.getTime() - 7 * DAY).toISOString(), to, bucket: "day" };
    case "30d":
      return { from: new Date(now.getTime() - 30 * DAY).toISOString(), to, bucket: "day" };
    case "all": {
      const start = since ? new Date(since) : new Date(now.getTime() - 30 * DAY);
      return { from: start.toISOString(), to, bucket: "day" };
    }
  }
}

export function viewerTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** "5h", "Weekly", "30 min", "2d", or a pool name made readable. */
export function windowLabel(q: Pick<QuotaWindow, "window" | "window_minutes">): string {
  const m = q.window_minutes;
  if (m == null) return POOL_LABELS[q.window] ?? q.window.replaceAll("_", " ");
  if (m === 10_080) return "Weekly";
  if (m % 1440 === 0) return `${m / 1440}d`;
  if (m % 60 === 0) return `${m / 60}h`;
  return `${m} min`;
}

const POOL_LABELS: Record<string, string> = {
  cursor: "Cursor models",
  third_party: "Third-party models",
};

/** 1234 → "1.2k", 3_400_000 → "3.4M". */
export function tokens(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
  if (n < 1_000_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  return `${(n / 1_000_000_000).toFixed(1)}B`;
}

export function totalTokens(b: Pick<UsageBucket, "input_tokens" | "output_tokens">): number {
  return b.input_tokens + b.output_tokens;
}

/** Full means the provider says no more until the reset. No other band: the
 * spec sets no threshold below 100%. */
export function quotaFull(q: QuotaWindow, now: number = Date.now()): boolean {
  if (q.used_percent != null) return q.used_percent >= 100;
  return poolState(q, now) === "exhausted";
}

/** A pool's state in words: two-state pools carry no percentage. */
export function poolState(q: QuotaWindow, now: number = Date.now()): "within" | "exhausted" {
  return q.exhausted_until && new Date(q.exhausted_until).getTime() > now ? "exhausted" : "within";
}

export const SOURCE_LABELS: Record<QuotaWindow["source"], string> = {
  reported: "reported by the worker",
  polled: "read from the provider",
  estimated: "estimated",
};

/** Bucket axis label in the viewer's timezone. */
export function bucketLabel(startIso: string, bucket: "hour" | "day", tz: string): string {
  const d = new Date(startIso);
  return bucket === "hour"
    ? d.toLocaleTimeString("en", { hour: "2-digit", minute: "2-digit", timeZone: tz })
    : d.toLocaleDateString("en", { month: "short", day: "numeric", timeZone: tz });
}
