// Accessibility of the main pages: axe (WCAG 2.x A and AA plus best practices) on each page as
// rendered from the contract's examples, its landmarks, and the keyboard path through the header.
// Colour contrast is checked on the design tokens instead (contrast.test.ts): jsdom has no layout.
import { screen, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { describe, expect, it } from "vitest";
import { labelTable } from "../src/lib/stackTables";
import { renderApp, type Who } from "./render";

const MAIN_PAGES: [path: string, heading: RegExp, who?: Who][] = [
  ["/", /^Home$/],
  ["/", /^Home$/, "builder"],
  ["/", /^Home$/, "viewer"],
  ["/apps", /^Applications$/],
  ["/triage", /^Scope triage$/],
  ["/recordings", /^Recordings$/],
  ["/recordings/9f2c4e1a7b3d5f60", /Order to invoice/],
  ["/apps/1", /Billing Desk/],
  ["/apps/1/setup", /Billing Desk: Setup \/ Advanced/],
  ["/apps/1/build-pack/preview", /Builder package preview/],
  ["/apps/1/build", /Build sessions: Billing Desk/],
  ["/builds/7", /Build session #7: Billing Desk, Claude/],
  ["/releases/5", /Billing Desk 1.1/],
  ["/sessions", /^Request a recording$/],
  ["/setup", /^Setup$/],
  ["/admin/users", /Users and portal admins/, "admin"],
  ["/admin/operations", /^Operations$/, "admin"],
];

async function violations(root: Element) {
  const result = await axe.run(root, {
    runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"] },
    rules: { "color-contrast": { enabled: false } },
  });
  return result.violations.map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(" ")).slice(0, 3).join(", ")}`);
}

async function loaded(heading: RegExp) {
  expect(await screen.findByRole("heading", { level: 1, name: heading })).toBeInTheDocument();
  await waitFor(() => expect(document.querySelector("[aria-busy='true']")).toBeNull());
}

describe("accessibility of the main pages", () => {
  it("would catch a violation (the check is wired up)", async () => {
    const sample = document.createElement("div");
    sample.innerHTML = `<details open><summary><a href="/x">Recording</a> 4 steps</summary></details><input type="text">`;
    document.body.append(sample);
    const found = await violations(sample).finally(() => sample.remove());
    expect(found.join(" | ")).toMatch(/nested-interactive/);
    expect(found.join(" | ")).toMatch(/label/);
  });

  it.each(MAIN_PAGES)("%s has no axe violations", async (path, heading, who = "owner") => {
    const { container } = renderApp(path, who);
    await loaded(heading);
    expect(await violations(container)).toEqual([]);
  });

  it.each(MAIN_PAGES)("%s has one h1, a main nav and a labelled region per section", async (path, heading, who = "owner") => {
    renderApp(path, who);
    await loaded(heading);
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
    expect(screen.getByRole("main")).toBeInTheDocument();
    const unnamed = Array.from(document.querySelectorAll("main section")).filter((s) => !s.getAttribute("aria-labelledby") && !s.getAttribute("aria-label"));
    expect(unnamed.map((s) => s.id || s.textContent?.slice(0, 40))).toEqual([]);
    for (const s of Array.from(document.querySelectorAll("main section[aria-labelledby]"))) {
      const label = document.getElementById(s.getAttribute("aria-labelledby") ?? "");
      expect(label?.textContent?.trim(), `section #${s.id} names a missing element`).toBeTruthy();
    }
  });

  it("puts the journey in a nav landmark on an application's page", async () => {
    renderApp("/apps/1");
    await loaded(/Billing Desk/);
    expect(screen.getByRole("navigation", { name: /^Progress of / })).toBeInTheDocument();
  });
});

describe("keyboard", () => {
  it("starts at the skip link, which moves focus to the content", async () => {
    const { user } = renderApp("/");
    await loaded(/^Home$/);
    await user.tab();
    const skip = screen.getByRole("link", { name: "Skip to content" });
    expect(skip).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("link", { name: "TwinLab2" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("link", { name: "Home" })).toHaveFocus();
  });

  it("follows the header in reading order, then the account controls", async () => {
    const { user } = renderApp("/", "admin");
    await loaded(/^Home$/);
    const order: string[] = [];
    await user.tab(); // the skip link
    for (let i = 0; i < 12; i++) {
      await user.tab();
      const el = document.activeElement as HTMLElement;
      if (!el.closest("header")) break;
      order.push(el.textContent?.trim() ?? "");
    }
    expect(order[0]).toBe("TwinLab2");
    expect(order.indexOf("Home")).toBeLessThan(order.indexOf("Applications"));
    // the account controls come after every page link
    const signOut = order.indexOf("Sign out");
    if (signOut >= 0) expect(signOut).toBe(order.length - 1);
  });

  it("moves focus to the new page's content after following a link", async () => {
    const { user } = renderApp("/");
    await loaded(/^Home$/);
    await user.click(screen.getByRole("link", { name: "Applications" }));
    await loaded(/^Applications$/);
    expect(screen.getByRole("main")).toHaveFocus();
  });

  it("has no positive tabindex anywhere", async () => {
    renderApp("/recordings/9f2c4e1a7b3d5f60");
    await loaded(/Order to invoice/);
    const positive = Array.from(document.querySelectorAll("[tabindex]")).filter((el) => Number(el.getAttribute("tabindex")) > 0);
    expect(positive).toEqual([]);
  });
});

describe("tables on a narrow screen", () => {
  it("labels each body cell with its column, so a row can stack as a card", () => {
    const holder = document.createElement("div");
    holder.innerHTML = `<table class="flow"><thead><tr><th scope="col">Application</th><th scope="col">Scope</th><th scope="col"></th></tr></thead>
      <tbody><tr><th scope="row">Billing</th><td>Replicate</td><td><button>Open</button></td></tr>
      <tr><td colspan="3">Nothing else</td></tr></tbody></table>`;
    const table = holder.querySelector("table") as HTMLTableElement;
    labelTable(table);
    const [first, second] = Array.from(table.tBodies[0]?.rows ?? []);
    expect(table).toHaveClass("stack");
    expect(first?.cells[0]).not.toHaveAttribute("data-label");
    expect(first?.cells[1]).toHaveAttribute("data-label", "Scope");
    expect(first?.cells[2]).not.toHaveAttribute("data-label");
    expect(second?.cells[0]).not.toHaveAttribute("data-label");
  });

  it("labels the tables of a rendered page", async () => {
    renderApp("/recordings/9f2c4e1a7b3d5f60");
    await loaded(/Order to invoice/);
    await waitFor(() => expect(document.querySelectorAll("main table.flow:not(.stack)")).toHaveLength(
      Array.from(document.querySelectorAll("main table.flow")).filter((t) => !(t as HTMLTableElement).tHead).length,
    ));
  });
});
