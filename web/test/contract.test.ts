// @vitest-environment node
// The contract itself: a valid OpenAPI 3.1 document whose examples (which the MSW handlers serve)
// match their schemas, every error is problem+json, and every /api/v1 operation states its role.
import SwaggerParser from "@apidevtools/swagger-parser";
import Ajv2020 from "ajv/dist/2020";
import addFormats from "ajv-formats";
import { describe, expect, it } from "vitest";
import { operations, resolve, responseExamples, spec } from "./contract";

describe("api/openapi.yaml", () => {
  it("is a valid OpenAPI 3.1 document", async () => {
    const api = await SwaggerParser.validate(structuredClone(spec));
    expect((api as { openapi?: string }).openapi).toBe("3.1.0");
  });

  it("gives every operation an id, a role rule and a tag", () => {
    for (const { method, path, operation } of operations) {
      const o = operation as unknown as Record<string, unknown>;
      expect(o.operationId, `${method} ${path}`).toBeTruthy();
      expect(o["x-twinlab-role"], `${method} ${path}`).toBeTruthy();
      expect((o.tags as string[]).length, `${method} ${path}`).toBeGreaterThan(0);
    }
  });

  it("keeps the agent API in its own tag, outside /api/v1", () => {
    for (const { path, operation } of operations) {
      const tags = (operation as unknown as { tags: string[] }).tags;
      expect(tags.includes("agent"), path).toBe(!path.startsWith("/api/v1/"));
    }
  });

  it("answers every /api/v1 error as problem+json", () => {
    for (const o of operations.filter((x) => x.path.startsWith("/api/v1/"))) {
      for (const [status, raw] of Object.entries(o.operation.responses)) {
        if (Number(status) < 400) continue;
        const content = resolve(raw).content ?? {};
        expect(Object.keys(content), `${o.operation.operationId} ${status}`).toEqual(["application/problem+json"]);
      }
    }
  });

  it("gives every JSON success of /api/v1 an example", () => {
    for (const o of operations.filter((x) => x.path.startsWith("/api/v1/"))) {
      for (const r of responseExamples(o)) {
        if (r.status.startsWith("2") && r.media === "application/json" && o.operation.operationId !== "downloadVerificationReport") {
          expect(Object.keys(r.examples).length, `${o.operation.operationId} ${r.status}`).toBeGreaterThan(0);
        }
      }
    }
  });

  it("has examples that match their schemas", async () => {
    const api = (await SwaggerParser.dereference(structuredClone(spec))) as typeof spec;
    const ajv = new Ajv2020({ strict: false, allErrors: true });
    addFormats(ajv);
    const problems: string[] = [];
    const check = (where: string, schema: object | undefined, value: unknown) => {
      if (!schema) return;
      const validate = ajv.compile(schema);
      if (!validate(value)) problems.push(`${where}: ${ajv.errorsText(validate.errors)}`);
    };
    for (const [path, item] of Object.entries(api.paths as Record<string, Record<string, Record<string, unknown>>>)) {
      for (const [method, operation] of Object.entries(item)) {
        if (typeof operation !== "object" || !operation || !("responses" in operation)) continue;
        const op = operation as { operationId: string; responses: Record<string, { content?: Record<string, { schema?: object; examples?: Record<string, { value: unknown }> }> }>; requestBody?: { content?: Record<string, { schema?: object; examples?: Record<string, { value: unknown }> }> } };
        for (const [status, response] of Object.entries(op.responses)) {
          for (const [media, m] of Object.entries(response.content ?? {})) {
            for (const [name, e] of Object.entries(m.examples ?? {})) check(`${method} ${path} ${status} ${media} ${name}`, m.schema, e.value);
          }
        }
        for (const [media, m] of Object.entries(op.requestBody?.content ?? {})) {
          for (const [name, e] of Object.entries(m.examples ?? {})) check(`${method} ${path} request ${media} ${name}`, m.schema, e.value);
        }
      }
    }
    expect(problems).toEqual([]);
  });
});
