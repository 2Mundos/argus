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
  fetchImpl?: typeof fetch;
}): Promise<AnthropicOpenAIBridge>;
