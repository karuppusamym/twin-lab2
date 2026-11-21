// The API contract (api/openapi.yaml) as test fixtures: every operation, its examples, and MSW
// handlers that answer each /api/v1 operation with its first example.
import { readFileSync } from "node:fs";
import nodePath from "node:path";
import { http, HttpResponse, type HttpResponseResolver } from "msw";
import { parse } from "yaml";

export const SPEC_PATH = nodePath.resolve(__dirname, "../../api/openapi.yaml");

type Json = unknown;
interface Media { examples?: Record<string, { $ref?: string; value?: Json }>; schema?: Json }
interface Response { content?: Record<string, Media>; $ref?: string }
interface Operation { operationId: string; responses: Record<string, Response>; requestBody?: { content?: Record<string, Media> } }

const METHODS = ["get", "post", "put", "patch", "delete"] as const;
export type Method = (typeof METHODS)[number];

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const spec: any = parse(readFileSync(SPEC_PATH, "utf8"), { merge: true, maxAliasCount: -1 });

export function resolve<T = Json>(node: T): T {
  let current: unknown = node;
  while (current && typeof current === "object" && "$ref" in current) {
    const ref = (current as { $ref: string }).$ref;
    current = ref.replace(/^#\//, "").split("/").reduce((o: Record<string, unknown>, k) => o[k] as Record<string, unknown>, spec);
  }
  return current as T;
}

export interface Op { method: Method; path: string; operation: Operation }

export const operations: Op[] = Object.entries(spec.paths as Record<string, Record<string, Operation>>).flatMap(([path, item]) =>
  METHODS.filter((m) => item[m]).map((method) => ({ method, path, operation: item[method] as Operation })),
);

export function op(operationId: string): Op {
  const found = operations.find((o) => o.operation.operationId === operationId);
  if (!found) throw new Error(`no operation ${operationId}`);
  return found;
}

/** The examples of an operation's response: status → name → value. */
export function responseExamples(o: Op): { status: string; media: string; examples: Record<string, Json> }[] {
  return Object.entries(o.operation.responses).flatMap(([status, raw]) => {
    const response = resolve(raw);
    return Object.entries(response.content ?? {}).map(([media, m]) => ({
      status, media,
      examples: Object.fromEntries(Object.entries(m.examples ?? {}).map(([name, e]) => [name, resolve(e).value])),
    }));
  });
}

/** One response example of an operation: by name, else the first example of its first 2xx JSON answer. */
export function example<T = Json>(operationId: string, name?: string, status?: string): T {
  const all = responseExamples(op(operationId)).filter((r) => (status ? r.status === status : r.status.startsWith("2")));
  for (const r of all) {
    const names = Object.keys(r.examples);
    const chosen = name ?? names[0];
    if (chosen && chosen in r.examples) return structuredClone(r.examples[chosen]) as T;
  }
  throw new Error(`no example ${name ?? "(first)"} for ${operationId} ${status ?? "2xx"}`);
}

/** The MSW path of an OpenAPI path: /api/v1/apps/{app_id} → */
export function mswPath(path: string) {
  return `*${path.replace(/\{([^}]+)\}/g, ":$1")}`;
}

function successOf(o: Op) {
  const statuses = Object.keys(o.operation.responses).filter((s) => s.startsWith("2")).sort();
  for (const s of statuses) {
    const response = resolve(o.operation.responses[s] as Response);
    const json = response.content?.["application/json"];
    if (json) {
      const first = Object.values(json.examples ?? {})[0];
      return { status: Number(s), body: first ? resolve(first).value : {} };
    }
    if (!response.content) return { status: Number(s), body: null };
  }
  return null;
}

/** Answers `operationId` with `resolver` (an override for one test). */
export function on(operationId: string, resolver: HttpResponseResolver) {
  const o = op(operationId);
  return http[o.method](mswPath(o.path), resolver);
}

/** Answers `operationId` with one of its examples. */
export function reply(operationId: string, name?: string, status?: string) {
  const o = op(operationId);
  const code = status ? Number(status) : successOf(o)?.status ?? 200;
  const body = example(operationId, name, status);
  return http[o.method](mswPath(o.path), () => HttpResponse.json(body as never, {
    status: code, headers: code >= 400 ? { "Content-Type": "application/problem+json" } : undefined,
  }));
}

/** Default handlers: every /api/v1 operation answers with its first success example. */
export function contractHandlers() {
  return operations
    .filter((o) => o.path.startsWith("/api/v1/"))
    .flatMap((o) => {
      const success = successOf(o);
      if (!success) return [];
      return [http[o.method](mswPath(o.path), () => (success.body === null
        ? new HttpResponse(null, { status: success.status })
        : HttpResponse.json(structuredClone(success.body) as never, { status: success.status })))];
    });
}
