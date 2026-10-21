export interface BridgeSettings {
  base_url: string;
  label?: string | null;
  model?: string | null;
  provider?: Record<string, unknown> | null;
}
export interface AnthropicOpenAIBridge {
  port: number;
  baseUrl: string;
  close(): Promise<void>;
}
export declare function startAnthropicOpenAIBridge(opts: {
  settings: BridgeSettings;
  apiKey: string;
  listenHost?: string;
  listenPort?: number;
  bridgeKey?: string | null;
  fetchImpl?: typeof fetch;
}): Promise<AnthropicOpenAIBridge>;
export declare const BRIDGE_KEY_HEADER: string;
export declare function bridgeKeyMatches(presented: unknown, expected: string | null): boolean;
