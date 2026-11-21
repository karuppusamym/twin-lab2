// Safe editing and a resilient, scalable UI: drafts survive refetches, a stale save shows yours
// next to theirs, a failed refresh keeps the page, mutations refresh only what they affect, an
// assignment can be removed, the catalog filters and pages, and 2,000 events stay responsive.
import { screen, waitFor, within } from "@testing-library/react";
import { HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";
import { affectedFamilies } from "../src/components/ui";
import { example, on } from "./contract";
import { renderApp } from "./render";
import { server } from "./setup";

type Assignment = { revision: number; notes: string; selected: string[]; saved: boolean; removed: boolean };
const RECORDING = "/recordings/9f2c4e1a7b3d5f60";
const E = (n: number) => `0b1c2d3e-0000-4000-8000-00000000000${n}`;

async function openAssignment(path = RECORDING) {
  const rendered = renderApp(path, "owner");
  await rendered.user.selectOptions(await screen.findByLabelText("Demo app"), "CaseDesk");
  await rendered.user.click(screen.getByRole("button", { name: "Open assignment" }));
  const form = await screen.findByRole("form", { name: "Assignment" });
  await within(form).findByLabelText(/^1 · click/);
  return { ...rendered, form };
}

function serveAssignment(get: () => Assignment) {
  server.use(on("getAssignment", () => HttpResponse.json({ ...example<Assignment>("getAssignment"), ...get() } as never)));
}

function conflict(current: Partial<Assignment>) {
  return HttpResponse.json({
    type: "about:blank", title: "Conflict", status: 409, code: "conflict",
    detail: "Someone else changed this assignment since you opened it (now revision 3).",
    current: { ...example<Assignment>("getAssignment"), ...current },
  } as never, { status: 409, headers: { "Content-Type": "application/problem+json" } });
}

afterEach(() => vi.restoreAllMocks());

describe("a draft", () => {
  it("follows the server until the owner edits it, and a refetch never wipes it", async () => {
    let server_: Assignment = { ...example<Assignment>("getAssignment") };
    serveAssignment(() => server_);
    const { user, client, form } = await openAssignment();
    const notes = within(form).getByLabelText("Outcome and build notes");
    expect(notes).toHaveValue("Invoice is created from the saved order.");
    expect(within(form).queryByText("Unsaved changes")).not.toBeInTheDocument();

    // not edited yet: another owner's save shows up
    server_ = { ...server_, revision: 3, notes: "Saved by ana." };
    await client.invalidateQueries({ queryKey: ["recording"] });
    await waitFor(() => expect(notes).toHaveValue("Saved by ana."));

    // edited: a refetch with yet another version keeps the draft
    await user.clear(notes);
    await user.type(notes, "My draft");
    await user.click(within(form).getByLabelText(/^5 · click/));
    expect(within(form).getByText("Unsaved changes")).toBeInTheDocument();
    server_ = { ...server_, revision: 4, notes: "Saved by ben.", selected: [E(1)] };
    await client.invalidateQueries({ queryKey: ["recording"] });
    await waitFor(() => expect(client.isFetching()).toBe(0));
    expect(notes).toHaveValue("My draft");
    expect(within(form).getByLabelText(/^5 · click/)).toBeChecked();
    expect(within(form).getByLabelText(/^2 · click/)).toBeChecked();
    expect(within(form).getByText("Unsaved changes")).toBeInTheDocument();
  });

  it("asks before leaving the page with unsaved changes", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const { user, form } = await openAssignment();
    await user.type(within(form).getByLabelText("Outcome and build notes"), " More.");
    await user.click(screen.getByRole("link", { name: "CaseDesk" }));
    expect(confirm).toHaveBeenCalledWith(expect.stringMatching(/unsaved changes/));
    expect(screen.getByRole("form", { name: "Assignment" })).toBeInTheDocument();
    expect(window.location.pathname).toBe(RECORDING);
  });
});

