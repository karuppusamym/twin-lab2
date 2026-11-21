import { type ReactNode } from "react";
import { plural } from "../lib/format";
import { ErrorText, Pager } from "./ui";

/** One recorded event as the chooser shows it. `flow` is what the event belongs to: its application
 * on a recording's page, its recording on a scenario. */
export interface ChooserEvent {
  id: string;
  seq: number;
  action: string;
  label?: string | null;
  screen?: string | null;
  flow: string;
  time?: string | null;
  note?: ReactNode;
}

export interface Facet { value: string; label: string; count: number }

/** A filter of the chooser: "" is every value. */
export interface FacetFilter { label: string; value: string; options: Facet[]; onChange: (value: string) => void }

type Page = { total: number; limit: number; offset: number; next_offset?: number | null };

/** Consecutive events on the same screen of the same flow: what the owner sees as one step of the process. */
function runs(events: ChooserEvent[]) {
  const out: { key: string; flow: string; screen: string | null; events: ChooserEvent[] }[] = [];
  for (const ev of events) {
    const last = out[out.length - 1];
    const screen = ev.screen ?? null;
    if (last && last.flow === ev.flow && last.screen === screen) last.events.push(ev);
    else out.push({ key: `${ev.id}`, flow: ev.flow, screen, events: [ev] });
  }
  return out;
}

/**
 * The one way to pick recorded events: a search, a filter by flow (application or recording) and by
 * screen, and the events grouped by screen in recording order, each group selectable at once.
 * Used by a recording's "Assign events" and by a scenario's linked events; the caller filters and
 * pages (the server for a recording, in memory for a scenario).
 */
export function EventChooser({
  legend, events, page, selected, onSelect, q, onQ, flow, screen, onSelectMatching, matchingBusy, matchingError,
  onOffset, loading, hint,
}: {
  legend: string;
  events: ChooserEvent[];
  page: Page;
  selected: Set<string>;
  onSelect: (ids: string[], on: boolean) => void;
  q: string;
  onQ: (q: string) => void;
  flow?: FacetFilter;
  screen: FacetFilter;
  /** Select (or clear) every event matching the filters, on every page. */
  onSelectMatching: (on: boolean) => void;
  matchingBusy?: boolean;
  matchingError?: unknown;
  onOffset: (offset: number) => void;
  loading?: boolean;
  hint?: ReactNode;
}) {
  const facet = (f: FacetFilter, all: string) => (
    <label>{f.label}
      <select value={f.value} onChange={(e) => f.onChange(e.target.value)}>
        <option value="">{all}</option>
        {f.options.map((o) => <option key={o.value} value={o.value}>{o.label} ({o.count})</option>)}
      </select>
    </label>
  );
  const groups = runs(events);
  const timed = events.some((e) => e.time);
  const columns = timed ? 6 : 5;
  return (
    <fieldset className="event-picker" aria-busy={loading || undefined}>
      <legend>{legend}</legend>
      {hint && <p className="muted">{hint}</p>}
      <div className="filters">
        <label>Search events <input type="search" value={q} placeholder="Action, label, screen or time" onChange={(e) => onQ(e.target.value)} /></label>
        {flow && facet(flow, "All")}
        {facet(screen, "All screens")}
      </div>
      <div className="actions">
        <span className="muted" role="status">{plural(page.total, "matching event")} · {selected.size} selected</span>
        <button type="button" className="secondary" disabled={matchingBusy || page.total === 0} onClick={() => onSelectMatching(true)}>Select all {page.total} matching</button>
        <button type="button" className="secondary" disabled={matchingBusy || page.total === 0} onClick={() => onSelectMatching(false)}>Clear {page.total} matching</button>
      </div>
      <ErrorText error={matchingError} />
      {events.length > 0 && (
        <div className="scroll">
          <table className="flow events-table" aria-label={legend}>
            <thead><tr>
              <th scope="col"><span className="visually-hidden">Selected</span></th><th scope="col">#</th><th scope="col">Action</th>
              <th scope="col">Label</th>{timed && <th scope="col">Time</th>}<th scope="col"><span className="visually-hidden">Notes</span></th>
            </tr></thead>
            {groups.map((g) => {
              const ids = g.events.map((e) => e.id);
              const all = ids.every((id) => selected.has(id));
              const where = g.screen ?? "No screen";
              return (
                <tbody key={g.key} className="event-group">
                  <tr className="group-row">
                    <th scope="rowgroup" colSpan={columns}>
                      <span className="group-title">{where}</span> <span className="muted">{g.flow} · {plural(ids.length, "event")}</span>{" "}
                      <button type="button" className="link-button" onClick={() => onSelect(ids, !all)}
                        aria-label={`${all ? "Clear" : "Select"} the ${plural(ids.length, "event")} on ${where} (${g.flow})`}>
                        {all ? "Clear these" : "Select these"}
                      </button>
                    </th>
                  </tr>
                  {g.events.map((ev) => (
                    <tr key={ev.id} className={selected.has(ev.id) ? "selected" : undefined}>
                      <td><input type="checkbox" aria-label={`${ev.seq} · ${ev.action}${ev.label ? ` ${ev.label}` : ""} on ${where}`}
                        checked={selected.has(ev.id)} onChange={(e) => onSelect([ev.id], e.target.checked)} /></td>
                      <td>{ev.seq}</td><td>{ev.action}</td><td>{ev.label ?? ""}</td>
                      {timed && <td>{ev.time ? <time dateTime={ev.time}>{ev.time.slice(11, 19)}</time> : ""}</td>}
                      <td>{ev.note}</td>
                    </tr>
                  ))}
                </tbody>
              );
            })}
          </table>
        </div>
      )}
      {events.length === 0 && !loading && <p className="muted">No event matches.</p>}
      <Pager page={page} onOffset={onOffset} label={`${legend}: pages`} />
    </fieldset>
  );
}

/** Filtering and paging a list held in memory the way the server does for a recording's events. */
export function filterEvents(events: ChooserEvent[], { q, flow, screen }: { q: string; flow: string; screen: string }) {
  const words = q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const byFlow = flow ? events.filter((e) => e.flow === flow) : events;
  const matching = byFlow.filter((e) => (!screen || (e.screen ?? NO_SCREEN) === screen)
    && words.every((w) => [e.action, e.label, e.screen, e.flow, e.time].join(" ").toLowerCase().includes(w)));
  return { byFlow, matching };
}

/** The filter value of the events with no screen ("" is every screen). */
export const NO_SCREEN = "none:";

/** Counts per value, in first-seen order; a missing value counts as `NO_SCREEN`, labelled `none`. */
export function facets(values: (string | null | undefined)[], none: string): Facet[] {
  const counts = new Map<string, number>();
  for (const v of values) counts.set(v || NO_SCREEN, (counts.get(v || NO_SCREEN) ?? 0) + 1);
  return [...counts].map(([value, count]) => ({ value, label: value === NO_SCREEN ? none : value, count }));
}
