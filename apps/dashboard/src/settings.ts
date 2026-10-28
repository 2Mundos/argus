import { ApiError, type Project, token } from "./api";

export type SettingSource = "project" | "global" | "default" | "env" | "file";
export type ReviewMode = "off" | "different_model" | "different_provider";

export type SettingValue = {
  key: string;
  value: unknown;
  source: SettingSource;
  updated_at?: string | null;
  updated_by?: string | null;
  revision?: string | null;
  notice?: string | null;
};

export type SettingsResponse = {
  scope: string;
  settings: SettingValue[];
};

export type IndependentReview = {
  mode: ReviewMode;
  max_rounds: number;
};

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: {
      Authorization: `Bearer ${token()}`,
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
    signal,
  });
  if (res.status === 401) throw new ApiError(401, "invalid_token", "unauthorized");
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const detail = data?.detail;
    if (typeof detail === "object" && detail !== null) {
      throw new ApiError(
        res.status,
        detail.code ?? "error",
        detail.message ?? res.statusText,
        detail.details,
      );
    }
    throw new ApiError(res.status, "error", String(detail ?? res.statusText));
  }
  return data as T;
}

export const fetchProjects = (signal?: AbortSignal) =>
  request<Project[]>("GET", "/api/projects", undefined, signal);

export const fetchSettings = (project: string | null, signal?: AbortSignal) =>
  request<SettingsResponse>(
    "GET",
    project ? `/api/projects/${encodeURIComponent(project)}/settings` : "/api/settings",
    undefined,
    signal,
  );

export const fetchModelsOverride = (project: string, signal?: AbortSignal) =>
  request<{ key: string; value: unknown }>(
    "GET",
    `/api/projects/${encodeURIComponent(project)}/settings/models/override`,
    undefined,
    signal,
  );

export const putSetting = (key: string, value: unknown, project: string | null) =>
  request<SettingValue>(
    "PUT",
    project
      ? `/api/projects/${encodeURIComponent(project)}/settings/${encodeURIComponent(key)}`
      : `/api/settings/${encodeURIComponent(key)}`,
    { value },
  );

export const resetSetting = (key: string, project: string | null) =>
  request<SettingValue>(
    "DELETE",
    project
      ? `/api/projects/${encodeURIComponent(project)}/settings/${encodeURIComponent(key)}`
      : `/api/settings/${encodeURIComponent(key)}`,
  );

export const sourceLabel = (s: SettingSource) => {
  if (s === "project") return "project override";
  if (s === "global") return "global";
  if (s === "env") return "env (locked)";
  if (s === "file") return "file";
  return "default";
};
