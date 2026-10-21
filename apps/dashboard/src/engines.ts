import { loadToken } from "./api";

export type EngineFact = {
  ok: boolean | null;
  detail: string;
};

export type EngineAxes = {
  model_provider: string;
  model: string | null;
  harness: string;
  executor: string | null;
  account_provider: string | null;
};

export type EngineEntry = {
  id: string;
  label: string;
  role: string;
  axes: EngineAxes;
  auth_methods: string[];
  version: string | null;
  readiness: {
    installed: EngineFact;
    authenticated: EngineFact;
    validated: EngineFact;
  };
  usable: boolean;
  notes: string;
};

export type EngineCatalog = {
  engines: EngineEntry[];
  local: {
    reachable: boolean;
    model: string | null;
    n_ctx: number | null;
    slots: number | null;
    error: string | null;
  } | null;
};

export async function fetchEngineCatalog(signal?: AbortSignal): Promise<EngineCatalog> {
  const token = loadToken();
  const res = await fetch("/api/engines", {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    signal,
  });
  if (!res.ok) {
    throw new Error(`engines catalogue: HTTP ${res.status}`);
  }
  return res.json() as Promise<EngineCatalog>;
}

export function readinessLabel(fact: EngineFact): string {
  if (fact.ok === true) return "yes";
  if (fact.ok === false) return "no";
  return "unknown";
}
