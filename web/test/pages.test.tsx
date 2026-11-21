// Every page renders from the contract's examples, with its heading and without an error.
import { screen, waitFor } from "@testing-library/react";
import { HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { on } from "./contract";
import { renderApp, type Who } from "./render";
import { server } from "./setup";

const PAGES: [path: string, heading: RegExp, who?: Who][] = [
  ["/", /^Home$/],
  ["/", /^Home$/, "builder"],
  ["/", /^Home$/, "viewer"],
  ["/", /^Home$/, "admin"],
  ["/apps", /^Applications$/],
  ["/triage", /^Scope triage$/],
  ["/recordings/9f2c4e1a7b3d5f60/triage", /^Scope triage$/],
  ["/apps/1", /Billing Desk/],
  ["/apps/1/setup", /Billing Desk: Setup \/ Advanced/],
  ["/apps/1/history", /Audit history: Billing Desk/],
  ["/apps/3/merge?into=1", /Merge Outlook into Billing Desk/],
  ["/apps/1/build-pack/preview", /Builder package preview/],
  ["/apps/1/packages/2", /Builder package revision 2/],
  ["/apps/1/build", /Build sessions: Billing Desk/],
  ["/apps/1/sessions/compare", /Compare results: Billing Desk/],
  ["/builds/7", /Build session #7: Billing Desk, Claude/],
  ["/releases/5", /Billing Desk 1.0.0/],
  ["/recordings", /^Recordings$/],
  ["/recordings/9f2c4e1a7b3d5f60", /Order to invoice/],
  ["/sessions", /^Request a recording$/],
  ["/machines", /^Machines$/],
  ["/automations", /^Automations$/],
  ["/automations/1", /UiPath order to invoice/],
  ["/setup", /^Setup$/],
  ["/setup/builds", /Build sessions: Setup/, "admin"],
  ["/admin/users", /Users and portal admins/, "admin"],
  ["/no/such/page", /Not found/],
];

describe("every page", () => {
  it.each(PAGES)("%s renders", async (path, heading, who = "owner") => {
    renderApp(path, who);
    expect(await screen.findByRole("heading", { level: 1, name: heading })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText(/…$/)).not.toBeInTheDocument());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says what a Tested release's evidence covers, and links the pinned release manifest", async () => {
    renderApp("/releases/5", "owner");
    expect(await screen.findByTestId("verified-scope")).toHaveTextContent(/2 of 2 scenarios passed .* \(owner\), against https:\/\/billing-demo/);
    expect(screen.getByRole("link", { name: "OpenShift manifest (release)" })).toHaveAttribute("href", "/api/v1/releases/5/openshift.yaml");
  });

  it("shows the portal's auth mode on the Users page", async () => {
    renderApp("/admin/users", "admin");
    expect(await screen.findByTestId("auth-mode")).toHaveTextContent(/Auth mode: (local-dev|individual|trusted-proxy)/);
  });

  it("shows a server refusal as the problem's detail", async () => {
    server.use(on("getApp", () => HttpResponse.json(
      { type: "about:blank", title: "Forbidden", status: 403, detail: "This needs the viewer role on this application.", code: "role" },
      { status: 403, headers: { "Content-Type": "application/problem+json" } },
    )));
    renderApp("/apps/9", "viewer");
    expect(await screen.findByRole("alert")).toHaveTextContent("This needs the viewer role on this application.");
  });
});

describe("a build session's chat", () => {
  it("answers the provider's open question and decides a tool", async () => {
    const sent: unknown[] = [];
    server.use(
      on("answerBuild", async ({ request }) => { sent.push(await request.json()); return HttpResponse.json({ message: { id: 33, direction: "to_provider", kind: "clarification", author: "ben", body: "x", created_at: "", state: "queued", state_label: "Queued", error: null, reply_to: 31, scenario_id: null, data: {}, decision: null }, created: true, notice: null } as never, { status: 201 }); }),
      on("decideTool", async ({ request }) => { sent.push(await request.json()); return HttpResponse.json({} as never); }),
    );
    const { user } = renderApp("/builds/7", "builder");
    expect(await screen.findByText("Is VAT added before or after the discount?")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Your answer"), "After the discount.");
    await user.click(screen.getByRole("button", { name: "Send to Claude" }));
    await user.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(sent).toHaveLength(2));
    expect(sent[0]).toMatchObject({ answer: "After the discount.", scope_change: false, reply_to: 31 });
    expect(sent[1]).toEqual({ decision: "approve", request_key: "tool:32:approve" });
    // only an owner verifies a build
    expect(screen.queryByRole("button", { name: "Verify this build" })).not.toBeInTheDocument();
  });
});
