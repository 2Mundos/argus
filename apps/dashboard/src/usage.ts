// Pure helpers for account usage and quota (docs/specs/account-usage.spec.md).
import type { components } from "@argus/api-client";

export type QuotaWindow = components["schemas"]["QuotaWindow"];
export type UsageSeries = components["schemas"]["UsageSeries"];
export type AccountUsage = components["schemas"]["AccountUsage"];
export type UsageBucket = components["schemas"]["UsageBucket"];
export type QuotaStatus = components["schemas"]["QuotaStatus"];
export type QuotaStatusWindow = components["schemas"]["QuotaStatusWindow"];
export type AccountCredit = components["schemas"]["AccountCredit"];
export type QuotaRefresh = components["schemas"]["QuotaRefresh"];

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
  session: "Session",
  weekly_opus: "Weekly · Opus",
  weekly_sonnet: "Weekly · Sonnet",
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

const PROVIDER_NAMES: Record<string, string> = {
  anthropic: "Claude",
  cursor: "Cursor",
  openai: "Codex",
  local: "The local model",
};

/** Why Argus has no quota figure. Each is a different fact: no source exists,
 * the source needs something Argus lacks, or the read failed this time. */
export const QUOTA_REASON_LABELS: Record<string, string> = {
  no_usage_records: "No usage reported yet",
  provider_does_not_expose: "Provider does not expose quota",
  window_not_configured: "No quota window configured",
  unsupported_provider: "No quota source is known for this provider",
  api_key_account: "API key: the provider shows usage windows for a login, not for a key",
  not_yet_probed: "Quota not read yet",
  probe_failed: "The last quota read failed",
  probe_skipped: "The last quota read was skipped",
  probe_rate_limited: "Quota read postponed (read recently)",
  probe_disabled: "Quota reader not enabled on this node (ARGUS_CODEX_BIN)",
  credential_missing: "Credential not found: register it again",
  credential_invalid: "The stored credential is not a login this reader understands",
  credential_expired: "Login expired: its holder refreshes it, Argus does not",
  credential_rejected: "The provider rejected the credential",
  oauth_scope_missing:
    "This token cannot read usage: sign in with a full Claude login, not setup-token",
  admin_key_required:
    "A Cursor API key reads usage only through the team Admin API; import a Cursor login to read it from the account",
  no_usage_window_for_login: "The login answered, but states no numeric usage or credit to show",
  window_locked: "The provider locked this window: it has no usage figure",
  no_provider_source: "No read-only quota source exists for this account",
  local_no_quota: "The local model has no provider quota",
  provider_rate_limited: "The provider rate-limited the read; it will be retried",
  fetch_failed: "Could not reach the provider; it will be retried",
  bad_response: "The provider's answer was not understood",
  no_windows: "The provider reported no quota window",
};

/** A human sentence per reason code. Never a blanket claim about a provider: the
 * code says what is missing for this account. `provider` is kept for callers. */
export function quotaUnknownLabel(status: Pick<QuotaStatus, "reason">, _provider?: string): string {
  return (status.reason && QUOTA_REASON_LABELS[status.reason]) || "Quota unknown";
}

/** USD with cents; credit is money, never tokens. */
export function usd(n: number): string {
  return `$${n.toFixed(2)}`;
}

/** A key's credit in words: what is left of the limit, or that it has no cap. */
export function creditSummary(c: AccountCredit): string {
  const reset = c.limit_reset
    ? ` · resets ${c.limit_reset}`
    : c.resets_at
      ? ` · resets ${c.resets_at.slice(0, 10)}`
      : "";
  if (c.limit == null && c.remaining != null) return `${usd(c.remaining)} left${reset}`;
  if (c.limit == null)
    return `No spending cap on this key${c.used != null ? ` · ${usd(c.used)} used` : ""}`;
  return `${usd(c.remaining ?? 0)} left of ${usd(c.limit)}${reset}`;
}

/** Credit read over an hour ago is shown as stale, like a window. */
export function creditStale(c: AccountCredit, now: number = Date.now()): boolean {
  return now - new Date(c.observed_at).getTime() > HOUR;
}

/** The refresh button's sentence, from the outcome and its code. */
export function refreshText(r: Pick<QuotaRefresh, "status" | "code" | "reason">): string {
  const why = r.code ? (QUOTA_REASON_LABELS[r.code] ?? r.reason) : r.reason;
  switch (r.status) {
    case "updated":
      return "Quota updated.";
    case "skipped":
      return `Not read: ${why ?? "skipped"}.`;
    case "rate_limited":
      return "Read less than 5 minutes ago; showing the last result.";
    case "failed":
      return `The read failed: ${why ?? "unknown error"}. The last good figures are kept.`;
  }
}