describe("a stale save", () => {
  it("shows yours next to theirs, and keeping yours saves on their revision", async () => {
    const bodies: { expected_revision?: number; notes?: string }[] = [];
    server.use(on("saveAssignment", async ({ request }) => {
      const body = (await request.json()) as { expected_revision?: number; notes?: string };
      bodies.push(body);
      if (body.expected_revision === 2) return conflict({ revision: 3, notes: "Theirs.", selected: [E(1)] });
      return HttpResponse.json({ ...example<object>("saveAssignment"), revision: 4 } as never);
    }));
    const { user, form } = await openAssignment();
    const notes = within(form).getByLabelText("Outcome and build notes");
    await user.clear(notes);
    await user.type(notes, "Mine.");
    await user.click(within(form).getByRole("button", { name: "Save assignment" }));

    const compare = await within(form).findByRole("alert", { name: "Compare changes" });
    const rows = within(within(compare).getByRole("table")).getAllByRole("row").slice(1).map((r) => r.textContent);
    expect(rows).toContain("NotesMine.Theirs.");
    expect(rows).toContain("Selected events2 events1 event");
    expect(notes).toHaveValue("Mine.");  // nothing lost

    await user.click(within(compare).getByRole("button", { name: "Keep mine" }));
    await screen.findByRole("heading", { level: 1, name: /Billing Desk/ });
    expect(bodies.map((b) => [b.expected_revision, b.notes])).toEqual([[2, "Mine."], [3, "Mine."]]);
  });

  it("can take theirs instead", async () => {
    server.use(on("saveAssignment", () => conflict({ revision: 3, notes: "Theirs.", selected: [E(1)] })));
    const { user, form } = await openAssignment();
    const notes = within(form).getByLabelText("Outcome and build notes");
    await user.clear(notes);
    await user.type(notes, "Mine.");
    await user.click(within(form).getByRole("button", { name: "Save assignment" }));
    const compare = await within(form).findByRole("alert", { name: "Compare changes" });
    await user.click(within(compare).getByRole("button", { name: "Take theirs" }));
    expect(notes).toHaveValue("Theirs.");
    expect(within(form).getByLabelText(/^2 · click/)).not.toBeChecked();
    expect(within(form).queryByRole("alert", { name: "Compare changes" })).not.toBeInTheDocument();
    expect(within(form).queryByText("Unsaved changes")).not.toBeInTheDocument();
  });

  it("works the same for a purpose edited in two tabs", async () => {
    const sent: unknown[] = [];
    server.use(on("updateApp", async ({ request }) => {
      sent.push(await request.json());
      return HttpResponse.json({ type: "about:blank", title: "Conflict", status: 409, code: "conflict", detail: "Someone else changed this application.",
        current: { ...example<object>("getApp"), revision: 4, purpose: "Their purpose" } } as never,
      { status: 409, headers: { "Content-Type": "application/problem+json" } });
    }));
    const { user } = renderApp("/apps/1", "owner");
    const form = await screen.findByRole("form", { name: "Purpose" });
    const input = within(form).getByLabelText(/Purpose/);
    await user.clear(input);
    await user.type(input, "My purpose");
    await user.click(within(form).getByRole("button", { name: "Save purpose" }));
    const compare = await within(form).findByRole("alert", { name: "Compare changes" });
    expect(within(compare).getByRole("row", { name: /Purpose/ })).toHaveTextContent("PurposeMy purposeTheir purpose");
    expect(sent).toEqual([{ purpose: "My purpose", expected_revision: 3 }]);
  });
});

