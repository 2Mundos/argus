import type { ArgusEvent } from "./api";

/** Resource dependencies shared by the shell and onboarding dialogs. */
export const onboardingEvent = (event: ArgusEvent) =>
  ["account", "pool", "settings"].includes(event.subject_type);
export const workerEvent = (event: ArgusEvent) =>
  ["agent", "instance", "pool"].includes(event.subject_type) ||
  (event.subject_type === "task" &&
    !["task.progress", "task.model_execution", "task.model_selected"].includes(event.kind));
export const overviewEvent = (event: ArgusEvent) =>
  [
    "task",
    "agent",
    "instance",
    "pool",
    "account",
    "decision",
    "knowledge_page",
    "knowledge_proposal",
    "settings",
  ].includes(event.subject_type);
export const systemEvent = (event: ArgusEvent) =>
  ["system", "settings", "pool"].includes(event.subject_type);
