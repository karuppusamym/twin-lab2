import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply } from "./contract";
import { recordRequests, renderApp } from "./render";
import { server } from "./setup";

const evidencePaths = ["/api/v1/apps/1/evidence", "/api/v1/apps/1/scenarios"];

describe("role gating on an application's page", () => {
  it("shows a viewer a read-only page and never asks for restricted evidence", async () => {
    server.use(reply("getApp", "viewer"));
    const seen = recordRequests();
    renderApp("/apps/1", "viewer");
    expect(await screen.findByRole("heading", { level: 1, name: /Billing Desk/ })).toBeInTheDocument();
    expect(screen.getByText(/You have read-only access to this application/)).toBeInTheDocument();
    expect(await screen.findByRole("cell", { name: "ben" })).toBeInTheDocument();

    expect(screen.queryByRole("form", { name: "Assign role" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "2. Add examples" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "3. Review required behaviour" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Preview and approve a builder package" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Builder-approved packages" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Audit history" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "1.0.0" })[0]).toHaveAttribute("href", "/releases/5");
    expect(seen.map((r) => r.path).filter((p) => evidencePaths.includes(p))).toEqual([]);
  });

  it("shows a builder the approved packages and build sessions, not the evidence", async () => {
    const seen = recordRequests();
    renderApp("/apps/1", "builder");
    expect(await screen.findByRole("heading", { name: "Builder-approved packages" })).toBeInTheDocument();
    expect(screen.getByText(/you can open its builder-approved packages below/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Build sessions" })).toHaveAttribute("href", "/apps/1/build");
    expect(screen.getByRole("link", { name: "Revision 2" })).toHaveAttribute("href", "/apps/1/packages/2");
    expect(screen.getAllByRole("link", { name: "Download" })[0]).toHaveAttribute("href", "/api/v1/apps/1/packages/2/download");

    expect(screen.queryByRole("heading", { name: "2. Add examples" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Preview and approve a builder package" })).not.toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Assign role" })).not.toBeInTheDocument();
    expect(seen.map((r) => r.path).filter((p) => evidencePaths.includes(p))).toEqual([]);
  });

  it("shows an owner the evidence, scenarios, roles and the package approval", async () => {
    renderApp("/apps/1", "owner");
    expect(await screen.findByRole("heading", { name: "2. Add examples" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "3. Review required behaviour" })).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Assign role" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Preview and approve a builder package" })).toHaveAttribute("href", "/apps/1/build-pack/preview");
    expect(screen.getByRole("link", { name: "Audit history" })).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Upload evidence" })).toBeInTheDocument();
    expect(screen.getByText("Screen · Invoice screen")).toBeInTheDocument();
    expect(screen.queryByText(/read-only access/)).not.toBeInTheDocument();
  });

  it("lets a portal admin manage roles and users without owner rights", async () => {
    const seen = recordRequests();
    renderApp("/apps/1", "admin");
    expect(await screen.findByRole("form", { name: "Assign role" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Users" })).toHaveAttribute("href", "/admin/users");
    expect(screen.getByRole("link", { name: "Audit history" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "2. Add examples" })).not.toBeInTheDocument();
    expect(seen.map((r) => r.path).filter((p) => evidencePaths.includes(p))).toEqual([]);
  });

  it("hides the Users page link from everyone but admins", async () => {
    renderApp("/", "owner");
    expect(await screen.findByRole("heading", { name: "Applications" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Users" })).not.toBeInTheDocument();
  });
});
