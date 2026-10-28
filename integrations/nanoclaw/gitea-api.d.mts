export type GatewayFetchOptions = {
  fetchImpl?: typeof fetch;
  proxy?: string;
  requestTls?: Record<string, unknown>;
};

export function createGatewayFetch(options?: GatewayFetchOptions): typeof fetch;

export type GiteaErrorCode =
  | "internal_error"
  | "usage"
  | "unauthorized"
  | "forbidden"
  | "not_found"
  | "conflict"
  | "validation_failed"
  | "rate_limited"
  | "server_error"
  | "network_error"
  | "http_error"
  | "limit_exceeded"
  | "policy_denied"
  | "ambiguous"
  | "bad_response";

/** Process exit code per error code; 0 is success. */
export const EXIT_CODES: Readonly<Record<GiteaErrorCode, number>>;

export class GiteaError extends Error {
  code: GiteaErrorCode;
  status: number | null;
  method: string | null;
  path: string | null;
}

export type GiteaApiOptions = {
  timeoutMs?: number;
  maxRetries?: number;
  maxRetryDelayMs?: number;
  sleep?: (ms: number) => Promise<unknown>;
  random?: () => number;
  now?: () => number;
};

export type PageOptions = {
  page?: number;
  limit?: number;
  all?: boolean;
  maxItems?: number;
  maxPages?: number;
};

export type Page<T> = {
  page: number;
  limit: number;
  pages_fetched: number;
  total_count: number | null;
  has_more: boolean;
  next_page: number | null;
  truncated: boolean;
  items: T[];
};

export type ReviewFindings = {
  pr_number: number;
  head_sha: string;
  comment_id: number | null;
  comment_head_sha: string | null;
  matches_head: boolean;
  /** Declared count from the comment for this exact head; null when unknown (see reason). */
  finding_count: number | null;
  body: string | null;
  status: {
    state: string | null;
    context: string | null;
    description: string | null;
    target_url: string | null;
  } | null;
  reason: string | null;
};

type Json = Record<string, unknown>;

export class GiteaApi {
  constructor(
    repositoryUrl: string,
    allowedRepositories: readonly string[],
    fetchImpl?: typeof fetch,
    options?: GiteaApiOptions,
  );
  /** Repository JSON from Gitea (not a `{pull, reconciled}` wrapper). */
  getRepository(): Promise<unknown>;
  request(path: string, init?: { method?: string; body?: unknown }): Promise<unknown>;
  paginate(
    path: string,
    query?: Record<string, string>,
    options?: PageOptions & { key?: string },
  ): Promise<Page<Json>>;
  getPullRequest(number: number): Promise<{
    number: number;
    state?: string;
    merged?: boolean;
    head?: { sha?: string };
    html_url?: string;
  }>;
  /**
   * Legacy return: the pull-request object itself (not `{pull, reconciled}`).
   * Ambiguous mutator failures still reconcile via GET readback internally.
   */
  updatePullRequest(
    number: number,
    input: { title?: string; body?: string; state?: string; base?: string },
  ): Promise<{
    number: number;
    title?: string;
    body?: string;
    state?: string;
    base?: { ref?: string };
    head?: { sha?: string };
    html_url?: string;
  }>;
  getPullRequestStatus(
    number: number,
  ): Promise<{ number: number; head_sha: string; statuses: unknown }>;
  listStatuses(
    sha: string,
    options?: PageOptions & { state?: string; sort?: string },
  ): Promise<Page<Json> & { sha: string; combined_state: string | null }>;
  reviewFindings(number: number, options?: { author?: string }): Promise<ReviewFindings>;
  /** Status-shaped CI entries for a commit (GitHub adds its check runs). */
  ciStatuses(sha: string): Promise<unknown>;
}

export function parseReviewComment(body: string): {
  head_sha: string | null;
  finding_count: number | null;
};
export function isAgenticReviewComment(body: string): boolean;
export function sanitizeMessage(text: string, max?: number): string;
export function main(
  argv: readonly string[],
  env?: Record<string, string | undefined>,
  io?: {
    stdout?: { write(chunk: string): unknown };
    stderr?: { write(chunk: string): unknown };
    readStdin?: () => Uint8Array;
    fetch?: typeof fetch;
    sleep?: (ms: number) => Promise<unknown>;
    random?: () => number;
    now?: () => number;
    cwd?: string;
  },
): Promise<number>;
