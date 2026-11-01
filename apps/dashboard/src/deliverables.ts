import type { Task } from "./api";

export type Deliverable = Task["deliverable"];

/** What a task must leave behind (task deliverable spec 3.1), in display order. */
export const DELIVERABLES: { value: Deliverable; label: string }[] = [
  { value: "response", label: "A response (summary)" },
  { value: "knowledge", label: "Knowledge (a page written)" },
  { value: "artifact", label: "An artifact (a file in the result)" },
  { value: "subtasks", label: "Subtasks (at least one child)" },
  { value: "branch", label: "A pushed branch" },
  { value: "pr_open", label: "A pull request opened" },
  { value: "pr_green", label: "A pull request open with passing checks" },
  { value: "pr_merged", label: "A pull request merged" },
];

/** Branch and PR deliverables need the repository capability (spec 3.3). */
export const CODE_DELIVERABLES: Deliverable[] = ["branch", "pr_open", "pr_green", "pr_merged"];

export function deliverableLabel(value: Deliverable | undefined): string {
  return DELIVERABLES.find((d) => d.value === value)?.label ?? "A response (summary)";
}
