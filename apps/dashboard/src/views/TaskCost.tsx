import { api, call } from "../api";
import {
  budgetLabel,
  budgetLevel,
  budgetShare,
  costLabel,
  money,
  notReportedLabel,
  provisionalLabel,
  tokensOrDash,
} from "../cost";
import { useLiveData } from "../live";

/**
 * What one task spent and its budget (task cost spec 3.7). The figures are estimates
 * and say so; in-flight executor usage shows apart as provisional until its run's final
 * row replaces it. Any event about the task (`task.cost` after each usage row) reloads them.
 */
export function TaskCostTile({ taskId }: { taskId: string }) {
  const { data: cost } = useLiveData(
    (signal) =>
      call(api.GET("/api/tasks/{task_id}/cost", { params: { path: { task_id: taskId } }, signal })),
    [taskId],
    { accepts: (event) => event.subject_id === taskId },
  );
  if (!cost) return null;
  const { totals } = cost;
  const currency = cost.pricing.currency;
  const level = budgetLevel(cost.budget_state);
  const budget = budgetLabel(cost);
  const provisional = provisionalLabel(cost.provisional, currency);
  return (
    <section className="task-cost" aria-label="Cost">
      <header className="task-section-label">Cost</header>
      <p className="task-cost-figures">
        <span>
          {costLabel(totals, currency)} <span className="muted small">estimate</span>
        </span>
        {provisional && <span className="badge"> {provisional}</span>}
        {totals.reported_cost_usd != null && (
          <span>
            {" · "}
            {money(totals.reported_cost_usd, currency)}{" "}
            <span className="muted small">reported</span>
          </span>
        )}
        <span className="muted small">
          {" · "}
          {tokensOrDash(totals.input_tokens)} in · {tokensOrDash(totals.output_tokens)} out ·{" "}
          {totals.runs === 1 ? "1 run" : `${totals.runs} runs`}
        </span>
        {cost.partial && <span className="badge"> partial</span>}
        {notReportedLabel(totals) && <span className="badge"> {notReportedLabel(totals)}</span>}
      </p>
      {level && budget && (
        <div className={`task-budget budget-${level}`}>
          <div
            className="task-budget-bar"
            role="meter"
            aria-label="Budget used"
            aria-valuemin={0}
            aria-valuemax={1}
            aria-valuenow={budgetShare(cost)}
            aria-valuetext={budget}
          >
            <span style={{ width: `${budgetShare(cost) * 100}%` }} />
          </div>
          <span className="small">
            {budget}
            {level !== "ok" && ` · ${cost.budget_state === "exhausted" ? "exhausted" : "near"}`}
          </span>
        </div>
      )}
      {cost.by_attempt.length > 0 && (
        <table className="task-cost-attempts">
          <thead>
            <tr>
              <th>Attempt</th>
              <th>Models</th>
              <th>Runs</th>
              <th>Tokens</th>
              <th>Estimate</th>
            </tr>
          </thead>
          <tbody>
            {cost.by_attempt.map((a) => (
              <tr key={a.attempt}>
                <td>#{a.attempt}</td>
                <td>{a.models.length ? a.models.join(", ") : "—"}</td>
                <td>
                  {a.runs}
                  {a.not_reported_runs > 0 && (
                    <span className="muted small"> ({a.not_reported_runs} not reported)</span>
                  )}
                </td>
                <td>
                  {tokensOrDash(a.input_tokens)} / {tokensOrDash(a.output_tokens)}
                </td>
                <td>{costLabel(a, currency)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
