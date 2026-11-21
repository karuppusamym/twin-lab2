// The typed API client: relative /api/v1 URLs on the same origin, the session cookie, and the
// CSRF header on every request that changes something. Types come from api/openapi.yaml
// (npm run gen → src/api/schema.ts).
import createClient, { type Middleware } from "openapi-fetch";
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];
export type Problem = Schemas["Problem"];
export type Role = Schemas["Role"];
export type Session = Schemas["Session"];

let csrfToken = "";

/** The session's form token, sent as X-CSRF-Token (set whenever the session is read). */
export function setCsrfToken(token: string) {
  csrfToken = token;
}

const SAFE = new Set(["GET", "HEAD", "OPTIONS"]);

/** An error answer of the API: always the problem+json shape. */
export class ApiError extends Error {
  readonly problem: Problem;
  constructor(problem: Problem) {
    super(problem.detail || problem.title);
    this.name = "ApiError";
    this.problem = problem;
  }
  get status() {
    return this.problem.status;
  }
}

async function problemOf(response: Response): Promise<Problem> {
  try {
    const body: unknown = await response.clone().json();
    if (body && typeof body === "object" && "status" in body && "title" in body) {
      const p = body as Problem;
      return { ...p, detail: p.detail || p.title };
    }
  } catch {
    // not JSON: fall through
  }
  return {
    type: "about:blank",
    title: response.statusText || "Error",
    status: response.status,
    detail: `The portal answered ${response.status} ${response.statusText}.`.trim(),
  };
}

const session: Middleware = {
  onRequest({ request }) {
    if (!SAFE.has(request.method) && csrfToken) request.headers.set("X-CSRF-Token", csrfToken);
    request.headers.set("Accept", "application/json");
    return request;
  },
  async onResponse({ response }) {
    if (!response.ok) throw new ApiError(await problemOf(response));
    return response;
  },
};

function origin() {
  return typeof window === "undefined" ? "http://localhost" : window.location.origin;
}

// fetch is looked up per call (tests replace it after this module loads)
export const api = createClient<paths>({ baseUrl: origin(), credentials: "same-origin", fetch: (request) => globalThis.fetch(request) });
api.use(session);

/** The data of an openapi-fetch answer (errors were already thrown by the middleware). */
export function data<T>(result: { data?: T; error?: unknown }): T {
  if (result.data === undefined) throw new ApiError({ type: "about:blank", title: "Empty", status: 500, detail: "The portal sent no data." });
  return result.data;
}

/** multipart/form-data requests (file uploads), with the same headers and errors. */
export async function postForm<T>(path: string, form: FormData): Promise<T> {
  const headers = new Headers({ Accept: "application/json" });
  if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
  const response = await fetch(new URL(path, origin()), { method: "POST", body: form, headers, credentials: "same-origin" });
  if (!response.ok) throw new ApiError(await problemOf(response));
  return (await response.json()) as T;
}

/** A download link: a plain same-origin link, so the cookie goes with it. */
export function downloadUrl(path: string) {
  return path;
}

/** The sentence to show for any error. */
export function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.problem.detail;
  if (error instanceof Error) return error.message;
  return "Something went wrong.";
}

/** A new idempotency key for a form (one per rendered form). */
export function newRequestKey() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}
