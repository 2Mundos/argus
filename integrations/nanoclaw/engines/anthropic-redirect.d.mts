export declare const LOCAL_HARNESS_AUTH_TOKEN: string;
export declare const LOCAL_ANTHROPIC_HOSTS: Set<string>;
export declare function isLocalAnthropicHost(hostname: string): boolean;
export declare function normalizeAnthropicBaseUrl(raw: unknown): string | null;
export declare function cloudHarnessCredentialPresent(
  e: Record<string, string | undefined>,
): boolean;
export declare function anthropicRedirectRejection(
  e: Record<string, string | undefined>,
  allowedBaseUrl?: string | null,
): string | null;
export declare const CUSTOM_HARNESS_AUTH_TOKEN: string;
