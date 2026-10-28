import type { GiteaApi, GiteaApiOptions } from "./gitea-api.mjs";

/** `github` for a github.com repository URL, `gitea` for any other host. */
export function repositoryProvider(repositoryUrl: string): "github" | "gitea";

/** GitHubApi on github.com, GiteaApi elsewhere; both through the OneCLI gateway by default. */
export function repositoryClient(
  repositoryUrl: string,
  allowedRepositories: readonly string[],
  fetchImpl?: typeof fetch,
  options?: GiteaApiOptions,
): GiteaApi;

/** GiteaApi's surface over the GitHub REST API; refuses repositories not on github.com. */
export class GitHubApi extends GiteaApi {}

export function main(
  argv: readonly string[],
  env?: Record<string, string | undefined>,
  io?: Parameters<typeof import("./gitea-api.mjs").main>[2],
): Promise<number>;
