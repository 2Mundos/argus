import { ApiError, type Task } from "./api";

export const EDIT_FIELDS = [
  "title",
  "spec",
  "acceptance",
  "priority",
  "required_capabilities",
  "knowledge",
  "depends_on",
] as const;
export type EditField = (typeof EDIT_FIELDS)[number];

/** The form's text view of a task: lists are one entry per line (ids and capabilities too). */
export type Draft = Record<EditField, string>;

export type Changes = {
  title?: string;
  spec?: string;
  acceptance?: string[];
  priority?: number;
  required_capabilities?: string[];
  knowledge?: string[];
  depends_on?: string[];
};

export type FieldErrors = Partial<Record<EditField | "form", string>>;

export function parseList(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

export function toDraft(task: Task): Draft {
  return {
    title: task.title,
    spec: task.spec,
    acceptance: task.acceptance.join("\n"),
    priority: String(task.priority),
    required_capabilities: task.required_capabilities.join("\n"),
    knowledge: task.knowledge.join("\n"),
    depends_on: task.depends_on.join("\n"),
  };
}

const same = (a: string[], b: string[]) => a.length === b.length && a.every((v, i) => v === b[i]);
const asSet = (a: string[]) => [...new Set(a)].sort();

/**
 * Only the fields the operator changed, against `base`; fields the task's state does not
 * allow are never sent. `errors` holds what the browser can already tell is wrong.
 */
export function buildChanges(base: Task, draft: Draft): { changes: Changes; errors: FieldErrors } {
  const changes: Changes = {};
  const errors: FieldErrors = {};
  const allowed = new Set<string>(base.editable_fields ?? []);
  const current = toDraft(base);
  for (const field of EDIT_FIELDS) {
    if (draft[field] === current[field]) continue;
    if (!allowed.has(field)) {
      // a re-based draft can hold a change the task's new state refuses: say so, never drop it
      errors[field] = `Not editable while the task is ${base.state}.`;
      continue;
    }
    const value = draft[field];
    if (field === "title") {
      const title = value.trim();
      if (!title) errors.title = "Title is required.";
      else if (title.length > 200) errors.title = "Title is at most 200 characters.";
      else if (title !== base.title) changes.title = title;
    } else if (field === "priority") {
      const n = Number(value);
      if (value.trim() === "" || !Number.isInteger(n))
        errors.priority = "Priority is a whole number.";
      else changes.priority = n;
    } else if (field === "spec") {
      changes.spec = value;
    } else {
      const list = parseList(value);
      const before = parseList(current[field]);
      const unchanged =
        field === "acceptance" ? same(list, before) : same(asSet(list), asSet(before));
      if (!unchanged) changes[field] = list;
    }
  }
  return { changes, errors };
}

/** Map an API refusal onto the form: `<field>: why` details, cycles and missing ids. */
export function errorsFrom(err: unknown): FieldErrors {
  if (!(err instanceof ApiError)) return { form: (err as Error).message };
  const out: FieldErrors = {};
  const fields = new Set<string>(EDIT_FIELDS);
  const add = (field: EditField | "form", why: string) => {
    out[field] = out[field] ? `${out[field]} ${why}` : why;
  };
  if (err.code === "dependency_cycle") {
    add("depends_on", `Would create a dependency cycle: ${err.details.join(" → ")}`);
    return out;
  }
  if (err.status === 404 && err.details.length > 0) {
    for (const d of err.details) {
      const [field = "", rest = ""] = d.split(": ", 2);
      if (fields.has(field)) add(field as EditField, `Not found: ${rest}`);
      else add("form", err.message);
    }
    return out;
  }
  for (const d of err.details) {
    const [field = "", ...rest] = d.split(": ");
    if (fields.has(field) && rest.length > 0) add(field as EditField, rest.join(": "));
  }
  if (Object.keys(out).length === 0) add("form", err.message);
  return out;
}
