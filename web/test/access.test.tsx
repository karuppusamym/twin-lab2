// Access requests (a 403 about an application offers "Request access"; owners decide on Home)
// and the stale form token: a 403 `csrf` reads the session again and is sent once more.
import { screen, waitFor } from "@testing-library/react";
import { HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { example, on } from "./contract";
import { renderApp } from "./render";
import { server } from "./setup";

const PROBLEM = { "Content-Type": "application/problem+json" };

describe("request access", () => {
  it("names the owners and sends a role and a reason", async () => {
    const sent: unknown[] = [];
    server.use(
      on("getApp", () => HttpResponse.json(
        { type: "/errors/role", title: "Forbidden", status: 403, code: "role", app_id: 9, requested_role: "owner", owners: ["olivia"],
          detail: "This needs the owner role on this application. Ask an owner (olivia) for it." },
        { status: 403, headers: PROBLEM },
      )),
      on("listAccessRequests", () => HttpResponse.json({ mine: [], to_decide: [] })),
      on("requestAccess", async ({ request }) => { sent.push(await request.json()); return HttpResponse.json(example("requestAccess") as never, { status: 202 }); }),
    );
    const { user } = renderApp("/apps/9", "builder");
    expect(await screen.findByText("olivia")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Request access" }));
    expect(screen.getByLabelText("Role")).toHaveValue("owner");
    await user.type(screen.getByLabelText("Why you need it"), "I freeze the next package.");
    await user.click(screen.getByRole("button", { name: "Send the request" }));
    await waitFor(() => expect(sent).toEqual([{ role: "owner", reason: "I freeze the next package." }]));
    expect(await screen.findByText(/sent to the application's owners/)).toBeInTheDocument();
  });

  it("is not offered on other refusals", async () => {
    server.use(on("getApp", () => HttpResponse.json(
      { type: "/errors/forbidden", title: "Forbidden", status: 403, code: "forbidden", detail: "No." }, { status: 403, headers: PROBLEM },
    )));
    renderApp("/apps/9", "builder");
    expect(await screen.findByRole("alert")).toHaveTextContent("No.");
    expect(screen.queryByRole("button", { name: "Request access" })).not.toBeInTheDocument();
  });

  it("lets an owner approve a request from Home", async () => {
    const decisions: unknown[] = [];
    server.use(on("decideAccessRequest", async ({ request, params }) => {
      decisions.push({ id: params.request_id, ...(await request.json() as object) });
      return HttpResponse.json(example("decideAccessRequest") as never);
    }));
    const { user } = renderApp("/", "owner");
    const approve = await screen.findByRole("button", { name: "Approve bob (builder on Billing)" });
    await user.type(screen.getByLabelText("Note to bob (optional)"), "Welcome");
    await user.click(approve);
    await waitFor(() => expect(decisions).toEqual([{ id: "4", decision: "approve", note: "Welcome" }]));
  });
});

describe("a stale form token", () => {
  it("reads the session again and sends the change once more", async () => {
    const tokens: (string | null)[] = [];
    const { user } = renderApp("/machines", "owner");
    await screen.findByRole("button", { name: /pairing code/i });
    let reads = 0;
    server.use(
      on("getSession", () => {
        reads += 1;
        return HttpResponse.json({ ...(example("getSession", "owner") as object), csrf_token: "fresh" } as never);
      }),
      on("makePairingCode", ({ request }) => {
        const token = request.headers.get("X-CSRF-Token");
        tokens.push(token);
        if (token !== "fresh") {
          return HttpResponse.json({ type: "/errors/csrf", title: "Forbidden", status: 403, code: "csrf", detail: "The form token is out of date." },
            { status: 403, headers: PROBLEM });
        }
        return HttpResponse.json(example("makePairingCode") as never, { status: 201 });
      }),
    );
    await user.click(screen.getByRole("button", { name: /pairing code/i }));
    await waitFor(() => expect(tokens).toHaveLength(2));
    expect(tokens[0]).not.toBe("fresh");
    expect(tokens[1]).toBe("fresh");
    expect(reads).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText("The form token is out of date.")).not.toBeInTheDocument();
  });

  it("shows the refusal when the new token is refused too", async () => {
    const { user } = renderApp("/machines", "owner");
    await screen.findByRole("button", { name: /pairing code/i });
    let posts = 0;
    server.use(on("makePairingCode", () => {
      posts += 1;
      return HttpResponse.json({ type: "/errors/csrf", title: "Forbidden", status: 403, code: "csrf", detail: "The form token is out of date." },
        { status: 403, headers: PROBLEM });
    }));
    await user.click(screen.getByRole("button", { name: /pairing code/i }));
    expect(await screen.findByText("The form token is out of date.")).toBeInTheDocument();
    expect(posts).toBe(2);
  });
});
