import { type components, createArgusClient } from "@argus/api-client";

export type Task = components["schemas"]["Task"];
export type TaskState = components["schemas"]["TaskState"];
export type Agent = components["schemas"]["Agent"];
export type Decision = components["schemas"]["Decision"];
export type ArgusEvent = components["schemas"]["EventOut"];
export type Artifact = components["schemas"]["Artifact"];
export type TaskKind = components["schemas"]["TaskKind"];

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

/** Fetch an authenticated file (artifacts) and trigger a download. */
export async function download(path: string, filename: string): Promise<void> {
  const res = await fetch(path, { headers: { Authorization: `Bearer ${currentToken}` } });
  if (!res.ok) throw new ApiError(res.status, "error", res.statusText);
  const url = URL.createObjectURL(await res.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: filename });
  a.click();
  URL.revokeObjectURL(url);
}

export function token(): string | null {
  return currentToken;
}
