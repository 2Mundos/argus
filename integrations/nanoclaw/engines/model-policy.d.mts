export interface ModelPolicy {
  harness: Record<string, string[]>;
  implementation: Record<string, string[]>;
  trivial?: Record<string, string[]>;
}
export interface PolicySnapshot {
  policy: ModelPolicy;
  revision: string;
  context: {
    task_id: string;
    attempt: number;
    /** The harness the leased account selected, and that account's provider. */
    engine?: string;
    account_provider?: string | null;
    executor_account_provider?: string | null;
    /** Leased account ids for usage reports (the executor falls back to the engine's account). */
    engine_account_id?: string | null;
    executor_account_id?: string | null;
    /** A custom (OpenRouter) engine lease's settings (base URL, model; no secret). */
    engine_settings?: object | null;
    executor_settings?: object | null;
    /** The delegate CLI of the best-priority leased account (claude | codex | agent). */
    preferred_executor?: string | null;
    /** Task complexity (trivial | standard | hard); trivial prefers the local model. */
    complexity?: string | null;
    /** The task's pinned executor and model (task models spec); null follows the policy. */
    executor_model?: { executor: string; model: string } | null;
  } | null;
}
export interface ModelSelection {
  scope: string;
  policy_engine: string;
  requested_model: string | null;
  configured_model: string | null;
  resolved_model: null;
  policy_revision: string;
  model_attempt: number;
  fallback_reason: string | null;
  has_next: boolean;
}
export function validatePolicy(raw: unknown): ModelPolicy;
export function policySnapshot(raw: unknown, context?: PolicySnapshot["context"]): PolicySnapshot;
export function loadPolicy(e?: NodeJS.ProcessEnv, snapshotFile?: string): PolicySnapshot;
export function selectModel(
  snapshot: PolicySnapshot,
  scope: "harness" | "implementation",
  engine: string,
  attempt?: number,
  reason?: string | null,
  override?: string,
): ModelSelection;
export function selectHarnessModel(
  snapshot: PolicySnapshot,
  engine: string,
  task: {
    spec: string;
    failures?: number;
    harness_model?: { engine: string; model: string } | null;
  },
): ModelSelection;
