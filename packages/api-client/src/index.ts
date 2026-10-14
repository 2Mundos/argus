import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "./schema";

export type { components, paths } from "./schema";

export interface ArgusClientOptions {
  /** Origin of the Argus server; empty for same-origin. Paths already start with /api. */
  baseUrl?: string;
  /** Returns the bearer token to send, if any. Read on every request. */
  token?: () => string | null;
}

/** Typed client for the Argus API. Generated types: `make openapi`. */
export function createArgusClient({ baseUrl = "", token }: ArgusClientOptions = {}) {
  const client = createClient<paths>({ baseUrl });
  if (token) {
    const auth: Middleware = {
      onRequest({ request }) {
        const value = token();
        if (value) request.headers.set("Authorization", `Bearer ${value}`);
        return request;
      },
    };
    client.use(auth);
  }
  return client;
}

export type ArgusClient = ReturnType<typeof createArgusClient>;
