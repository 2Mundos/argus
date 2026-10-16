import type { Instance, Pool, PoolCapability } from "./api";

export interface Gate {
  enabled: boolean;
  /** Why the button is off, or what is unverified; shown next to it. */
  reason?: string;
}

/** Whether "Create worker" may be pressed for a pool, from what the node reported. */
export function launchGate(
  pool: Pool | undefined,
  capability: PoolCapability | undefined,
  busy: boolean,
): Gate {
  if (!pool) return { enabled: false, reason: "No pool is defined; an admin creates one first." };
  if (busy) return { enabled: false, reason: "Launch request in flight…" };
  if (!capability) return { enabled: false, reason: "Checking what this node can run…" };
  if (!capability.launchable) return { enabled: false, reason: capability.blockers.join("; ") };
  if (capability.free_slots === 0) return { enabled: false, reason: "Pool is at its cap." };
  return { enabled: true };
}

/** Where a requested worker is, in words that never claim more than is known. */
export function launchStatus(pool: Pool, instances: Instance[]): string {
  const live = instances.filter((i) => i.pool === pool.name && i.state !== "terminated");
  const failing = live.find((i) => i.state === "reserved" && i.last_error);
  if (failing) return `failed to start (attempt ${failing.start_attempts}): ${failing.last_error}`;
  if (live.some((i) => i.state === "reserved")) return "starting…";
  if (pool.desired_replicas > live.length) return "requested, waiting for the reconciler…";
  if (pool.status === "unreachable") return `target unreachable: ${pool.last_error ?? ""}`;
  return "up to date";
}
