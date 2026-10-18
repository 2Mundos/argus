export interface ModelPolicy {
  harness: Record<string, string[]>;
  implementation: Record<string, string[]>;
}
export interface PolicySnapshot {
  policy: ModelPolicy;
  revision: string;
  context: { task_id: string; attempt: number } | null;
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
  task: { spec: string; failures?: number },
): ModelSelection;
