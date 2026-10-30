import { useState } from "react";
import { api, call } from "../api";
import {
  COST_WINDOWS,
  type CostFigures,
  type CostReport,
  type CostWindow,
  costLabel,
  notReportedLabel,
  pricingNote,
  projectName,
  share,
  tokensOrDash,
  topFive,
} from "../cost";
import { useLiveData } from "../live";
import { PageHeader } from "../PageHeader";
import { bucketLabel, viewerTimeZone } from "../usage";
import { AccountQuota } from "./charts";

/** Tokens and estimated cost per account, project and day (cost view spec). */
export function Cost() {
  const [win, setWin] = useState<CostWindow>("7d");
  const tz = viewerTimeZone();
  const { data, error } = useLiveData(
    (signal) =>
      call(api.GET("/api/accounts/cost", { params: { query: { window: win, tz } }, signal })),
    [win, tz],
  );
  return (
    <section>
      <PageHeader title="Cost" lead="Tokens and estimated cost for the selected window.">
        <div className="segmented" role="group" aria-label="Window">
          {COST_WINDOWS.map((w) => (
            <button
              key={w.id}
              type="button"
              className={w.id === win ? "active" : ""}
              aria-pressed={w.id === win}
              onClick={() => setWin(w.id)}
            >
              {w.label}
            </button>
          ))}
        </div>
      </PageHeader>
      {error && <p className="error">{error.message}</p>}
      {data ? <Report report={data} tz={tz} /> : !error && <p className="muted">Loading…</p>}
    </section>
  );
}

export function Report({ report, tz }: { report: CostReport; tz: string }) {
  const { totals, pricing } = report;
  const currency = pricing.currency;
  const missed = notReportedLabel(totals);
  return (
    <>
      <div className="stats">
        <Stat label="Input" value={tokensOrDash(totals.input_tokens)} />
        <Stat label="Output" value={tokensOrDash(totals.output_tokens)} />
        <Stat label="Cache" value={tokensOrDash(totals.cached_input_tokens)} />
        <Stat label={`Cost (estimate, ${currency})`} value={costLabel(totals, currency)} />
        <Stat label="Measured runs" value={String(totals.measured_runs)} />
        {missed && <Stat label="Runs without figures" value={missed} />}
        {(totals.attempts ?? 0) > 0 && (
          <Stat
            label="Attempts measured"
            value={`${totals.measured_attempts ?? 0} of ${totals.attempts ?? 0}`}
          />
        )}
      </div>
      {totals.reported_cost_usd != null && (
        <p className="muted small">
          Cost reported by engines (not added to the estimate):{" "}
          {totals.reported_cost_usd.toFixed(2)} USD
        </p>
      )}
      <h3>By day</h3>
      <DailyBars report={report} tz={tz} />
      <h3>By account</h3>
      <Table
        rows={report.by_account.map((a) => ({
          key: a.account_id,
          name: a.name,
          f: a,
          extra: <AccountQuota quota={a.quota} status={a.quota_status} provider={a.provider} />,
        }))}
        currency={currency}
        label="Account"
        extraLabel="Quota"
      />
      <h3>By project</h3>
      <Table
        rows={report.by_project.map((p) => ({
          key: p.project ?? "",
          name: projectName(p.project),
          f: p,
        }))}
        currency={currency}
        label="Project"
      />
      <p className="muted small" data-testid="pricing-note">
        {pricingNote(pricing)}
      </p>
    </>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat">
      <span className="n">{value}</span>
      <span className="muted small">{label}</span>
    </div>
  );
}

type Row = { key: string; name: string; f: CostFigures; extra?: React.ReactNode };

function Table(props: { rows: Row[]; currency: string; label: string; extraLabel?: string }) {
  const { rows, currency } = props;
  const all = rows.map((r) => r.f);
  const top = new Set(topFive(rows.map((r) => ({ ...r.f, key: r.key }))).map((r) => r.key));
  return (
    <table>
      <thead>
        <tr>
          <th>{props.label}</th>
          <th>Input</th>
          <th>Output</th>
          <th>Cache</th>
          <th>Cost (estimate)</th>
          <th>Runs</th>
          {props.extraLabel && <th>{props.extraLabel}</th>}
        </tr>
      </thead>
      <tbody>
        {rows.map(({ key, name, f, extra }) => (
          <tr key={key} data-top={top.has(key) ? "true" : undefined}>
            <td>
              {top.has(key) && <span className="badge">top {[...top].indexOf(key) + 1}</span>}{" "}
              {name}
              <svg
                className="chart"
                viewBox="0 0 100 4"
                preserveAspectRatio="none"
                aria-hidden="true"
              >
                <rect className="bar-input" width={share(f, all) * 100} height="4" rx="2" />
              </svg>
            </td>
            <td>{tokensOrDash(f.input_tokens)}</td>
            <td>{tokensOrDash(f.output_tokens)}</td>
            <td>{tokensOrDash(f.cached_input_tokens)}</td>
            <td>{costLabel(f, currency)}</td>
            <td>
              {f.runs === 0 ? "no usage" : f.measured_runs > 0 ? f.measured_runs : "—"}
              {notReportedLabel(f) && <span className="badge"> {notReportedLabel(f)}</span>}
            </td>
            {props.extraLabel && <td>{extra}</td>}
          </tr>
        ))}
        {rows.length === 0 && (
          <tr>
            <td colSpan={7} className="muted">
              No usage in this window.
            </td>
          </tr>
        )}
      </tbody>
    </table>
  );
}

/** One bar per day: input, cached and output tokens; an empty day is an empty slot. */
function DailyBars({ report, tz }: { report: CostReport; tz: string }) {
  const days = report.daily;
  const w = 640;
  const h = 120;
  const slot = w / Math.max(days.length, 1);
  const barW = Math.max(Math.min(slot * 0.7, 28), 1);
  const total = (d: CostFigures) =>
    (d.input_tokens ?? 0) + (d.output_tokens ?? 0) + (d.cached_input_tokens ?? 0);
  const max = Math.max(...days.map(total), 1);
  const labelEvery = Math.ceil(days.length / 8);
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${w} ${h + 18}`} role="img" aria-label="Tokens per day">
        {days.map((d, i) => {
          const x = i * slot + (slot - barW) / 2;
          const parts = [
            ["bar-input", d.input_tokens],
            ["bar-cached", d.cached_input_tokens],
            ["bar-output", d.output_tokens],
          ] as const;
          let y = h;
          const label = bucketLabel(d.start, "day", tz);
          return (
            <g key={d.start}>
              <title>
                {d.measured_runs === 0 && d.not_reported_runs === 0
                  ? `${label}: no usage`
                  : `${label}: ${tokensOrDash(d.input_tokens)} input, ${tokensOrDash(d.output_tokens)} output${
                      d.not_reported_runs ? `, ${d.not_reported_runs} not reported` : ""
                    }`}
              </title>
              {parts.map(([cls, v]) => {
                const bar = ((v ?? 0) / max) * h;
                y -= bar;
                return (
                  bar > 0 && (
                    <rect key={cls} className={cls} x={x} y={y} width={barW} height={bar} />
                  )
                );
              })}
              {i % labelEvery === 0 && (
                <text x={x + barW / 2} y={h + 13} textAnchor="middle">
                  {label}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      <figcaption className="legend small muted">
        <span>
          <i className="swatch bar-input" /> Input
        </span>
        <span>
          <i className="swatch bar-cached" /> Cache
        </span>
        <span>
          <i className="swatch bar-output" /> Output
        </span>
      </figcaption>
    </figure>
  );
}
