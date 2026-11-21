// The owner's authoring and the release pages: the guided scenario form, the one event chooser,
// readable release versions, distinct verification reports, a build without a pull request and
// searchable application pickers.
import { screen, waitFor, within } from "@testing-library/react";
import { HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { example, on } from "./contract";
import { renderApp } from "./render";
import { server } from "./setup";

type ScenarioList = { scenarios: Record<string, unknown>[]; event_choices: Record<string, unknown>[] };

describe("the guided scenario form", () => {
  it("writes steps and failure cases as lists and saves them one per line", async () => {
    let body: Record<string, unknown> | undefined;
    server.use(on("createScenario", async ({ request }) => {
      body = await request.json() as Record<string, unknown>;
      return HttpResponse.json(example("createScenario") as never, { status: 201 });
    }));
    const { user } = renderApp("/apps/1", "owner");
    const form = await screen.findByRole("form", { name: "New scenario" });
    expect(within(form).getByRole("group", { name: "Starting conditions" })).toBeInTheDocument();
    expect(within(form).getByRole("group", { name: "Expected result" })).toBeInTheDocument();
    await user.type(within(form).getByLabelText("Scenario name"), "Pay an invoice");
    const steps = within(form).getByRole("group", { name: "Steps" });
    await user.type(within(steps).getByLabelText("Step 1"), "Open the invoice{Enter}");
    await waitFor(() => expect(within(steps).getByLabelText("Step 2")).toHaveFocus());
    await user.type(within(steps).getByLabelText("Step 2"), "Press Pay");
    await user.click(within(steps).getByRole("button", { name: "Add a step to the new scenario" }));
    expect(within(steps).getByLabelText("Step 3")).toHaveValue("");
    await user.click(within(steps).getByRole("button", { name: "Move step 2 of the new scenario up" }));
    const failures = within(form).getByRole("group", { name: "Failure cases" });
    await user.type(within(failures).getByLabelText("Failure case 1"), "Already paid: no second payment");
    await user.click(within(form).getByRole("button", { name: "Add scenario" }));
    await waitFor(() => expect(body).toBeDefined());
    // the blank third step is not saved
    expect(body).toMatchObject({ name: "Pay an invoice", actions: "Press Pay\nOpen the invoice", failures: "Already paid: no second payment" });
  });

  it("keeps the notes migrated from free-text requirements in sight", async () => {
    const list = example<ScenarioList>("listScenarios");
    list.scenarios = [{ ...list.scenarios[0], name: "Requirements (migrated)", starting: "", actions: "", expected: "", failures: "",
                        notes: "Invoices are numbered per year.\nA paid invoice cannot change.", events: [] }];
    server.use(on("listScenarios", () => HttpResponse.json(list as never)));
    renderApp("/apps/1", "owner");
    const form = await screen.findByRole("form", { name: "Scenario Requirements (migrated)" });
    const notes = within(form).getByRole("group", { name: "Notes carried over from the old requirements" });
    expect(within(notes).getByRole("textbox")).toHaveValue("Invoices are numbered per year.\nA paid invoice cannot change.");
    expect(within(notes).getByText(/Move each part into the steps/)).toBeInTheDocument();
  });

  it("links events with the same chooser as a recording: grouped by screen, filtered by screen", async () => {
    const { user } = renderApp("/apps/1", "owner");
    const form = await screen.findByRole("form", { name: /^Scenario / });
    const chooser = within(form).getByRole("group", { name: "Recorded events that show these steps" });
    const table = within(chooser).getByRole("table");
    expect(within(table).getAllByRole("rowheader").map((h) => h.querySelector(".group-title")?.textContent)).toEqual(["Orders", "Order 1001"]);
    await user.selectOptions(within(chooser).getByLabelText("Screen"), "Order 1001");
    expect(within(chooser).getAllByRole("checkbox")).toHaveLength(1);
    expect(within(chooser).getByRole("status")).toHaveTextContent("1 matching event");
    await user.click(within(chooser).getByRole("button", { name: "Select all 1 matching" }));
    expect(within(chooser).getByLabelText(/^2 · click Invoice/)).toBeChecked();
  });
});

describe("the release page", () => {
  it("names the release readably, keeps the SHA and tells the reports apart", async () => {
    const detail = example<{ checks: { reports: unknown[] } }>("getRelease");
    detail.checks.reports = [
      { id: 4, number: 2, latest: true, created_at: "2026-11-20T10:00:00+00:00", sha256: "a".repeat(64), tested: true, requested_by: "alice" },
      { id: 3, number: 1, latest: false, created_at: "2026-11-19T16:00:00+00:00", sha256: "b".repeat(64), tested: false, requested_by: null },
    ];
    server.use(on("getRelease", () => HttpResponse.json(detail as never)));
    renderApp("/releases/5", "owner");
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent(/^Billing Desk 1\.1 \(3f9a1c2\)/);
    expect(screen.getAllByText("3f9a1c2d4e5b6a7980a1b2c3d4e5f60718293a4b").length).toBeGreaterThan(0);
    const reports = screen.getByRole("list", { name: /Verification reports/ });
    const items = within(reports).getAllByRole("listitem");
    expect(items[0]).toHaveTextContent(/Report 2.*Latest.*Passed.*2026-11-20 10:00 by alice/);
    expect(items[1]).toHaveTextContent(/Report 1.*Failed.*2026-11-19 16:00/);
    expect(within(reports).getByRole("link", { name: /^Verification report 2 of Billing Desk 1\.1 \(3f9a1c2\), the latest: passed/ })).toHaveAttribute("href", "/api/v1/releases/5/reports/4");
  });
});

describe("a build session", () => {
  it("says plainly when there is no pull request and links the commit", async () => {
    const detail = example<{ build: Record<string, unknown> }>("getBuild");
    detail.build = { ...detail.build, pr_number: null, pr_url: null, head_sha: "7c41e9b2d05f3a68c1e4b97a20d6f58e3b9c1a74" };
    server.use(on("getBuild", () => HttpResponse.json(detail as never)));
    renderApp("/builds/7", "builder");
    const pr = await screen.findByText(/^No pull request:/);
    expect(pr.closest("dd")).toHaveTextContent(/pushed commit 7c41e9b .*to twinlab2\/app-1\/session-7-claude/);
    expect(within(pr.closest("dd") as HTMLElement).getByRole("link", { name: "7c41e9b" }))
      .toHaveAttribute("href", "https://github.com/example/billing-desk-demo/commit/7c41e9b2d05f3a68c1e4b97a20d6f58e3b9c1a74");
  });
});

describe("a searchable application picker", () => {
  it("narrows the list as the owner types and chooses with the keyboard", async () => {
    const { user } = renderApp("/recordings/9f2c4e1a7b3d5f60", "owner");
    const box = await screen.findByRole("combobox", { name: "Demo app" });
    await user.type(box, "case");
    const list = screen.getByRole("listbox", { name: "Demo app" });
    expect(within(list).getAllByRole("option").map((o) => o.textContent)).toEqual(["CaseDesk"]);
    await user.keyboard("{Enter}");
    expect(box).toHaveValue("CaseDesk");
    expect(box).toHaveAttribute("aria-expanded", "false");
    await user.click(screen.getByRole("button", { name: "Open assignment" }));
    expect(await screen.findByRole("form", { name: "Assignment" })).toBeInTheDocument();
  });
});
