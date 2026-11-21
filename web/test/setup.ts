import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll } from "vitest";
import { contractHandlers } from "./contract";

export const server = setupServer(...contractHandlers());

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
  if (typeof window !== "undefined") window.history.pushState({}, "", "/");
});
afterAll(() => server.close());

// jsdom has no scrolling: the layout scrolls to the top of each new page
if (typeof window !== "undefined") window.scrollTo = () => undefined;
