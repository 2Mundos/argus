import type { components } from "@argus/api-client";
import { api, call } from "./api";
import { DELIVERABLES, deliverableLabel } from "./deliverables";
import { DIFFICULTY_LEVELS } from "./settings";

/** Task defaults: settings that fill what a new task leaves out (task-defaults.spec.md). */
export type TaskDefaults = components["schemas"]["TaskDefaults"];
export type TaskDefaultValues = TaskDefaults["values"];
export type TaskDefaultSource = TaskDefaults["sources"]["deliverable"];
export type TaskDefaultProblem = TaskDefaults["problems"][number];

export const TASK_DEFAULT_FIELDS = [
  "deliverable",
  "priority",
  "difficulty",
  "required_capabilities",
  "budget_usd",
  "max_attempts",
  "timeout_s",
] as const;
export type TaskDefaultField = (typeof TASK_DEFAULT_FIELDS)[number];

export const settingKey = (field: TaskDefaultField) => `task_default_${field}`;

export const FIELD_LABELS: Record<TaskDefaultField, string> = {
  deliverable: "Deliverable",
  priority: "Priority",
  difficulty: "Difficulty",
  required_capabilities: "Required capabilities",
  budget_usd: "Budget (USD)",
  max_attempts: "Max attempts",
  timeout_s: "Timeout (seconds)",
};

/** The registry default of each key: what a field is with no stored value anywhere. */
export const BUILT_IN: Record<TaskDefaultField, unknown> = {
  deliverable: "response",
  priority: 0,
  difficulty: null,
  required_capabilities: [],
  budget_usd: null,
  max_attempts: 3,
  timeout_s: null,
};

/** What an empty input means for the fields where empty is a value (3.1). */
export const EMPTY_MEANS: Partial<Record<TaskDefaultField, string>> = {
  difficulty: "inferred from the title and spec",
  required_capabilities: "none",
  budget_usd: "no budget",
  timeout_s: "the kind's, then the server's",
};

export function fetchTaskDefaults(slug: string, kind: string, signal?: AbortSignal) {
  return call(
    api.GET("/api/projects/{slug}/task-defaults", {
      params: { path: { slug }, query: { kind } },
      signal,
    }),
  );
}

/** A stored or effective value as the text of its input. */
export function toText(field: TaskDefaultField, value: unknown): string {
  if (value === null || value === undefined) return "";
  if (field === "required_capabilities") return (value as string[]).join(", ");
  return String(value);
}

export function splitCapabilities(text: string): string[] {
  return text
    .split(",")
    .map((c) => c.trim())
    .filter(Boolean);
}

function integer(text: string, min: number, max: number): { value: number } | { error: string } {
  const t = text.trim();
  if (!/^-?\d+$/.test(t)) return { error: "Enter a whole number." };
  const n = Number(t);
  if (n < min || n > max) return { error: `Enter a number from ${min} to ${max}.` };
  return { value: n };
}

/** The value an input's text stands for, checked as the settings registry checks it. */
export function parseText(
  field: TaskDefaultField,
  text: string,
): { value: unknown } | { error: string } {
  const t = text.trim();
  switch (field) {
    case "deliverable":
      return DELIVERABLES.some((d) => d.value === t)
        ? { value: t }
        : { error: "Choose a deliverable." };
    case "priority":
      return integer(t, -1_000_000, 1_000_000);
    case "difficulty":
      if (t === "") return { value: null };
      return (DIFFICULTY_LEVELS as string[]).includes(t)
        ? { value: t }
        : { error: "Choose a difficulty." };
    case "required_capabilities": {
      const list = splitCapabilities(t);
      if (new Set(list).size !== list.length) return { error: "Name each capability once." };
      if (list.length > 50) return { error: "At most 50 capabilities." };
      if (list.some((c) => c.length > 200))
        return { error: "A capability is at most 200 characters." };
      return { value: list };
    }
    case "budget_usd": {
      if (t === "") return { value: null };
      const n = Number(t);
      if (!Number.isFinite(n) || n <= 0 || n > 10_000) {
        return { error: "Enter an amount above 0 and at most 10000, or leave it empty." };
      }
      return { value: n };
    }
    case "max_attempts":
      return integer(t, 1, 20);
    case "timeout_s":
      return t === "" ? { value: null } : integer(t, 60, 7 * 24 * 3600);
  }
}

function duration(seconds: number): string {
  if (seconds % 3600 === 0) return `${seconds / 3600} h`;
  if (seconds % 60 === 0) return `${seconds / 60} min`;
  return `${seconds} s`;
}

/** A value for reading, with the fallback a null stands for. */
export function formatValue(field: TaskDefaultField, value: unknown): string {
  if (value === null || value === undefined) return EMPTY_MEANS[field] ?? "unset";
  switch (field) {
    case "deliverable":
      return deliverableLabel(value as TaskDefaultValues["deliverable"]);
    case "required_capabilities":
      return (value as string[]).length ? (value as string[]).join(", ") : "none";
    case "budget_usd":
      return `$${value}`;
    case "timeout_s":
      return duration(value as number);
    default:
      return String(value);
  }
}

export function sourceText(source: TaskDefaultSource): string {
  if (source === "project") return "from project";
  if (source === "global") return "from global";
  if (source === "kind") return "from the kind";
  if (source === "inferred") return "inferred";
  return "built-in";
}

export const sameValue = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