describe("a failed refresh", () => {
  it("keeps the loaded page with a banner, and Retry refreshes it", async () => {
    const { user, client } = renderApp("/apps/1", "owner");
    expect(await screen.findByRole("heading", { level: 1, name: /Billing Desk/ })).toBeInTheDocument();
    const failing = on("getApp", () => HttpResponse.json({ type: "about:blank", title: "Service Unavailable", status: 503, detail: "The portal is restarting." } as never,
      { status: 503, headers: { "Content-Type": "application/problem+json" } }));
    server.use(failing);
    await client.invalidateQueries({ queryKey: ["app", 1] });
    const banner = await screen.findByText(/Could not refresh/);
    expect(banner.closest("[role=alert]")).toHaveTextContent(/The portal is restarting\. Showing what was loaded at/);
    expect(screen.getByRole("heading", { level: 1, name: /Billing Desk/ })).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Purpose" })).toBeInTheDocument();

    server.resetHandlers();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.queryByText(/Could not refresh/)).not.toBeInTheDocument());
  });
});

describe("after a change", () => {
  it("refreshes only the affected query families", async () => {
    const { user, client, form } = await openAssignment();
    client.setQueryData(["users"], { users: [] });
    client.setQueryData(["setup", "builds"], {});
    client.setQueryData(["apps", {}, 0], { apps: [] });
    await user.click(within(form).getByLabelText(/^5 · click/));
    await user.click(within(form).getByRole("button", { name: "Save assignment" }));
    await screen.findByRole("heading", { level: 1, name: /Billing Desk/ });
    expect(client.getQueryState(["users"])?.isInvalidated).toBe(false);
    expect(client.getQueryState(["setup", "builds"])?.isInvalidated).toBe(false);
    expect(client.getQueryState(["apps", {}, 0])?.isInvalidated).toBe(true);
  });

  it("maps each page to its own families", () => {
    expect(affectedFamilies("/apps/4/setup")).toEqual([["app", 4], ["apps"]]);
    expect(affectedFamilies("/recordings/ws%201")).toEqual([["recording", "ws 1"], ["recordings"], ["apps"]]);
    expect(affectedFamilies("/releases/5")).toEqual([["release", 5], ["app"], ["apps"]]);
    for (const path of ["/", "/machines", "/builds/7", "/setup/builds", "/admin/users"]) {
      expect(affectedFamilies(path).length).toBeGreaterThan(0);
      expect(affectedFamilies(path)).not.toContainEqual([]);
    }
  });
});

describe("removing an assignment", () => {
  it("asks, sends the revision, and says what it means", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    let current: Assignment = example<Assignment>("getAssignment");
    serveAssignment(() => current);
    let url = "";
    server.use(on("removeAssignment", ({ request }) => {
      url = request.url;
      current = example<Assignment>("removeAssignment");
      return HttpResponse.json(current as never);
    }));
    const { user, form } = await openAssignment();
    expect(within(form).getByText(/next draft package of CaseDesk leaves out all of this recording/)).toBeInTheDocument();
    await user.click(within(form).getByRole("button", { name: "Remove this app's assignment from the recording" }));
    expect(await within(form).findByText(/Removed from this recording: CaseDesk uses none of its events or notes/)).toBeInTheDocument();
    expect(new URL(url).searchParams.get("expected_revision")).toBe("2");
    expect(within(form).getByLabelText(/^1 · click/)).not.toBeChecked();
    expect(within(form).queryByRole("button", { name: /Remove this app/ })).not.toBeInTheDocument();
  });
});

