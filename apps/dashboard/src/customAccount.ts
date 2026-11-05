import type { components } from "@argus/api-client";

// Custom OpenAI-compatible accounts (docs/specs/custom-accounts.spec.md).

export type CustomConfig = components["schemas"]["CustomConfig"];

export type RoleForm = {
  model: string;
  order: string;
  only: string;
  quantizations: string;
  fallbacks: "default" | "allow" | "deny";
};

export type CustomForm = {
  baseUrl: string;
  label: string;
  engine: RoleForm;
  executor: RoleForm;
};

const OPENROUTER_URL = "https://openrouter.ai/api/v1";
const GOOGLE_AI_STUDIO_URL = "https://generativelanguage.googleapis.com/v1beta/openai";

export type CustomPresetId = "openrouter" | "google";

type CustomPreset = {
  id: CustomPresetId;
  name: string;
  label: string;
  baseUrl: string;
  note: string;
};

export const CUSTOM_PRESETS: CustomPreset[] = [
  {
    id: "openrouter",
    name: "OpenRouter",
    label: "openrouter",
    baseUrl: OPENROUTER_URL,
    note: "Any OpenAI-compatible endpoint. The key is stored encrypted as an Argus account and is never shown again.",
  },
  {
    id: "google",
    name: "Google AI Studio",
    label: "google",
    baseUrl: GOOGLE_AI_STUDIO_URL,
    note: "Google AI Studio's OpenAI-compatible endpoint. Create a key in AI Studio; the key is stored encrypted and is never shown again.",
  },
];

export function presetById(id: CustomPresetId): CustomPreset {
  return CUSTOM_PRESETS.find((p) => p.id === id) ?? CUSTOM_PRESETS[0]!;
}

const emptyRole = (): RoleForm => ({
  model: "",
  order: "",
  only: "",
  quantizations: "",
  fallbacks: "default",
});

export const formForPreset = (id: CustomPresetId): CustomForm => {
  const preset = presetById(id);
  return {
    baseUrl: preset.baseUrl,
    label: preset.label,
    engine: emptyRole(),
    executor: emptyRole(),
  };
};

export const emptyForm = (): CustomForm => formForPreset("openrouter");

const csv = (s: string) =>
  s
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);

function role(f: RoleForm, openrouter: boolean) {
  const options = {
    ...(csv(f.order).length > 0 && { order: csv(f.order) }),
    ...(csv(f.only).length > 0 && { only: csv(f.only) }),
    ...(csv(f.quantizations).length > 0 && { quantizations: csv(f.quantizations) }),
    ...(f.fallbacks !== "default" && { allow_fallbacks: f.fallbacks === "allow" }),
  };
  return {
    ...(f.model.trim() && { model: f.model.trim() }),
    ...(openrouter && Object.keys(options).length > 0 && { routing_options: options }),
  };
}

/** The `config` of an account create request. Routing options are OpenRouter-only. */
export function buildConfig(f: CustomForm): CustomConfig {
  const openrouter = f.label.trim() === "openrouter";
  return {
    base_url: f.baseUrl.trim(),
    label: f.label.trim(),
    engine: role(f.engine, openrouter),
    executor: role(f.executor, openrouter),
  };
}

/** Client-side check mirroring the API's URL rule; the API stays the authority. */
export function checkBaseUrl(url: string): string | null {
  let u: URL;
  try {
    u = new URL(url.trim());
  } catch {
    return "Enter an http(s) URL.";
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") return "Enter an http(s) URL.";
  if (u.username || u.password || u.search || u.hash) {
    return "The URL must not carry credentials or a query.";
  }
  return null;
}
