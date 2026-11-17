export const OPENCODE_GATEWAY_SENTINEL: string;
export const OPENCODE_GATEWAY_SETTINGS: Readonly<{
  base_url: string;
  label: string;
  model: null;
  provider: null;
}>;
export const OPENCODE_GATEWAY_PATHS: string[];
export function openRouterExecutorSettings(settings: unknown): boolean;
export function opencodeGatewayProblem(
  executor: string,
  e: Record<string, string | undefined>,
  settings: unknown,
): string | null;
