import { useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import { ApiError, api, call, type Task } from "../api";
import { BUDGET_MAX_USD, parseBudget } from "../taskEdit";

/**
 * What to do about a task blocked as `budget_exhausted` (task cost spec 3.4): raise
 * its budget, which continues it once the estimate is below the new figure, or cancel
 * it. Unblock alone is refused while the budget is spent.
 */
export function BudgetBlockAction({
  task,
  onChanged,
}: {
  task: Pick<Task, "id" | "version" | "budget_usd" | "progress_message">;
  onChanged?: () => void;
}) {
  const suggested = task.budget_usd ? Math.min(task.budget_usd * 2, BUDGET_MAX_USD) : "";
  const [text, setText] = useState(String(suggested));
  const [done, setDone] = useState<string>();
  const [error, setError] = useState<string>();
  const action = useAction();
  const path = { task_id: task.id };

  async function raise() {
    setError(undefined);
    const budget = parseBudget(text);
    if ("error" in budget) return setError(budget.error);
    await action.run(async () => {
      try {
        const saved = await call(
          api.PATCH("/api/tasks/{task_id}", {
            params: { path },
            body: { expected_version: task.version, changes: { budget_usd: budget.value } },
          }),
        );
        onChanged?.();
        // Still blocked: the estimate is above the new figure too, so the form stays.
        if (saved.state === "blocked")
          setError("Budget saved, but the task has already spent more: raise it further.");
        else setDone("Budget raised: a worker can claim the task again.");
      } catch (err) {
        setError(
          err instanceof ApiError && err.code === "stale_version"
            ? "The task changed meanwhile; open it to see where it stands."
            : (err as Error).message,
        );
      }
    }, "raise");
  }

  async function cancel() {
    setError(undefined);
    await action.run(async () => {
      try {
        await call(api.POST("/api/tasks/{task_id}/cancel", { params: { path } }));
        setDone("Cancelled.");
        onChanged?.();
      } catch (err) {
        setError((err as Error).message);
      }
    }, "cancel");
  }

  if (done) {
    return (
      <p className="small" role="status">
        {done}
      </p>
    );
  }
  const id = `budget-${task.id}`;
  return (
    <div className="budget-block-action">
      {task.progress_message && (
        // The block message carries the figures: "budget of $X is spent (estimate $Y)…"
        <p className="small">
          {task.progress_message.charAt(0).toUpperCase() + task.progress_message.slice(1)}.
        </p>
      )}
      <label htmlFor={id} className="small">
        New budget (USD; empty: no budget)
      </label>
      <input
        id={id}
        type="number"
        min={0}
        step="any"
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="actions left">
        <ActionButton
          className="primary"
          pending={action.is("raise")}
          disabled={action.busy}
          action={raise}
        >
          Raise budget
        </ActionButton>
        <ActionButton
          className="danger"
          pending={action.is("cancel")}
          disabled={action.busy}
          action={cancel}
        >
          Cancel task
        </ActionButton>
      </div>
    </div>
  );
}