describe("the applications page", () => {
  it("searches, filters, sorts and shows a compact table", async () => {
    const queries: URLSearchParams[] = [];
    server.use(on("listApps", ({ request }) => {
      queries.push(new URL(request.url).searchParams);
      return HttpResponse.json({ ...example<object>("listApps"), page: { total: 120, limit: 50, offset: Number(new URL(request.url).searchParams.get("offset") ?? 0), next_offset: 50 } } as never);
    }));
    const { user } = renderApp("/", "owner");
    await screen.findByRole("heading", { level: 2, name: /Billing Desk/ });
    const find = within(screen.getByRole("search", { name: "Find applications" }));
    await user.type(find.getByRole("searchbox", { name: "Search" }), "bill");
    await waitFor(() => expect(queries.at(-1)?.get("q")).toBe("bill"));
    await user.selectOptions(find.getByLabelText("Owner"), "ana");
    await user.selectOptions(find.getByLabelText("Status"), "discovered");
    await user.selectOptions(find.getByLabelText("Sort"), "-stage");
    await waitFor(() => expect(Object.fromEntries(queries.at(-1) ?? [])).toMatchObject({ q: "bill", owner: "ana", status: "discovered", sort: "-stage", limit: "50", offset: "0" }));

    await user.click(screen.getByRole("button", { name: "Table" }));
    const table = await screen.findByRole("table", { name: "Applications" });
    expect(within(table).getByRole("rowheader", { name: /Billing Desk/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Table" })).toHaveAttribute("aria-pressed", "true");

    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(queries.at(-1)?.get("offset")).toBe("50"));
    expect(window.location.search).toMatch(/view=table/);
  });
});

describe("a recording with 2,000 events", () => {
  it("renders one page of rows, searches on the server and selects every match at once", async () => {
    const events = Array.from({ length: 2000 }, (_, i) => ({
      event_id: `0b1c2d3e-0000-4000-8000-${String(i + 1).padStart(12, "0")}`, seq: i + 1, action: i % 3 ? "click" : "type",
      source: i % 2 ? "billing.example.test" : "CaseDesk.exe", label: `Control ${i + 1}`, screen: `Screen ${i % 40}`,
      time: "2026-11-15T10:40:01.000Z", in_gap: false,
    }));
    const detail = example<{ events: unknown[]; events_total: number }>("getRecording");
    detail.events = events.slice(0, 200);
    detail.events_total = events.length;
    server.use(
      on("getRecording", () => HttpResponse.json(detail as never)),
      on("getAssignment", () => HttpResponse.json({ ...example<object>("getAssignment"), selected: [] } as never)),
      on("listRecordingEvents", ({ request }) => {
        const p = new URL(request.url).searchParams;
        const words = (p.get("q") ?? "").toLowerCase().split(/\s+/).filter(Boolean);
        const found = events.filter((e) => words.every((w) => `${e.action} ${e.screen} ${e.source}`.toLowerCase().includes(w)));
        const limit = Number(p.get("limit") ?? 100);
        const offset = Number(p.get("offset") ?? 0);
        return HttpResponse.json({
          events: found.slice(offset, offset + limit), sources: [{ source: "CaseDesk.exe", count: 1000 }, { source: "billing.example.test", count: 1000 }],
          page: { total: found.length, limit, offset, next_offset: offset + limit < found.length ? offset + limit : null },
          ...(p.get("ids") === "true" ? { ids: found.map((e) => e.event_id) } : {}),
        } as never);
      }),
    );
    const { user } = renderApp(RECORDING, "owner");
    await user.selectOptions(await screen.findByLabelText("Demo app"), "CaseDesk");
    await user.click(screen.getByRole("button", { name: "Open assignment" }));
    const table = await screen.findByRole("table", { name: "Recording events" });
    expect(within(table).getAllByRole("row")).toHaveLength(101);  // a header and one page
    expect(screen.getByText("2000 matching events")).toBeInTheDocument();
    expect(within(screen.getByRole("navigation", { name: "Event pages" })).getByRole("status")).toHaveTextContent("1–100 of 2000");

    const started = performance.now();
    await user.click(screen.getByRole("button", { name: "Select all 2000 matching" }));
    expect(await screen.findByText(/^2000 events selected/)).toBeInTheDocument();
    expect(performance.now() - started).toBeLessThan(3000);
    expect(within(table).getAllByRole("checkbox").every((c) => (c as HTMLInputElement).checked)).toBe(true);

    await user.type(screen.getByRole("searchbox", { name: "Search events" }), "screen 39");
    await waitFor(() => expect(screen.getByText("50 matching events")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Clear 50 matching" }));
    expect(await screen.findByText(/^1950 events selected/)).toBeInTheDocument();
  });
});
