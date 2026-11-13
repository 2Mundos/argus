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
  env_input_unavailable: "a required task input could not be downloaded",
  env_input_invalid: "a required task input failed its safety or integrity check",
  env_input_limit: "the task inputs exceed the worker's size or file limits",
  env_integration_unavailable:
    "a selected integration is revoked, expired, unvalidated or its credential is unavailable",
  env_integration_unsupported: "the worker's engine cannot use Kubernetes integrations",
  env_integration_invalid: "the worker could not install a selected integration safely",
  env_integration_gateway_intercepts:
    "TLS to a cluster is intercepted or does not match its CA, so its credential would be exposed",
  env_integration_route_unavailable:
    "the worker's Kubernetes route is not available (no docker bridge, or the cluster is unreachable)",
  env_guard_missing: "the worker image has no command guard hook for its engine",
  env_oom: "sessions on its pool keep running out of memory; raise the pool's memory or unblock",
  worker_env: "the worker reported a missing workspace, tool or credential",
  worker_gave_up: "the worker gave up; retry or cancel it",
  budget_exhausted: "its budget is spent; raise the budget or cancel it",
};

/** The sentence for a block reason: the mapped copy, the raw reason if unknown, nothing if unset. */
export function blockReasonText(reason: string | null | undefined): string | undefined {
  return reason ? (BLOCK_COPY[reason] ?? reason) : undefined;
}
