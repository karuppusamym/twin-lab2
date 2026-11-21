import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App, makeQueryClient } from "../src/App";
import { reply } from "./contract";
import { server } from "./setup";

export type Who = "owner" | "builder" | "viewer" | "admin" | "signedOut";

/** Renders the whole app at `path`, signed in as `who` (a session example of the contract). */
export function renderApp(path: string, who: Who = "owner") {
  server.use(reply("getSession", who));
  window.history.pushState({}, "", path);
  const user = userEvent.setup();
  const client = makeQueryClient();
  return { user, client, ...render(<App client={client} />) };
}

/** Records the requests MSW sees: method and path. */
export function recordRequests() {
  const seen: { method: string; path: string; body?: unknown }[] = [];
  server.events.on("request:start", ({ request }) => {
    seen.push({ method: request.method, path: new URL(request.url).pathname });
  });
  return seen;
}

/** Chooses `option` in the searchable list labelled `label` (a Combobox): types part of it and clicks it. */
export async function choose(user: ReturnType<typeof userEvent.setup>, label: string, option: string) {
  const { screen } = await import("@testing-library/react");
  const box = await screen.findByRole("combobox", { name: label });
  await user.clear(box);
  await user.type(box, option.slice(0, 4));
  await user.click(await screen.findByRole("option", { name: option }));
}
