import type { components } from "@argus/api-client";
import { useEffect, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import { api, call } from "../api";
import { shortId } from "../format";
import { useLiveData } from "../live";
import { putSetting, resetSetting, type SettingValue, sourceLabel } from "../settings";

type Report = components["schemas"]["ReviewReport"];
type Policy = {
  mode: "off" | "observe" | "enforce";
  default: "standard" | "human";
  rules: { name: string; tier: string; paths: string[] }[];
};

const DEFAULT_POLICY: Policy = { mode: "off", default: "standard", rules: [] };
const TIERS = ["skip", "standard", "human"] as const;

function PolicyReport({ project }: { project: string }) {
  const { data } = useLiveData(
    (signal) =>
      call(
        api.GET("/api/projects/{slug}/review-report", {
          params: { path: { slug: project } },
          signal,
        }),
      ).catch(() => null),
    [project],
  );
  const report: Report["policy"] | undefined = data?.policy;
  if (!report) return null;
  return (
    <div aria-label="Review policy report">
      <h4>Last {report.days} days</h4>
      <table className="small">
        <thead>
          <tr>
            <th>Tier</th>
            <th>Heads</th>
            <th>Tasks</th>
            <th>Verdicts</th>
            <th>Findings</th>
            <th>Succeeded</th>
            <th>Rejected</th>
            <th>Cancelled</th>
          </tr>
        </thead>
        <tbody>
          {TIERS.map((t) => {
            const r = report.tiers[t];
            if (!r) return null;
            return (
              <tr key={t}>
                <td>{t}</td>
                <td>{r.heads}</td>
                <td>{r.tasks}</td>
                <td>
                  {Object.entries(r.verdicts ?? {})
                    .map(([k, n]) => `${k} ${n}`)
                    .join(", ")}
                </td>
                <td>
                  {Object.entries(r.findings_by_priority ?? {})
                    .map(([k, n]) => `${k} ${n}`)
                    .join(", ") || "none"}
                </td>
                <td>{r.succeeded}</td>
                <td>{r.rejected}</td>
                <td>{r.cancelled}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {report.skipped_with_findings.length > 0 && (
        <>
          <p className="small">Skipped heads whose review found a P1 or P2 problem:</p>
          <ul className="plain small">
            {report.skipped_with_findings.map((s) => (
              <li key={`${s.task_id}-${s.head_sha}`}>
                <a href={`#tasks/${s.task_id}`}>{shortId(s.task_id)}</a> {s.pr ?? "no PR"} ·{" "}
                {s.rules.join(", ") || "default"} · {s.findings} finding
                {s.findings === 1 ? "" : "s"}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

/** The `review.policy` editor with its tuning report (docs/specs/review-policy.spec.md). */
export function ReviewPolicySection(props: {
  project: string | null;
  setting?: SettingValue;
  onChanged: () => void;
}) {
  const { project, setting, onChanged } = props;
  const value = (setting?.value ?? DEFAULT_POLICY) as Policy;
  const [mode, setMode] = useState(value.mode);
  const [fallback, setFallback] = useState(value.default);
  const [rules, setRules] = useState(JSON.stringify(value.rules, null, 2));
  const action = useAction();
  const [failure, setFailure] = useState<string>();
  const [done, setDone] = useState<string>();
  const inherited = project === null ? setting?.source !== "global" : setting?.source !== "project";

  useEffect(() => {
    setMode(value.mode);
    setFallback(value.default);
    setRules(JSON.stringify(value.rules, null, 2));
  }, [value.mode, value.default, value.rules, setting?.updated_at]);

  async function run(label: string, change: () => Promise<unknown>, ok: string) {
    await action.run(async () => {
      setFailure(undefined);
      setDone(undefined);
      try {
        await change();
        setDone(ok);
        onChanged();
      } catch (e) {
        setFailure(e instanceof Error ? e.message : String(e));
      }
    }, label);
  }

  return (
    <section>
      <div className="toolbar">
        <h3>Review policy</h3>
      </div>
      <p className="muted small">
        Sort each pull request head by the paths it changes: <code>skip</code> needs no reviewer,{" "}
        <code>standard</code> is reviewed as usual, <code>human</code> is reviewed and then left to
        a person to merge. <strong>Observe</strong> only records the tier; <strong>enforce</strong>{" "}
        applies it. Stored setting: <strong>{setting ? sourceLabel(setting.source) : "…"}</strong>.
      </p>
      <div className="review-fields">
        <label>
          Mode
          <select
            aria-label="review policy mode"
            value={mode}
            onChange={(e) => setMode(e.target.value as Policy["mode"])}
          >
            <option value="off">Off</option>
            <option value="observe">Observe</option>
            <option value="enforce">Enforce</option>
          </select>
        </label>
        <label>
          Default tier
          <select
            aria-label="review policy default tier"
            value={fallback}
            onChange={(e) => setFallback(e.target.value as Policy["default"])}
          >
            <option value="standard">standard</option>
            <option value="human">human</option>
          </select>
        </label>
      </div>
      <label>
        Rules (JSON: name, tier, paths)
        <textarea
          aria-label="review policy rules"
          rows={8}
          value={rules}
          onChange={(e) => setRules(e.target.value)}
        />
      </label>
      <div className="toolbar model-actions">
        <ActionButton
          type="button"
          disabled={action.busy}
          pending={action.is("save")}
          action={() =>
            void run(
              "save",
              async () => {
                await putSetting(
                  "review.policy",
                  { mode, default: fallback, rules: JSON.parse(rules) },
                  project,
                );
              },
              "Saved.",
            )
          }
        >
          Save
        </ActionButton>
        <ActionButton
          type="button"
          disabled={action.busy || inherited}
          pending={action.is("reset")}
          action={() =>
            void run("reset", () => resetSetting("review.policy", project), "Reset to inherited.")
          }
        >
          Reset to inherited
        </ActionButton>
      </div>
      {failure && (
        <p className="error" role="alert">
          {failure}
        </p>
      )}
      {done && <p className="small">{done}</p>}
      {project && <PolicyReport project={project} />}
    </section>
  );
}
