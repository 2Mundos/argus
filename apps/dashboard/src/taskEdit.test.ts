import { describe, expect, it } from "vitest";
import { ApiError, type Task } from "./api";
import { buildChanges, errorsFrom, parseList, toDraft } from "./taskEdit";

const task = {
  id: "t1",
  title: "Old",
  spec: "s",
  acceptance: ["a"],
  priority: 1,
  required_capabilities: ["x", "y"],
  knowledge: [],
  depends_on: [],
  state: "ready",
  version: "v1",
  editable_fields: [
    "title",
    "spec",
    "acceptance",
    "priority",
    "required_capabilities",
    "knowledge",
    "depends_on",
    "account_providers",
    "harness_model",
    "executor_model",
    "harness_account",
  ],
  executor_model: { executor: "codex", model: "gpt-6.1-sol" },
} as unknown as Task;

describe("buildChanges", () => {
  it("sends nothing when the draft equals the task", () => {
    expect(buildChanges(task, toDraft(task))).toEqual({ changes: {}, errors: {} });
  });

  it("pins a harness account by id and clears it back to any account", () => {
    const pinned = { ...toDraft(task), harness_account: "acc-strata" };
    expect(buildChanges(task, pinned).changes).toEqual({ harness_account: "acc-strata" });
    const withPin = { ...task, harness_account: "acc-strata" } as Task;
    expect(toDraft(withPin).harness_account).toBe("acc-strata");
    const cleared = { ...toDraft(withPin), harness_account: "" };
    expect(buildChanges(withPin, cleared).changes).toEqual({ harness_account: null });
  });

  it("sends only changed fields and treats capabilities as a set", () => {
    const draft = { ...toDraft(task), title: " New ", required_capabilities: "y\nx" };
    expect(buildChanges(task, draft).changes).toEqual({ title: "New" });
    const more = { ...toDraft(task), priority: "5", spec: "", acceptance: "" };
    expect(buildChanges(task, more).changes).toEqual({ priority: 5, spec: "", acceptance: [] });
  });

  it("reports what the browser can tell is wrong", () => {
    const draft = { ...toDraft(task), title: "  ", priority: "1.5" };
    expect(Object.keys(buildChanges(task, draft).errors).sort()).toEqual(["priority", "title"]);
  });

  it("pins a model, clears a pin back to the policy and checks the model ID", () => {
    const draft = { ...toDraft(task), harness_model: "claude claude-opus-5-5", executor_model: "" };
    expect(buildChanges(task, draft).changes).toEqual({
      harness_model: { engine: "claude", model: "claude-opus-5-5" },
      executor_model: null,
    });
    const bad = { ...toDraft(task), harness_model: "claude " };
    expect(buildChanges(task, bad).errors).toEqual({ harness_model: "Enter a model ID." });
  });

  it("never sends a field the state does not allow", () => {
    const claimed = { ...task, state: "claimed", editable_fields: ["priority"] } as Task;
    const draft = { ...toDraft(claimed), title: "Other", priority: "9" };
    expect(buildChanges(claimed, draft)).toEqual({
      changes: { priority: 9 },
      errors: { title: "Not editable while the task is claimed." },
    });
  });
});

describe("errorsFrom", () => {
  it("maps per-field details, cycles and missing ids to their fields", () => {
    expect(errorsFrom(new ApiError(422, "validation_failed", "bad", ["title: too long"]))).toEqual({
      title: "too long",
    });
    expect(errorsFrom(new ApiError(422, "dependency_cycle", "cycle", ["a", "b", "a"]))).toEqual({
      depends_on: "Would create a dependency cycle: a → b → a",
    });
    expect(errorsFrom(new ApiError(404, "not_found", "gone", ["depends_on: ghost"]))).toEqual({
      depends_on: "Not found: ghost",
    });
    expect(errorsFrom(new ApiError(409, "task_not_editable", "nope", ["state: claimed"]))).toEqual({
      form: "nope",
    });
  });
});

it("parseList drops blanks", () => {
  expect(parseList(" a \n\n b\n")).toEqual(["a", "b"]);
});
