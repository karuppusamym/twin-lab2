import { screen } from "@testing-library/react";
import { HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import type { Schemas } from "../src/api/client";
import { example, on, reply } from "./contract";
import { renderApp } from "./render";
import { server } from "./setup";

const DRAFT_SHA = "8d2f4b6a0c1e3f5a7b9d1c3e5f7a9b1d3c5e7f9a1b3d5c7e9f1a3b5d7c9e1f3a";

describe("freezing an application's builder package", () => {
  it("approves exactly the previewed bytes, then opens the new revision", async () => {
    const sent: { body: unknown; csrf: string | null; path: string }[] = [];
    server.use(
      on("freezePackage", async ({ request }) => {
        sent.push({ body: await request.json(), csrf: request.headers.get("X-CSRF-Token"), path: new URL(request.url).pathname });
        return HttpResponse.json(example("freezePackage") as never, { status: 201 });
      }),
      reply("getPackage", "rev3"),
    );
    const { user } = renderApp("/apps/1/build-pack/preview", "owner");

    expect(await screen.findByRole("heading", { name: /Builder package preview/ })).toBeInTheDocument();
    expect(screen.getByText(DRAFT_SHA, { selector: "#archive-sha256" })).toBeInTheDocument();
    expect(screen.getByText("1 item excluded, 1 redacted.")).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "Invoice screen" })).toBeInTheDocument();

    const freeze = screen.getByRole("button", { name: "Freeze as revision 3" });
    expect(freeze).toBeDisabled();
    await user.type(screen.getByLabelText(/Change summary/), "Adds the unpaid-order scenario.");
    await user.click(screen.getByLabelText(/I reviewed this package and approve exactly these bytes/));
    expect(freeze).toBeEnabled();
    await user.click(freeze);

    expect(await screen.findByRole("heading", { name: "Builder package revision 3" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/apps/1/packages/3");
    expect(screen.getByText(DRAFT_SHA, { selector: "#package-sha256" })).toBeInTheDocument();
    expect(sent).toEqual([{
      path: "/api/v1/apps/1/packages",
      csrf: "3c1f0e2d9a8b7c6d5e4f3a2b1c0d9e8f",
      body: { sha256: DRAFT_SHA, summary: "Adds the unpaid-order scenario.", approve: true },
    }]);
  });

  it("shows the server's refusal when the draft changed since the preview", async () => {
    server.use(reply("freezePackage", "changed", "409"));
    const { user } = renderApp("/apps/1/build-pack/preview", "owner");
    await user.click(await screen.findByLabelText(/I reviewed this package/));
    await user.click(screen.getByRole("button", { name: "Freeze as revision 3" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The package changed since you previewed it. Preview it again before approving.");
    expect(window.location.pathname).toBe("/apps/1/build-pack/preview");
  });

  it("offers no freeze when nothing changed since the latest revision", async () => {
    server.use(on("previewPackageDraft", () => HttpResponse.json({ ...example<Record<string, unknown>>("previewPackageDraft"), unchanged: true } as never)));
    renderApp("/apps/1/build-pack/preview", "owner");
    expect(await screen.findByText(/Nothing changed since/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "revision 2" })).toHaveAttribute("href", "/apps/1/packages/2");
    expect(screen.queryByRole("button", { name: /Freeze as revision/ })).not.toBeInTheDocument();
  });

  it("hides the pre-freeze report of a frozen revision from builders", async () => {
    renderApp("/apps/1/packages/2", "builder");
    expect(await screen.findByRole("heading", { name: "Builder package revision 2" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download revision 2" })).toHaveAttribute("href", "/api/v1/apps/1/packages/2/download");
    expect(screen.queryByRole("heading", { name: "Pre-freeze report" })).not.toBeInTheDocument();
  });
  it("shows the completeness checklist with each gap's fix", async () => {
    renderApp("/apps/1/build-pack/preview", "owner");
    expect(await screen.findByRole("heading", { name: "Completeness" })).toBeInTheDocument();
    expect(screen.getByText(/92%/, { selector: "#completeness-summary strong" })).toBeInTheDocument();
    const gap = screen.getByText("Data model (DDL)").closest("li") as HTMLElement;
    expect(gap).toHaveTextContent("important");
    expect(gap).toHaveTextContent("Attach the DDL (.sql, kind Database)");
    expect(screen.getByRole("link", { name: "Uploaded evidence" })).toHaveAttribute("href", "/apps/1#examples");
    expect(screen.getByText(/By verification dimension/)).toHaveTextContent("deployment 78%");
    expect(screen.getByRole("button", { name: "Freeze as revision 3" })).toBeInTheDocument();
  });

  it("freezes a package with critical gaps only with a written reason", async () => {
    const draft = example<Schemas["PackageDraft"]>("previewPackageDraft");
    const gap: Schemas["CompletenessGap"] = { item: "scenario.expected", title: "Expected result of each scenario", section: "scenarios", dimension: "behavioural", severity: "critical",
      why: "The acceptance cases judge the business outcome against it.", details: ["Scenario \"Invoice an order\" does not state its expected result."],
      fix: { page: "Scenarios", href: "/apps/1#scenarios", action: "Write the expected result." } };
    draft.completeness = { ...draft.completeness, passed: false, counts: { critical: 1, important: 1, nice: 0 }, gaps: [gap, ...draft.completeness.gaps] };
    const sent: unknown[] = [];
    const previewed: (string | null)[] = [];
    server.use(
      on("previewPackageDraft", ({ request }) => {
        const reason = new URL(request.url).searchParams.get("override_reason");
        previewed.push(reason);
        const withGaps = reason ? { ...draft, sha256: "f".repeat(64), known_gaps: { reason, gaps: [{ item: gap.item, title: gap.title }] } } : draft;
        return HttpResponse.json(withGaps as never);
      }),
      on("freezePackage", async ({ request }) => {
        sent.push(await request.json());
        return HttpResponse.json(example("freezePackage") as never, { status: 201 });
      }),
      reply("getPackage", "rev3"),
    );
    const { user } = renderApp("/apps/1/build-pack/preview", "owner");
    expect(await screen.findByText("Critical items missing")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Scenarios" })).toHaveAttribute("href", "/apps/1#scenarios");
    await user.click(screen.getByLabelText(/I reviewed this package/));
    expect(screen.getByRole("button", { name: "Freeze with known gaps as revision 3" })).toBeDisabled();
    await user.type(screen.getByLabelText(/Reason to freeze with known gaps/), "Expected results follow from the demo.");
    await user.click(screen.getByRole("button", { name: "Add the known gaps to the package" }));
    // the draft is previewed again with the reason: the bytes the freeze stores
    expect(await screen.findByText(/This draft carries the known gaps/)).toBeInTheDocument();
    expect(screen.getByText("f".repeat(64), { selector: "#archive-sha256" })).toBeInTheDocument();
    await user.click(screen.getByLabelText(/I reviewed this package/));
    const freeze = screen.getByRole("button", { name: "Freeze with known gaps as revision 3" });
    expect(freeze).toBeEnabled();
    await user.click(freeze);
    expect(await screen.findByRole("heading", { name: "Builder package revision 3" })).toBeInTheDocument();
    expect(previewed.slice(0, 2)).toEqual([null, "Expected results follow from the demo."]);
    expect(sent).toEqual([{ sha256: "f".repeat(64), summary: "", approve: true, override_reason: "Expected results follow from the demo." }]);
  });

  it("shows the server's refusal of an incomplete package", async () => {
    server.use(reply("freezePackage", "incomplete", "409"));
    const { user } = renderApp("/apps/1/build-pack/preview", "owner");
    await user.click(await screen.findByLabelText(/I reviewed this package/));
    await user.click(screen.getByRole("button", { name: "Freeze as revision 3" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The package misses 1 critical item");
  });

  it("shows a revision's known gaps and the reason", async () => {
    const rev = example<Schemas["PackageRevision"]>("getPackage", "rev3");
    rev.known_gaps = { reason: "Expected results follow from the demo.", gaps: [{ item: "scenario.expected", title: "Expected result of each scenario" }] };
    server.use(on("getPackage", () => HttpResponse.json(rev as never)));
    renderApp("/apps/1/packages/3", "builder");
    expect(await screen.findByRole("heading", { name: "Known gaps" })).toBeInTheDocument();
    expect(screen.getByText("Expected results follow from the demo.")).toBeInTheDocument();
    expect(screen.getByText("Expected result of each scenario")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Completeness" })).toBeInTheDocument();
  });
});
