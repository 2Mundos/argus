export type GatewayFetchOptions = {
  fetchImpl?: typeof fetch;
  proxy?: string;
  requestTls?: Record<string, unknown>;
};

export function createGatewayFetch(options?: GatewayFetchOptions): typeof fetch;

export class GiteaApi {
  constructor(
    repositoryUrl: string,
    allowedRepositories: readonly string[],
    fetchImpl?: typeof fetch,
  );
  getRepository(): Promise<unknown>;
  getPullRequest(number: number): Promise<{
    number: number;
    state?: string;
    merged?: boolean;
    head?: { sha?: string };
    html_url?: string;
  }>;
}
