import { screen } from "@testing-library/react";
import { HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
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
});
