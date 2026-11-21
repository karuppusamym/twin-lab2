// The owner-facing redesign: the recording workboard (a row per application, one action each, the
// download of every approved package, the swim-lane timeline), the scope triage (cards, ranked
// suggestions, bulk and reversible decisions, no "discovered" choice), the role-aware homes and
// navigation, the Build page's reasons when it cannot start, and the accessibility fixes.
import { act, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WaitingAnnouncer } from "../src/pages/BuildSessionPage";
import { SCOPE_LABELS } from "../src/lib/scope";
import { example, on, reply } from "./contract";
import { renderApp } from "./render";
import { server } from "./setup";
import { render } from "@testing-library/react";

afterEach(() => vi.restoreAllMocks());

describe("the recording workboard", () => {
  it("shows one row per application with its state and one action", async () => {
    renderApp("/recordings/9f2c4e1a7b3d5f60", "owner");
    const table = await screen.findByRole("table", { name: "Workboard" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((r) => within(r).getAllByRole("rowheader")[0]?.querySelector("a")?.textContent)).toEqual(["Outlook", "Billing Desk", "CaseDesk"]);
    const [outlook, billing, casedesk] = rows as [HTMLElement, HTMLElement, HTMLElement];
    expect(within(outlook).getByText("Needs a scope decision")).toBeInTheDocument();
    expect(within(outlook).getByRole("link", { name: "Decide scope" })).toHaveAttribute("href", "/recordings/9f2c4e1a7b3d5f60/triage#app-3");
    expect(within(outlook).getByText("OUTLOOK.EXE")).toBeInTheDocument();
    expect(within(billing).getByText("billing.example.test")).toBeInTheDocument();
    expect(within(billing).getByText("2 reviewed")).toBeInTheDocument();
    expect(within(billing).getByRole("link", { name: "Package revision 1 of Billing Desk" })).toHaveAttribute("href", "/apps/1/packages/1");
    expect(within(billing).getByText("ready, 2 warnings")).toBeInTheDocument();
    expect(within(billing).getByRole("link", { name: "Verify this build" })).toHaveAttribute("href", "/builds/3");
    expect(within(casedesk).getByText("1 to review")).toBeInTheDocument();
    expect(within(casedesk).getByRole("link", { name: "Review steps" })).toHaveAttribute("href", "/recordings/9f2c4e1a7b3d5f60?app_id=2#assign");
    // exactly one action per row
    for (const row of rows) expect(within(row).getAllByRole("link").filter((a) => a.classList.contains("button"))).toHaveLength(1);
    expect(screen.getByRole("link", { name: "Download all approved packages" })).toHaveAttribute("href", "/api/v1/recordings/9f2c4e1a7b3d5f60/packages/download");
    // the raw tables, preflight and upload are in the collapsed details
    const details = document.getElementById("details") as HTMLDetailsElement;
    expect(details.open).toBe(false);
    expect(within(details).getByRole("heading", { name: "Preflight" })).toBeInTheDocument();
    expect(within(details).getByRole("heading", { name: "Upload" })).toBeInTheDocument();
  });

  it("shows a swim lane per application, coloured by surface, with its steps on hover", async () => {
    const { user } = renderApp("/recordings/9f2c4e1a7b3d5f60", "owner");
    const lanes = await screen.findByRole("list", { name: "Applications over time" });
    const items = within(lanes).getAllByRole("listitem");
    expect(items.map((li) => li.getAttribute("aria-label"))).toEqual([
      "Billing Desk: 2 steps in 1 run", "CaseDesk: 1 step in 1 run", "Outlook: 4 steps in 1 run",
    ]);
    const run = within(items[0] as HTMLElement).getByRole("button");
    expect(run).toHaveClass("surface-web");
    expect(within(items[1] as HTMLElement).getByRole("button")).toHaveClass("surface-desktop");
    await user.hover(run);
    expect(document.getElementById("timeline-hover")).toHaveTextContent("Billing Desk: 2 steps from 09:00:00 to 09:01:00. 09:00:00 click Orders · Orders; 09:01:00 submit Invoice · Invoice");
    // runs are placed on the time axis: Billing Desk starts at the left edge
    expect(run.style.left).toBe("0%");
  });

  it("explains how a shared host's steps were told apart", async () => {
    const board = example<Record<string, unknown>>("getRecordingWorkboard");
    server.use(on("getRecordingWorkboard", () => HttpResponse.json({ ...board, shared_hosts: [{
      host: "erp.example.test", apps: [{ app_id: 1, name: "Billing Desk", path: "/billing" }, { app_id: 4, name: "Billing Admin", path: "/billing-admin" }],
      text: "Billing Admin (/billing-admin), Billing Desk (/billing) share erp.example.test: each step went to the application whose path is the longest prefix of the step's address (matched by the longest path prefix).",
    }] } as never)));
    renderApp("/recordings/9f2c4e1a7b3d5f60", "owner");
    expect(await screen.findByText(/matched by the longest path prefix/)).toBeInTheDocument();
  });

  it("opens the assignment when the address names an application", async () => {
    renderApp("/recordings/9f2c4e1a7b3d5f60?app_id=2#assign", "owner");
    await screen.findByRole("table", { name: "Workboard" });
    expect((document.getElementById("assign") as HTMLDetailsElement).open).toBe(true);
  });
});

describe("the scope triage", () => {
  it("shows a card per undecided application with what it looks like", async () => {
    renderApp("/triage", "owner");
    const card = await screen.findByRole("article", { name: /Outlook/ });
    expect(within(card).getByText("OUTLOOK.EXE")).toBeInTheDocument();
    expect(within(card).getByText("Inbox · New message")).toBeInTheDocument();
    expect(within(card).getByText("Send button · To field")).toBeInTheDocument();
    expect(within(card).getByText("(similar program file)")).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Replicate Outlook" })).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Outlook: not replicated (used only)" })).toBeInTheDocument();
    // "Same as" is a merge, previewed first; "Needs a scope decision" is never a choice
    const sameAs = within(card).getByRole("form", { name: "Outlook is the same as" });
    expect(within(sameAs).getAllByRole("option", { hidden: true }).map((o) => o.textContent)).toEqual(["CaseDesk (suggested)", "Billing Desk"]);
    expect(within(card).queryByRole("option", { name: SCOPE_LABELS.discovered })).not.toBeInTheDocument();
  });

  it("decides for the selected cards at once, says it can be reversed, and shows the outcome", async () => {
    const bodies: unknown[] = [];
    server.use(on("decideScope", async ({ request }) => {
      bodies.push(await request.json());
      return HttpResponse.json(example("decideScope") as never);
    }));
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const { user } = renderApp("/triage", "owner");
    await screen.findByRole("article", { name: /Outlook/ });
    const bulk = screen.getByRole("group", { name: "Decide for the selected applications" });
    expect(within(bulk).getByRole("button", { name: "Replicate selected" })).toBeDisabled();
    await user.click(within(bulk).getByLabelText(/Select all/));
    await user.click(within(bulk).getByRole("button", { name: "Not replicated (used only)" }));
    expect(confirm).toHaveBeenCalledWith(expect.stringMatching(/You can reverse this later/));
    await waitFor(() => expect(bodies).toEqual([{ decisions: [{ app_id: 3, decision: "not_replicated", expected_revision: 1 }] }]));
    expect(await screen.findByText(/You can reverse this at any time/)).toBeInTheDocument();
  });

  it("does nothing when the confirmation is declined", async () => {
    let called = false;
    server.use(on("decideScope", () => { called = true; return HttpResponse.json(example("decideScope") as never); }));
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const { user } = renderApp("/triage", "owner");
    await user.click(await screen.findByRole("button", { name: "Replicate Outlook" }));
    expect(called).toBe(false);
  });
});

