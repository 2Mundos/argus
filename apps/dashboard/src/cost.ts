// Pure helpers for the Cost view (docs/specs/cost-view.spec.md).
import type { components } from "@argus/api-client";
import { tokens } from "./usage";

export type CostReport = components["schemas"]["CostReport"];
export type CostFigures = components["schemas"]["CostFigures"];
export type CostWindow = CostReport["window"];

export const COST_WINDOWS: { id: CostWindow; label: string }[] = [
  { id: "24h", label: "24h" },
  { id: "7d", label: "7 days" },
  { id: "30d", label: "30 days" },
];

/** A missing figure is "—", never 0 (cost view 3.1). */
export function tokensOrDash(n: number | null | undefined): string {
  return n == null ? "—" : tokens(n);
}

const FRACTION_DIGITS = 2;

export function money(n: number, currency: string): string {
  const small = n > 0 && n < 0.01;
  return `${small ? "<0.01" : n.toFixed(FRACTION_DIGITS)} ${currency}`;
}

/** The estimate for a group: "unpriced" when nothing could be priced but tokens exist,
 * "—" when there is nothing to price, and "partial" when some runs were left out. */
export function costLabel(f: CostFigures, currency: string): string {
  if (f.estimated_cost_usd == null) {
    return f.unpriced_runs > 0 ? "unpriced" : "—";
  }
  return money(f.estimated_cost_usd, currency) + (f.unpriced_runs > 0 ? " (partial)" : "");
}

/** Runs the engine did not measure, as a badge text; null when there are none. */
export function notReportedLabel(f: Pick<CostFigures, "not_reported_runs">): string | null {
  return f.not_reported_runs > 0 ? `${f.not_reported_runs} not reported` : null;
}

/** The API sorts groups with measured runs first; the top five are the first
 * five that have any (cost view 3.3). */
export function topFive<T extends Pick<CostFigures, "measured_runs">>(rows: T[]): T[] {
  return rows.filter((r) => r.measured_runs > 0).slice(0, 5);
}

/** Bar length (0..1) of a group against the largest in its table, by cost when
 * priced, else by tokens; 0 when the group has no figure. */
export function share(f: CostFigures, rows: CostFigures[]): number {
  const cost = (r: CostFigures) => r.estimated_cost_usd ?? 0;
  const toks = (r: CostFigures) => (r.input_tokens ?? 0) + (r.output_tokens ?? 0);
  const useCost = rows.some((r) => cost(r) > 0);
  const value = useCost ? cost : toks;
  const max = Math.max(...rows.map(value), 0);
  return max > 0 ? value(f) / max : 0;
}

export function projectName(p: string | null): string {
  return p ?? "no project";
}

/** Footer text: where the prices come from and what was left unpriced. */
export function pricingNote(p: CostReport["pricing"]): string {
  const unpriced = p.unpriced_models.length
    ? ` Unpriced models: ${p.unpriced_models.join(", ")}.`
    : "";
  return `Estimate from ${p.source} (${p.version}, as of ${p.as_of}), in ${p.currency} from tokens × model price.${unpriced}`;
}

export type TaskCost = components["schemas"]["TaskCost"];
export type BudgetLevel = "ok" | "warning" | "exhausted";

/** The bar colour for a task budget (task cost 3.7); null when the task has none. */
export function budgetLevel(state: TaskCost["budget_state"]): BudgetLevel | null {
  if (state === "none") return null;
  if (state === "exhausted") return state;
  return state === "warning" || state === "partial_warning" ? "warning" : "ok";
}

/** How much of the budget the estimate fills, 0..1; 0 without a budget or a figure. */
export function budgetShare(cost: Pick<TaskCost, "budget_usd" | "totals">): number {
  const spent = cost.totals.estimated_cost_usd ?? 0;
  if (!cost.budget_usd || spent <= 0) return 0;
  return Math.min(spent / cost.budget_usd, 1);
}

/** "<estimate> of <budget> budget", or null without a budget. */
export function budgetLabel(cost: TaskCost): string | null {
  if (cost.budget_usd == null) return null;
  const currency = cost.pricing.currency;
  const spent = money(cost.totals.estimated_cost_usd ?? 0, currency);
  return `${spent} of ${money(cost.budget_usd, currency)} budget`;
}
