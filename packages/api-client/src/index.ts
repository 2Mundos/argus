import createClient from "openapi-fetch";
import type { paths } from "./schema";

export type { components, paths } from "./schema";

/** Typed client for the Argus API. Generated types: `make openapi`. */
export const createArgusClient = (baseUrl = "/api") => createClient<paths>({ baseUrl });
