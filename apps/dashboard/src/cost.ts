// Pure helpers for the Custo view (docs/specs/cost-view.spec.md).
import type { components } from "@argus/api-client";
import { tokens } from "./usage";

export type CostReport = components["schemas"]["CostReport"];
export type CostFigures = components["schemas"]["CostFigures"];
export type CostWindow = CostReport["window"];

export const COST_WINDOWS: { id: CostWindow; label: string }[] = [
  { id: "24h", label: "24h" },
  { id: "7d", label: "7 dias" },
  { id: "30d", label: "30 dias" },
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

/** The estimate for a group: "sem preço" when nothing could be priced but tokens exist,
 * "—" when there is nothing to price, and "parcial" when some runs were left out. */
export function costLabel(f: CostFigures, currency: string): string {
  if (f.estimated_cost_usd == null) {
    return f.unpriced_runs > 0 ? "sem preço" : "—";
  }
  return money(f.estimated_cost_usd, currency) + (f.unpriced_runs > 0 ? " (parcial)" : "");
}

/** Runs the engine did not measure, as a badge text; null when there are none. */
export function notReportedLabel(f: Pick<CostFigures, "not_reported_runs">): string | null {
  return f.not_reported_runs > 0 ? `${f.not_reported_runs} não reportado` : null;
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
  return p ?? "sem projeto";
}

/** Footer text: where the prices come from and what was left unpriced. */
export function pricingNote(p: CostReport["pricing"]): string {
  const unpriced = p.unpriced_models.length
    ? ` Modelos sem preço: ${p.unpriced_models.join(", ")}.`
    : "";
  return `Estimativa a partir de ${p.source} (${p.version}, de ${p.as_of}), em ${p.currency} por tokens × preço do modelo.${unpriced}`;
}
