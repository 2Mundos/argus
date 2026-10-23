// Hand-drawn SVG charts for account usage (account usage spec 3.6). Colours come
// from CSS variables, so both themes work.

import { relative } from "../format";
import {
  type AccountCredit,
  bucketLabel,
  bucketTokens,
  creditStale,
  creditSummary,
  NOT_MEASURED,
  notMeasured,
  probeNote,
  type QuotaStatus,
  type QuotaStatusWindow,
  type QuotaWindow,
  quotaFull,
  quotaUnknownLabel,
  SOURCE_LABELS,
  snapshotStale,
  tokens,
  totalTokens,
  type UsageBucket,
  windowLabel,
} from "../usage";

/** Tokens per bucket. A null value is a bucket nobody measured: a gap in the line. */
export function Sparkline(props: { values: (number | null)[]; label: string }) {
  const { values } = props;
  const w = 96;
  const h = 24;
  const max = Math.max(...values.map((v) => v ?? 0), 1);
  const step = values.length > 1 ? w / (values.length - 1) : w;
  const runs: string[][] = [[]];
  values.forEach((v, i) => {
    if (v == null) {
      if (runs[runs.length - 1]?.length) runs.push([]);
      return;
    }
    runs[runs.length - 1]?.push(
      `${(i * step).toFixed(1)},${(h - 2 - (v / max) * (h - 4)).toFixed(1)}`,
    );
  });
  return (
    <svg
      className="spark"
      width={w}
      height={h}
      viewBox={`0 0 ${w} ${h}`}
      role="img"
      aria-label={props.label}
    >
      {runs
        .filter((points) => points.length > 0)
        .map((points) =>
          points.length === 1 ? (
            <circle
              key={points[0]}
              cx={points[0]?.split(",")[0]}
              cy={points[0]?.split(",")[1]}
              r="1.5"
              fill="currentColor"
            />
          ) : (
            <polyline
              key={points[0]}
              points={points.join(" ")}
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
            />
          ),
        )}
    </svg>
  );
}

const SERIES = [
  { key: "input", label: "Input (uncached)", className: "bar-input" },
  { key: "cached", label: "Cached input", className: "bar-cached" },
  { key: "output", label: "Output", className: "bar-output" },
] as const;

