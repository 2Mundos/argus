/** Why a worker blocked a task (failure classes 3.2-3.4); unknown reasons show as-is. */
export const BLOCK_COPY: Record<string, string> = {
  env_repository_unconfigured: "its project has no repository configured",
  env_repository_unavailable: "the repository credential could not be applied",
  env_repository_unreachable: "the repository did not answer",
  env_workspace_unconfigured: "the worker has no place for the task workspace",
  env_workspace_permission: "the worker cannot write the task workspace",
  env_workspace_invalid: "the task workspace is not a usable git repository",
  env_workspace_remote_mismatch: "the task workspace points at another repository",
  env_checkpoint_unavailable: "its saved work is not on this worker",
  env_checkpoint_invalid: "its saved work failed validation",
  env_checkpoint_conflict: "the workspace holds different work than the saved one",
  env_checkpoint_busy: "another worker holds the saved work",
  env_checkpoint_limit: "its saved work exceeds the size limit",
  env_workspace_branch_mismatch: "the task workspace is on another branch",
  env_workspace_limit: "the worker holds too many task workspaces",
  env_executor_auth: "the executor could not log in",
  env_workspace_missing: "its agent group directory is missing on the worker host",
  worker_env: "the worker reported a missing workspace, tool or credential",
  worker_gave_up: "the worker gave up; retry or cancel it",
};

/** The sentence for a block reason: the mapped copy, the raw reason if unknown, nothing if unset. */
export function blockReasonText(reason: string | null | undefined): string | undefined {
  return reason ? (BLOCK_COPY[reason] ?? reason) : undefined;
}
