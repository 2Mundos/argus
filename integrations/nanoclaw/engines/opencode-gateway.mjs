/** Non-secret OpenRouter executor contract shared by host and delegate. */
export const OPENCODE_GATEWAY_SENTINEL = "argus-openrouter-executor-placeholder";
export const OPENCODE_GATEWAY_SETTINGS = Object.freeze({
  base_url: "https://openrouter.ai/api/v1",
  label: "openrouter",
  model: null,
  provider: null,
});
export const OPENCODE_GATEWAY_PATHS = ["/api/v1/models", "/api/v1/chat/completions"];

export function openRouterExecutorSettings(settings) {
  return (
    settings?.base_url === OPENCODE_GATEWAY_SETTINGS.base_url && settings?.label === "openrouter"
  );
}

/** A gateway sentinel must never authenticate against a different endpoint. */
export function opencodeGatewayProblem(executor, e, settings) {
  if (e.ARGUS_ACCOUNT_API_KEY !== OPENCODE_GATEWAY_SENTINEL) return null;
  if (executor !== "opencode" || !openRouterExecutorSettings(settings))
    return "OpenRouter gateway sentinel requires the leased OpenCode endpoint";
  return null;
}