/** One stacked bar per bucket: uncached input, cached input, output. */
export function StackedBars(props: { buckets: UsageBucket[]; bucket: "hour" | "day"; tz: string }) {
  const { buckets, bucket, tz } = props;
  const w = 640;
  const h = 180;
  const pad = { top: 8, bottom: 22, left: 44 };
  const plotH = h - pad.top - pad.bottom;
  const max = Math.max(...buckets.map((b) => bucketTokens(b) ?? 0), 1);
  const slot = (w - pad.left) / Math.max(buckets.length, 1);
  const barW = Math.max(Math.min(slot * 0.7, 28), 1);
  const labelEvery = Math.ceil(buckets.length / 8);
  const total = buckets.reduce((s, b) => s + (bucketTokens(b) ?? 0), 0);
  const anyUnmeasured = buckets.some(notMeasured);
  return (
    <figure className="chart">
      <svg
        viewBox={`0 0 ${w} ${h}`}
        role="img"
        aria-label={`Tokens per ${bucket}: ${tokens(total)} in total`}
      >
        {[0, 0.5, 1].map((f) => {
          const y = pad.top + plotH * (1 - f);
          return (
            <g key={f} className="grid">
              <line x1={pad.left} x2={w} y1={y} y2={y} />
              <text x={pad.left - 6} y={y + 4} textAnchor="end">
                {tokens(Math.round(max * f))}
              </text>
            </g>
          );
        })}
        {buckets.map((b, i) => {
          const parts = {
            input: Math.max(b.input_tokens - b.cached_input_tokens, 0),
            cached: Math.min(b.cached_input_tokens, b.input_tokens),
            output: b.output_tokens,
          };
          const x = pad.left + i * slot + (slot - barW) / 2;
          let y = pad.top + plotH;
          const unmeasured = notMeasured(b);
          return (
            <g key={b.start}>
              <title>
                {unmeasured
                  ? `${bucketLabel(b.start, bucket, tz)}: ${NOT_MEASURED} (${b.runs} runs gave no figures)`
                  : `${bucketLabel(b.start, bucket, tz)}: ${tokens(totalTokens(b))} tokens` +
                    ` (input ${tokens(b.input_tokens)}, cached ${tokens(b.cached_input_tokens)},` +
                    ` output ${tokens(b.output_tokens)}), ${b.runs} runs`}
              </title>
              {unmeasured && (
                <rect
                  className="bar-unmeasured"
                  x={x}
                  y={pad.top + plotH - 4}
                  width={barW}
                  height={4}
                />
              )}
              {SERIES.map((s) => {
                const height = (parts[s.key] / max) * plotH;
                y -= height;
                return height > 0 ? (
                  <rect
                    key={s.key}
                    className={s.className}
                    x={x}
                    y={y}
                    width={barW}
                    height={height}
                  />
                ) : null;
              })}
              {i % labelEvery === 0 && (
                <text className="axis" x={x + barW / 2} y={h - 6} textAnchor="middle">
                  {bucketLabel(b.start, bucket, tz)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      <figcaption className="legend small muted">
        {SERIES.map((s) => (
          <span key={s.key}>
            <i className={`swatch ${s.className}`} /> {s.label}
          </span>
        ))}
        {anyUnmeasured && (
          <span>
            <i className="swatch bar-unmeasured" /> {NOT_MEASURED} (no figures reported)
          </span>
        )}
      </figcaption>
    </figure>
  );
}

/** A timed window as a bar with its percentage, or a pool as a pill. Always
 * with its source and age: the figure is the provider's, not Argus's. */
export function QuotaMeter(props: { q: QuotaWindow; compact?: boolean }) {
  const { q, compact } = props;
  const stale = snapshotStale(q);
  const full = !stale && quotaFull(q);
  const seen = `${SOURCE_LABELS[q.source]} ${relative(q.observed_at)}`;
  const age = stale ? `stale, observed ${relative(q.observed_at)} · ${seen}` : seen;
  if (q.used_percent == null) {
    return (
      <span className={`pill ${full ? "pill-full" : ""} ${stale ? "stale" : ""}`} title={age}>
        {windowLabel(q)}:{" "}
        {full ? `exhausted · resets ${relative(q.exhausted_until)}` : "within limit"}
        {q.source === "estimated" && " (estimated)"}
      </span>
    );
  }
  const left = 100 - Math.min(Math.max(q.used_percent, 0), 100);
  return (
    <div className={`meter ${full ? "meter-full" : ""} ${stale ? "stale" : ""}`} title={age}>
      <span className="meter-label">
        {windowLabel(q)} <strong>{Math.round(left)}% left</strong>
        {stale && <span className="muted small"> (stale)</span>}
      </span>
      <svg viewBox="0 0 100 6" preserveAspectRatio="none" aria-hidden="true">
        <rect className="meter-track" width="100" height="6" rx="3" />
        <rect className="meter-fill" width={left} height="6" rx="3" />
      </svg>
      {(!compact || stale) && (
        <span className="small muted">
          {!stale && q.resets_at && `resets ${relative(q.resets_at)} · `}
          {compact ? `stale, observed ${relative(q.observed_at)}` : age}
        </span>
      )}
    </div>
  );
}

/** An account's credit (money left on a key), with where it came from and its age. */
export function CreditMeter(props: { credit: AccountCredit; compact?: boolean }) {
  const { credit: c, compact } = props;
  const stale = creditStale(c);
  const seen = `${stale ? "stale, " : ""}read from the provider ${relative(c.observed_at)}`;
  const capped = c.limit != null && c.limit > 0;
  const left = capped ? Math.min(Math.max(((c.remaining ?? 0) / (c.limit ?? 1)) * 100, 0), 100) : 0;
  return (
    <div
      className={`meter ${capped && left <= 0 ? "meter-full" : ""} ${stale ? "stale" : ""}`}
      title={seen}
    >
      <span className="meter-label">
        Credit <strong>{creditSummary(c)}</strong>
        {stale && <span className="muted small"> (stale)</span>}
      </span>
      {capped && (
        <svg viewBox="0 0 100 6" preserveAspectRatio="none" aria-hidden="true">
          <rect className="meter-track" width="100" height="6" rx="3" />
          <rect className="meter-fill" width={left} height="6" rx="3" />
        </svg>
      )}
      {!compact && <span className="small muted">credit, in USD · {seen}</span>}
    </div>
  );
}

/** One locally estimated window (account.quota_status): used of limit, from measured runs only. */
export function EstimatedWindow(props: { w: QuotaStatusWindow; compact?: boolean }) {
  const { w, compact } = props;
  const left = 100 - Math.min(Math.max(w.used_percent, 0), 100);
  const detail = `${tokens(w.used)} of ${tokens(w.limit)} used · ${tokens(w.remaining)} left · estimated`;
  return (
    <div className={`meter ${left <= 0 ? "meter-full" : ""}`} title={detail}>
      <span className="meter-label">
        {w.period} <strong>{Math.round(left)}% left</strong>
      </span>
      <svg viewBox="0 0 100 6" preserveAspectRatio="none" aria-hidden="true">
        <rect className="meter-track" width="100" height="6" rx="3" />
        <rect className="meter-fill" width={left} height="6" rx="3" />
      </svg>
      <span className="small muted">
        {compact ? `${tokens(w.used)} / ${tokens(w.limit)} (est.)` : detail}
      </span>
    </div>
  );
}

/** Quota state apart from the provider's snapshots: estimated windows, or the
 * reason it is unknown, with the last probe as a tooltip. */
export function QuotaStatusView(props: {
  status: QuotaStatus;
  provider?: string;
  compact?: boolean;
}) {
  const { status, provider, compact } = props;
  if (status.state === "unknown") {
    const probe = probeNote(status.last_probe, relative(status.last_probe?.at));
    return (
      <span className={compact ? "muted small" : "muted"} title={probe ?? undefined}>
        {quotaUnknownLabel(status, provider)}
      </span>
    );
  }
  const windows = status.windows ?? [];
  if (windows.length === 0) return null;
  return (
    <div className={compact ? "quota-cell" : "quota-list"}>
      {windows.map((w) => (
        <EstimatedWindow key={w.period} w={w} compact={compact} />
      ))}
    </div>
  );
}

/** The quota cell shared by Accounts and Cost: provider windows (flagged when
 * stale), estimated windows, or the reason nothing is known. */
export function AccountQuota(props: {
  quota?: QuotaWindow[];
  status: QuotaStatus;
  provider?: string;
}) {
  const quota = props.quota ?? [];
  const credit = props.status.credit;
  if (quota.length === 0 && !credit)
    return <QuotaStatusView status={props.status} provider={props.provider} compact />;
  return (
    <div className="quota-cell">
      {quota.map((q) => (
        <QuotaMeter key={q.window} q={q} compact />
      ))}
      {credit && <CreditMeter credit={credit} compact />}
      {(props.status.windows ?? []).length > 0 && (
        <QuotaStatusView status={props.status} provider={props.provider} compact />
      )}
    </div>
  );
}
