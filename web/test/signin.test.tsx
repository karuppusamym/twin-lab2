import { screen, waitFor } from "@testing-library/react";
import { HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { example, on, reply } from "./contract";
import { renderApp } from "./render";
import { server } from "./setup";

describe("sign-in", () => {
  it("shows the sign-in form while signed out, refuses a wrong password, then opens the catalog", async () => {
    const sent: { body: unknown; csrf: string | null }[] = [];
    server.use(on("signIn", async ({ request }) => {
      const body = (await request.json()) as { user: string; password: string };
      sent.push({ body, csrf: request.headers.get("X-CSRF-Token") });
      if (body.password !== "correct horse") {
        return HttpResponse.json(example("signIn", "wrong", "401") as never, { status: 401, headers: { "Content-Type": "application/problem+json" } });
      }
      server.use(reply("getSession", "owner"));
      return HttpResponse.json(example("signIn", "owner") as never);
    }));
    const { user } = renderApp("/apps/1", "signedOut");

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    await user.type(screen.getByLabelText("Your name"), "ana");
    await user.type(screen.getByLabelText("Password"), "wrong");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Wrong name or password.");

    await user.clear(screen.getByLabelText("Password"));
    await user.type(screen.getByLabelText("Password"), "correct horse");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    // the page the address pointed at opens once signed in
    expect(await screen.findByRole("heading", { level: 1, name: /Billing Desk/ })).toBeInTheDocument();
    expect(screen.getByText("ana", { selector: ".who" })).toBeInTheDocument();
    expect(sent).toHaveLength(2);
    expect(sent[1]).toEqual({ body: { user: "ana", password: "correct horse" }, csrf: "3c1f0e2d9a8b7c6d5e4f3a2b1c0d9e8f" });
  });

  it("signs out a local user", async () => {
    let signedOut = false;
    server.use(on("signOut", () => {
      signedOut = true;
      server.use(reply("getSession", "signedOut"));
      return new HttpResponse(null, { status: 204 });
    }));
    const { user } = renderApp("/", "owner");
    await user.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(signedOut).toBe(true);
  });

  it("has no sign-in or sign-out behind the OAuth proxy", async () => {
    renderApp("/", "viewer");
    expect(await screen.findByRole("heading", { level: 1, name: "Home" })).toBeInTheDocument();
    expect(screen.getByText("vic", { selector: ".who" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
  });

  it("asks for the organisation's sign-in when there is no local sign-in", async () => {
    server.use(on("getSession", () => HttpResponse.json({ ...example<Record<string, unknown>>("getSession", "signedOut"), auth_mode: "proxy", login_available: false } as never)));
    renderApp("/", "signedOut");
    server.use(on("getSession", () => HttpResponse.json({ ...example<Record<string, unknown>>("getSession", "signedOut"), auth_mode: "proxy", login_available: false } as never)));
    await waitFor(() => expect(screen.getByText(/organisation's sign-in/)).toBeInTheDocument());
    expect(screen.queryByRole("heading", { name: "Sign in" })).not.toBeInTheDocument();
  });
});