describe("the home of each role", () => {
  it("gives a viewer the published demos", async () => {
    server.use(reply("getHome", "viewer"));
    renderApp("/", "viewer");
    const published = await screen.findByRole("region", { name: "Published demos" });
    expect(within(published).getByRole("link", { name: "Open the Billing Desk demo" })).toHaveAttribute("href", "https://billing-demo.example.test");
    expect(within(published).getByRole("link", { name: "Download Billing Desk 1.4.0" })).toHaveAttribute("href", "/api/v1/releases/7/download");
    expect(screen.queryByRole("region", { name: "Ready to build" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Recordings and scope decisions" })).not.toBeInTheDocument();
  });

  it("gives a builder the packages ready to build and the questions waiting", async () => {
    server.use(reply("getHome", "builder"));
    renderApp("/", "builder");
    const ready = await screen.findByRole("region", { name: "Ready to build" });
    expect(within(ready).getByRole("link", { name: "Build Billing Desk" })).toHaveAttribute("href", "/apps/1/build");
    expect(within(within(ready).getByRole("list", { name: "Waiting for your answer" })).getByRole("link", { name: "Build session #3 of Billing Desk" })).toHaveAttribute("href", "/builds/3");
    expect(screen.queryByRole("region", { name: "Recordings and scope decisions" })).not.toBeInTheDocument();
  });

  it("gives an owner the workboards and the triage queue", async () => {
    renderApp("/", "owner");
    const work = await screen.findByRole("region", { name: "Recordings and scope decisions" });
    expect(within(work).getByRole("link", { name: "Decide scope" })).toHaveAttribute("href", "/triage");
    expect(within(work).getByRole("link", { name: "Order to invoice" })).toHaveAttribute("href", "/recordings/9f2c4e1a7b3d5f60");
    expect(within(work).getByText("1 to decide")).toBeInTheDocument();
  });

  it("gives an admin the setup", async () => {
    server.use(reply("getHome", "admin"));
    renderApp("/", "admin");
    const admin = await screen.findByRole("region", { name: "Portal setup" });
    expect(within(admin).getByRole("link", { name: "Setup" })).toHaveAttribute("href", "/setup");
  });
});

describe("navigation and gating by role", () => {
  const nav = () => within(screen.getByRole("navigation", { name: "Main" }));

  it("shows a viewer only what a viewer can use", async () => {
    server.use(reply("getHome", "viewer"));
    renderApp("/", "viewer");
    await screen.findByRole("heading", { level: 1, name: "Home" });
    expect(nav().getAllByRole("link").map((a) => a.textContent)).toEqual(["Home", "Applications"]);
  });

  it("shows an owner recordings, triage, requests and setup", async () => {
    renderApp("/", "owner");
    await screen.findByRole("heading", { level: 1, name: "Home" });
    expect(nav().getAllByRole("link").map((a) => a.textContent)).toEqual(["Home", "Applications", "Recordings", "Scope triage", "Request a recording", "Setup"]);
  });

  it("does not offer a viewer to add an application or import a recording", async () => {
    renderApp("/apps", "viewer");
    await screen.findByRole("heading", { level: 2, name: /Billing Desk/ });
    expect(screen.queryByRole("heading", { name: "Add an application" })).not.toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Import recording" })).not.toBeInTheDocument();
  });

  it("offers an owner to add an application", async () => {
    renderApp("/apps", "owner");
    expect(await screen.findByRole("heading", { name: "Add an application" })).toBeInTheDocument();
  });

  it("numbers a builder's sections in the order shown", async () => {
    renderApp("/apps/1", "builder");
    expect(await screen.findByRole("heading", { level: 2, name: "1. Choose application" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "2. Build" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "3. Verify, then open or download" })).toBeInTheDocument();
  });

  it("hides the storage location from everyone but admins", async () => {
    server.use(on("getSetup", () => HttpResponse.json({ ...example<object>("getSetup"), storage: null } as never)));
    renderApp("/setup", "owner");
    await screen.findByRole("heading", { level: 1, name: "Setup" });
    expect(screen.queryByRole("heading", { name: "Artifact storage" })).not.toBeInTheDocument();
  });
});

describe("the Build page", () => {
  it("disables a new session and says why when no repository is connected", async () => {
    server.use(on("getBuildPage", () => HttpResponse.json({ ...example<object>("getBuildPage"), repos: [] } as never)));
    renderApp("/apps/1/build", "builder");
    expect(await screen.findByRole("note")).toHaveTextContent("Cannot start yet: No GitHub repository is connected for build sessions.");
    expect(screen.getByRole("button", { name: "Start build session" })).toBeDisabled();
  });

  it("registers a release from a commit, replacing the release command", async () => {
    const bodies: unknown[] = [];
    server.use(on("registerRelease", async ({ request }) => {
      bodies.push(await request.json());
      return HttpResponse.json(example("registerRelease") as never, { status: 201 });
    }));
    const { user } = renderApp("/apps/1/build", "builder");
    const form = await screen.findByRole("form", { name: "Register a commit" });
    await user.type(within(form).getByLabelText(/Commit SHA/), "7c41e9b2d05f3a68c1e4b97a20d6f58e3b9c1a74");
    await user.click(within(form).getByRole("button", { name: "Register the commit" }));
    await waitFor(() => expect(bodies).toEqual([{ sha: "7c41e9b2d05f3a68c1e4b97a20d6f58e3b9c1a74" }]));
    await waitFor(() => expect(window.location.pathname).toBe("/releases/8"));
  });
});

describe("accessibility", () => {
  it("names each Remove button after what it removes", async () => {
    renderApp("/apps/1", "owner");
    const removes = await screen.findAllByRole("button", { name: /^Remove / });
    const names = removes.map((b) => b.getAttribute("aria-label"));
    expect(new Set(names).size).toBe(names.length);
  });

  it("announces a new question from the provider", () => {
    const messages = [{ id: 9, body: "Which currency?" }] as never;
    const { rerender } = render(<WaitingAnnouncer outstanding={[]} messages={messages} who="Claude" />);
    const live = document.getElementById("chat-announcer") as HTMLElement;
    expect(live).toHaveAttribute("aria-live", "polite");
    expect(live).toHaveTextContent("");
    act(() => rerender(<WaitingAnnouncer outstanding={[{ id: 9, kind: "clarification" }] as never} messages={messages} who="Claude" />));
    expect(live).toHaveTextContent("Claude asks: Which currency?");
  });
});