type Probe = NonNullable<QuotaStatus["last_probe"]>;

/** "Last read: skipped (codex binary not configured), 2 minutes ago", or null. */
export function probeNote(probe: Probe | null | undefined, ago: string): string | null {
  if (!probe) return null;
  const outcome = {
    updated: "updated",
    skipped: "skipped",
    rate_limited: "postponed",
    failed: "failed",
  }[probe.status];
  return `Last read ${outcome}${probe.reason ? ` (${probe.reason})` : ""}, ${ago}`;
}

export const STALE_AFTER_MS = HOUR;

/** A snapshot is stale when observed over an hour ago or its window already reset. */
export function snapshotStale(q: QuotaWindow, now: number = Date.now()): boolean {
  if (now - new Date(q.observed_at).getTime() > STALE_AFTER_MS) return true;
  return q.resets_at != null && new Date(q.resets_at).getTime() <= now;
}

/** Runs that carried figures. Older servers send no count: derive it. */
export function measuredRuns(t: {
  runs?: number;
  unmeasured_runs?: number;
  measured_runs?: number;
}): number {
  return t.measured_runs ?? Math.max((t.runs ?? 0) - (t.unmeasured_runs ?? 0), 0);
}

/** Runs happened, but none gave figures: show "not measured", never 0 tokens. */
export function notMeasured(t: {
  runs?: number;
  unmeasured_runs?: number;
  measured_runs?: number;
}): boolean {
  return (t.runs ?? 0) > 0 && measuredRuns(t) === 0;
}

export const NOT_MEASURED = "Not measured";

/** A token figure, or an em dash when nothing was measured. */
export function tokensOrDash(
  t: { runs?: number; unmeasured_runs?: number; measured_runs?: number },
  n: number,
): string {
  return notMeasured(t) ? "—" : tokens(n);
}

/** "no usage in output ×2, unknown ×1": the unmeasured reasons, most frequent first. */
export function reasonsNote(reasons: Record<string, number> | undefined): string | null {
  const entries = Object.entries(reasons ?? {}).sort(
    (x, y) => y[1] - x[1] || x[0].localeCompare(y[0]),
  );
  if (entries.length === 0) return null;
  return entries.map(([k, n]) => `${k.replaceAll("_", " ")} ×${n}`).join(", ");
}

const ROLE_LABELS: Record<string, string> = {
  engine: "Harness",
  executor: "Executor",
  unknown: "Role unknown",
};

/** "Harness 2 · Executor 1": runs by role, or null when none are known. */
export function roleSplit(runsByRole: Record<string, number> | undefined): string | null {
  const order = ["engine", "executor", "unknown"];
  const parts = order
    .filter((k) => (runsByRole?.[k] ?? 0) > 0)
    .map((k) => `${ROLE_LABELS[k]} ${runsByRole?.[k]}`);
  return parts.length ? parts.join(" · ") : null;
}

/** Tokens of a bucket for a chart: null when its runs were all unmeasured (a gap,
 * not a zero). A bucket with no runs is a measured zero. */
export function bucketTokens(b: UsageBucket): number | null {
  return notMeasured(b) ? null : totalTokens(b);
}

/** "3 runs without measurement", or null when every run was measured. */
export function unmeasuredNote(n: number | undefined): string | null {
  return n ? `${n} ${n === 1 ? "run" : "runs"} without measurement` : null;
}

/** Every run in these totals lacked figures: showing zero tokens would be a lie. */
export function allUnmeasured(t: { runs?: number; unmeasured_runs?: number }): boolean {
  const n = t.unmeasured_runs ?? 0;
  return n > 0 && n >= (t.runs ?? 0);
}

export const TOKEN_LIMIT_PERIODS = ["5h", "24h", "7d"] as const;

/** Parse the three limit inputs ("" = unset). All empty clears the limits (null). */
export function parseTokenLimits(
  raw: Record<(typeof TOKEN_LIMIT_PERIODS)[number], string>,
): { limits: Record<string, number> | null } | { error: string } {
  const limits: Record<string, number> = {};
  for (const period of TOKEN_LIMIT_PERIODS) {
    const text = raw[period].trim();
    if (text === "") continue;
    const n = Number(text);
    if (!Number.isInteger(n) || n < 1)
      return { error: `The ${period} token limit must be a whole number of at least 1, or empty.` };
    limits[period] = n;
  }
  return { limits: Object.keys(limits).length ? limits : null };
}
