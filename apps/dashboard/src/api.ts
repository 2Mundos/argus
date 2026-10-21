import { type components, createArgusClient } from "@argus/api-client";

export type Task = components["schemas"]["Task"];
export type TaskSummary = components["schemas"]["TaskSummary"];
export type TaskState = components["schemas"]["TaskState"];
export type Agent = components["schemas"]["Agent"];
export type Decision = components["schemas"]["Decision"];
export type DecisionDetail = components["schemas"]["DecisionDetail"];
export type ArgusEvent = components["schemas"]["EventOut"];
export type Artifact = components["schemas"]["Artifact"];
export type TaskKind = components["schemas"]["TaskKind"];
export type Project = components["schemas"]["Project"];
export type Page = components["schemas"]["Page"];
export type PageListItem = components["schemas"]["PageListItem"];
export type Revision = components["schemas"]["Revision"];
export type Proposal = components["schemas"]["Proposal"];
export type SearchHit = components["schemas"]["SearchHit"];
export type Account = components["schemas"]["Account"];
export type ClaudeLogin = components["schemas"]["ClaudeLogin"];
export type Pool = components["schemas"]["Pool"];
export type SystemSummary = components["schemas"]["SystemSummary"];
export type Instance = components["schemas"]["Instance"];
export type FleetCapabilities = components["schemas"]["FleetCapabilities"];
export type FleetHealth = components["schemas"]["FleetHealth"];
export type Overview = components["schemas"]["Overview"];
export type EngineCatalog = components["schemas"]["EngineCatalog"];
export type ModelPolicy = components["schemas"]["ModelPolicy"];
export type TemplateCatalog = components["schemas"]["TemplateCatalog"];
export type WorkerTemplate = components["schemas"]["WorkerTemplate"];
export type CreateWorkerRequest = components["schemas"]["CreateWorkerRequest"];
export type PoolCapability = components["schemas"]["PoolCapability"];

const TOKEN_KEY = "argus.token";

export function loadToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function saveToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    // Storage unavailable (private mode): the token lives for this page only.
  }
}

let currentToken = loadToken();
let onUnauthorized: () => void = () => {};

export function setToken(token: string | null): void {
  currentToken = token;
  saveToken(token);
}

export function handleUnauthorized(callback: () => void): void {
  onUnauthorized = callback;
}

export const api = createArgusClient({ token: () => currentToken });

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details: string[] = [],
  ) {
    super(message);
  }
}

interface ErrorBody {
  detail?: { code?: string; message?: string; details?: string[] } | string;
}

/** Unwrap an openapi-fetch result: return data or throw an ApiError. */
export async function call<T>(
  request: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await request;
  if (response.status === 401) onUnauthorized();
  if (error !== undefined || !response.ok) {
    const detail = (error as ErrorBody | undefined)?.detail;
    if (typeof detail === "object" && detail !== null) {
      throw new ApiError(
        response.status,
        detail.code ?? "error",
        detail.message ?? response.statusText,
        detail.details,
      );
    }
    throw new ApiError(response.status, "error", String(detail ?? response.statusText));
  }
  return data as T;
}

/**
 * The newest tasks, following the API's cursor for up to `pages` pages of 100.
 * Archived tasks are left out unless `archived` asks for exactly those.
 */
export async function newestTasks(
  pages = 5,
  archived = false,
  opts?: {
    signal?: AbortSignal;
    onPage?: (page: { items: TaskSummary[]; total: number }) => void;
  },
): Promise<{ items: TaskSummary[]; total: number }> {
  const items: TaskSummary[] = [];
  let cursor: string | undefined;
  let total = 0;
  for (let n = 0; n < pages; n++) {
    const page = await call(
      api.GET("/api/tasks", {
        params: { query: { limit: 100, cursor, archived } },
        signal: opts?.signal,
      }),
    );
    items.push(...page.items);
    total = page.total;
    opts?.onPage?.({ items: items.slice(), total });
    if (!page.next_cursor) break;
    cursor = page.next_cursor;
  }
  return { items, total };
}

/** Fetch an authenticated file (artifacts) and trigger a download. */
export async function download(path: string, filename: string): Promise<void> {
  const res = await fetch(path, { headers: { Authorization: `Bearer ${currentToken}` } });
  if (!res.ok) throw new ApiError(res.status, "error", res.statusText);
  const url = URL.createObjectURL(await res.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: filename });
  a.click();
  URL.revokeObjectURL(url);
}

/** GET a plain-text endpoint with auth (e.g. container logs). */
export async function fetchText(path: string): Promise<string> {
  const res = await fetch(path, { headers: { Authorization: `Bearer ${currentToken}` } });
  if (res.status === 401) onUnauthorized();
  if (!res.ok) throw new ApiError(res.status, "error", await res.text());
  return res.text();
}

export function token(): string | null {
  return currentToken;
}
