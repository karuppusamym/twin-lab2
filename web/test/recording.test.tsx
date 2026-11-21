import { screen, within } from "@testing-library/react";
import { HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { example, on } from "./contract";
import { renderApp } from "./render";
import { server } from "./setup";

describe("a recording that holds several applications", () => {
  it("shows each application with its own steps and its own package", async () => {
    renderApp("/recordings/9f2c4e1a7b3d5f60", "owner");
    const section = await screen.findByRole("region", { name: "Recorded applications" });
    const timeline = within(section).getByRole("list");
    expect(within(timeline).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "Billing Desk 2 steps", "CaseDesk 3 steps", "Other applications 1 step",
    ]);

    const billing = within(section).getByRole("group", { name: "Billing Desk" });
    const casedesk = within(section).getByRole("group", { name: "CaseDesk" });

    const billingSteps = within(within(billing).getByRole("table", { name: "Steps of Billing Desk" })).getAllByRole("row").slice(1);
    expect(billingSteps.map((r) => within(r).getAllByRole("cell")[3]?.textContent)).toEqual(["Search", "Invoice"]);
    const caseSteps = within(within(casedesk).getByRole("table", { name: "Steps of CaseDesk" })).getAllByRole("row").slice(1);
    expect(caseSteps.map((r) => within(r).getAllByRole("cell")[3]?.textContent)).toEqual(["Case 77", "Note", "Close case"]);
    expect(within(billing).queryByText("Close case")).not.toBeInTheDocument();
    expect(within(casedesk).queryByText("Invoice")).not.toBeInTheDocument();

    expect(within(billing).getByRole("link", { name: "revision 2" })).toHaveAttribute("href", "/apps/1/packages/2");
    expect(within(billing).getByRole("link", { name: "Download" })).toHaveAttribute("href", "/api/v1/apps/1/packages/2/download");
    expect(within(casedesk).getByRole("link", { name: "revision 1" })).toHaveAttribute("href", "/apps/2/packages/1");
    expect(within(casedesk).getByRole("link", { name: "Download" })).toHaveAttribute("href", "/api/v1/apps/2/packages/1/download");
    // all of them at once: the per-recording export
    expect(within(section).getByRole("link", { name: "Download the packages of this recording" }))
      .toHaveAttribute("href", "/api/v1/recordings/9f2c4e1a7b3d5f60/packages/download");
  });

  it("says so when an application of the recording has no package yet", async () => {
    const detail = example<{ refs: { apps: { latest_package: unknown }[] } }>("getRecording");
    const second = detail.refs.apps[1];
    if (second) second.latest_package = null;
    server.use(on("getRecording", () => HttpResponse.json(detail as never)));
    renderApp("/recordings/9f2c4e1a7b3d5f60", "owner");
    const casedesk = await screen.findByRole("group", { name: "CaseDesk" });
    expect(within(casedesk).getByText("No builder-approved package of CaseDesk yet.")).toBeInTheDocument();
    expect(within(screen.getByRole("group", { name: "Billing Desk" })).getByRole("link", { name: "revision 2" })).toBeInTheDocument();
  });

  it("saves the owner's assignment of events to one application by event_id", async () => {
    let saved: unknown;
    server.use(on("saveAssignment", async ({ request, params }) => {
      saved = { app: params.app_id, body: await request.json() };
      return HttpResponse.json(example("saveAssignment") as never);
    }));
    const { user } = renderApp("/recordings/9f2c4e1a7b3d5f60", "owner");
    await user.selectOptions(await screen.findByLabelText("Demo app"), "CaseDesk");
    await user.click(screen.getByRole("button", { name: "Open assignment" }));
    const form = await screen.findByRole("form", { name: "Assignment" });
    // the saved selection of the example (events 1 and 2) is loaded; add event 5
    expect(within(form).getByLabelText(/^1 · click/)).toBeChecked();
    expect(within(form).getByText("in an interrupted period")).toBeInTheDocument();
    await user.click(within(form).getByLabelText(/^5 · click/));
    await user.click(within(form).getByRole("button", { name: "Save assignment" }));
    await screen.findByRole("heading", { level: 1, name: /Billing Desk/ });
    expect(saved).toEqual({
      app: "2",
      body: {
        events: ["0b1c2d3e-0000-4000-8000-000000000001", "0b1c2d3e-0000-4000-8000-000000000002", "0b1c2d3e-0000-4000-8000-000000000005"],
        notes: "Invoice is created from the saved order.",
      },
    });
  });
});
