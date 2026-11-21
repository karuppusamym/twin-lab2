// Small shared pieces: loading and error states, badges, action buttons, and safe editing
// (drafts that follow the server until the user edits them, conflicts, the leave-page warning).
import { useMutation, useQueryClient, type QueryKey, type UseQueryResult } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { ApiError, errorText } from "../api/client";
import { RequestAccess } from "./AccessRequests";
import { capitalize, words } from "../lib/format";
import { sessionKey } from "../session";

/** A query on its way: its words for screen readers and a placeholder of the content's shape. */
export function Loading({ what = "Loading", lines = 3 }: { what?: string; lines?: number }) {
  return (
    <div className="loading" role="status" aria-busy="true">
      <p className="muted">{what}…</p>
      <div className="skeleton" aria-hidden="true">
        {Array.from({ length: lines }, (_, i) => <span key={i} className="skeleton-line" />)}
      </div>
    </div>
  );
}

/** Nothing to show yet: what is missing and, as children, what to do about it. */
export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function ErrorText({ error }: { error: unknown }) {
  if (!error) return null;
  const text = <p className="error" role="alert">{errorText(error)}</p>;
  // refused for want of a role on an application: who can grant it, and "Request access"
  if (error instanceof ApiError && error.status === 403 && error.problem.code === "role" && typeof error.problem.app_id === "number") {
    return <>{text}<RequestAccess problem={error.problem} /></>;
  }
  return text;
}

export function Notice({ children }: { children: ReactNode }) {
  if (!children) return null;
  return <p className="notice" role="status">{children}</p>;
}

function clock(ms: number) {
  return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/**
 * Renders `children(data)` once a query has data; else its loading or error state. A refetch
 * that fails keeps the loaded content (and any form in it, with its draft) and says so above it.
 */
export function QueryView<T>({ query, children, what }: { query: UseQueryResult<T>; children: (data: T) => ReactNode; what?: string }) {
  if (query.data === undefined) {
    if (query.isError) return <ErrorText error={query.error} />;
    return <Loading what={what} />;
  }
  return (
    <>
      {query.isError && (
        <div className="refresh-failed" role="alert">
          <span><b>Could not refresh</b>: {errorText(query.error)} Showing what was loaded{query.dataUpdatedAt ? ` at ${clock(query.dataUpdatedAt)}` : ""}.</span>{" "}
          <button type="button" className="secondary" disabled={query.isFetching} onClick={() => void query.refetch()}>Retry</button>
        </div>
      )}
      {children(query.data)}
    </>
  );
}

/**
 * One meaning per status family, the same everywhere: blue = active or primary, green = passed
 * or done, amber = needs review, red = failed or blocked, gray = cancelled, excluded or not run.
 * A badge always has its words too: the color is never the only signal.
 */
const TONES: Record<string, "active" | "done" | "review" | "failed" | "neutral"> = {
  known: "active", running: "active", starting: "active", queued: "active", ready_for_verification: "active",
  checking_started: "active", offered: "active", open: "active", owner: "active",
  recorded: "done", complete: "done", pass: "done", passed: "done", success: "done", tested: "done",
  published: "done", verified: "done", reviewed: "done", synthetic: "done", done: "done", succeeded: "done",
  discovered: "review", waiting_for_answer: "review", unverified: "review", attention: "review", restricted: "review",
  partial: "review", incomplete: "review", interrupted: "review", suggested: "review", draft: "review",
  fail: "failed", failed: "failed", failure: "failed", blocked: "failed", error: "failed", mismatch: "failed",
  missing: "failed", rejected: "failed",
  cancelled: "neutral", canceled: "neutral", not_replicated: "neutral", withdrawn: "neutral", packaged: "neutral",
  "not-run": "neutral", not_run: "neutral", excluded: "neutral", skipped: "neutral", removed: "neutral",
};

export function tone(kind: string | null | undefined) {
  return (kind && TONES[kind]) || "neutral";
}

export function Badge({ kind, children }: { kind?: string | null; children: ReactNode }) {
  return <span className={`badge tone-${tone(kind)} ${kind ?? ""}`.trim()}>{children}</span>;
}

export function StateBadge({ state }: { state: string }) {
  return <Badge kind={state}>{capitalize(words(state))}</Badge>;
}

/**
 * The query families a change on the page at `pathname` can affect: the page's own object and
 * the lists it appears in. A mutation refreshes these, never every query.
 */
export function affectedFamilies(pathname: string): QueryKey[] {
  const [first, id, sub] = pathname.split("/").filter(Boolean);
  switch (first) {
    case "apps": {
      const appId = Number(id);
      const own: QueryKey[] = [["app", appId], ["apps"]];
      return sub === "merge" ? [["app"], ["apps"], ["recording"], ["recordings"]] : own;
    }
    case "recordings": return id ? [["recording", decodeURIComponent(id)], ["recordings"], ["apps"]] : [["recordings"], ["apps"]];
    case "builds": return [["build", Number(id)], ["app"]];
    case "releases": return [["release", Number(id)], ["app"], ["apps"]];
    case "automations": return [["automation"], ["automations"]];
    case "sessions": return [["capture"], ["recordings"]];
    case "machines": return [["machines"], ["capture"]];
    case "setup": return [["setup"]];
    case "admin": return [["users"], sessionKey as unknown as QueryKey];
    default: return [["apps"], ["recordings"]];
  }
}

/**
 * A mutation whose success refreshes the query families it affects (the server is the source of
 * truth): `invalidate`, else the families of the current page (affectedFamilies). Its error is
 * shown where the action is.
 */
export function useAction<TVars, TResult>(run: (vars: TVars) => Promise<TResult>, onDone?: (result: TResult) => void, invalidate?: QueryKey[]) {
  const client = useQueryClient();
  const { pathname } = useLocation();
  return useMutation({
    mutationFn: run,
    onSuccess: async (result) => {
      await Promise.all((invalidate ?? affectedFamilies(pathname)).map((queryKey) => client.invalidateQueries({ queryKey })));
      onDone?.(result);
    },
  });
}

/** A one-button action (the old pages' small POST forms). */
export function ActionButton({ label, name, run, secondary, destructive, confirm, disabled, invalidate }: {
  label: string;
  /** The accessible name when the label alone is ambiguous (several "Remove" buttons). */
  name?: string;
  run: () => Promise<unknown>;
  secondary?: boolean;
  destructive?: boolean;
  confirm?: string;
  disabled?: boolean;
  invalidate?: QueryKey[];
}) {
  const action = useAction(run, undefined, invalidate);
  return (
    <span className="action">
      <button
        type="button"
        className={destructive ? "destructive" : secondary ? "secondary" : undefined}
        disabled={disabled || action.isPending}
        aria-label={name}
        onClick={() => {
          if (confirm && !window.confirm(confirm)) return;
          action.mutate(undefined);
        }}
      >
        {label}
      </button>
      {action.isError && <ErrorText error={action.error} />}
    </span>
  );
}

export function Crumbs({ children }: { children: ReactNode }) {
  return <p className="crumbs">{children}</p>;
}

/** Sets the document title like the old pages: "<what> · TwinLab2". */
export function useTitle(title: string | undefined) {
  useEffect(() => {
    document.title = title ? `${title} · TwinLab2` : "TwinLab2";
  }, [title]);
}

// -- safe editing ------------------------------------------------------------------------------

function same(a: unknown, b: unknown) {
  return a === b || JSON.stringify(a) === JSON.stringify(b);
}

export interface Draft<T> {
  /** What the form shows: the server's value until the user edits it, then the user's. */
  value: T;
  set: (next: T | ((current: T) => T)) => void;
  /** Edited and not saved (an edit back to the loaded value is not dirty). */
  dirty: boolean;
  /** The revision the draft was made from: send it as `expected_revision`. */
  revision: number;
  /** Start again from `to` (a saved or the server's version) at `revision`. */
  reset: (to: T, revision: number) => void;
}

/**
 * A form's draft of a server value. It follows `server` (a refetch, another tab's save) only
 * until the user edits it; from then on a refetch never replaces it. `revision` stays the one
 * the edit started from, so a save made from a stale copy is refused (409) instead of
 * overwriting someone else's change.
 */
export function useDraft<T>(server: T, serverRevision = 0): Draft<T> {
  const [state, setState] = useState({ value: server, base: server, revision: serverRevision });
  const dirty = !same(state.value, state.base);
  const serverKey = JSON.stringify(server);
  useEffect(() => {
    setState((s) => (same(s.value, s.base) ? { value: server, base: server, revision: serverRevision } : s));
    // the server's value by content, not identity: a refetch with the same data changes nothing
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverKey, serverRevision]);
  const set = useCallback((next: T | ((current: T) => T)) => {
    setState((s) => ({ ...s, value: typeof next === "function" ? (next as (c: T) => T)(s.value) : next }));
  }, []);
  const reset = useCallback((to: T, revision: number) => setState({ value: to, base: to, revision }), []);
  return { value: state.value, set, dirty, revision: state.revision, reset };
}

/** A form field filled from the server: follows `initial` until the user edits it. */
export function useField<T extends string>(initial: T, revision = 0) {
  const draft = useDraft<T>(initial, revision);
  return [draft.value, draft.set as (value: T) => void, draft] as const;
}

/** Shown next to a form with unsaved changes. */
export function DirtyNote({ dirty }: { dirty: boolean }) {
  if (!dirty) return null;
  return <span className="dirty" role="status"><span aria-hidden="true">●</span> Unsaved changes</span>;
}

const LEAVE = "You have unsaved changes on this page. Leave without saving them?";
let dirtyForms = 0;

/**
 * While `dirty`, leaving the page asks first: closing or reloading the tab (beforeunload) and
 * following a link inside the app.
 */
export function useLeaveWarning(dirty: boolean) {
  const counted = useRef(false);
  useEffect(() => {
    if (!dirty) return undefined;
    counted.current = true;
    dirtyForms += 1;
    const unload = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = LEAVE; return LEAVE; };
    const click = (e: MouseEvent) => {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const link = (e.target as Element | null)?.closest?.("a[href]") as HTMLAnchorElement | null;
      if (!link || link.target === "_blank" || link.hasAttribute("download")) return;
      const to = new URL(link.href, window.location.href);
      if (to.origin !== window.location.origin || (to.pathname === window.location.pathname && to.search === window.location.search)) return;
      if (!window.confirm(LEAVE)) { e.preventDefault(); e.stopPropagation(); }
    };
    window.addEventListener("beforeunload", unload);
    document.addEventListener("click", click, true);
    return () => {
      dirtyForms -= 1;
      window.removeEventListener("beforeunload", unload);
      document.removeEventListener("click", click, true);
    };
  }, [dirty]);
}

/** Forms with unsaved changes on the page now (for tests and the layout). */
export function unsavedForms() {
  return dirtyForms;
}

/** The server's current version from a 409 `conflict` (a stale save), else null. */
export function conflictOf<T = Record<string, unknown>>(error: unknown): (T & { revision: number }) | null {
  if (error instanceof ApiError && error.status === 409 && error.problem.code === "conflict" && error.problem.current) {
    return error.problem.current as T & { revision: number };
  }
  return null;
}

export interface CompareRow { label: string; mine: string; theirs: string }

/**
 * A save refused because someone changed the resource since it was opened: yours next to
 * theirs, field by field, and the choice to keep yours (save it over theirs) or take theirs.
 */
export function ConflictView({ rows, onKeepMine, onTakeTheirs, busy }: {
  rows: CompareRow[]; onKeepMine: () => void; onTakeTheirs: () => void; busy?: boolean;
}) {
  const changed = rows.filter((r) => r.mine !== r.theirs);
  return (
    <div className="conflict" role="alert" aria-label="Compare changes">
      <p><b>Someone else saved this while you were editing.</b> Nothing was overwritten. Compare the two versions, then keep yours (it replaces theirs) or take theirs (your edits are dropped).</p>
      <div className="scroll">
        <table className="flow compare">
          <thead><tr><th scope="col">Field</th><th scope="col">Yours</th><th scope="col">Theirs (saved)</th></tr></thead>
          <tbody>{(changed.length ? changed : rows).map((r) => (
            <tr key={r.label}><th scope="row">{r.label}</th><td className="notes">{r.mine || <span className="muted">empty</span>}</td><td className="notes">{r.theirs || <span className="muted">empty</span>}</td></tr>
          ))}</tbody>
        </table>
      </div>
      {changed.length === 0 && <p className="muted">Both versions have the same content.</p>}
      <div className="actions">
        <button type="button" disabled={busy} onClick={onKeepMine}>Keep mine</button>
        <button type="button" className="secondary" disabled={busy} onClick={onTakeTheirs}>Take theirs</button>
      </div>
    </div>
  );
}

/** Pagination controls for a list answered with a `page` object. */
export function Pager({ page, onOffset, label }: {
  page: { total: number; limit: number; offset: number; next_offset?: number | null }; onOffset: (offset: number) => void; label: string;
}) {
  if (page.total <= page.limit && page.offset === 0) return null;
  const first = page.total ? page.offset + 1 : 0;
  const last = Math.min(page.offset + page.limit, page.total);
  return (
    <nav className="pager" aria-label={label}>
      <button type="button" className="secondary" disabled={page.offset === 0} onClick={() => onOffset(Math.max(0, page.offset - page.limit))}>Previous</button>
      <span role="status">{first}–{last} of {page.total}</span>
      <button type="button" className="secondary" disabled={page.next_offset == null} onClick={() => onOffset(page.next_offset ?? page.offset)}>Next</button>
    </nav>
  );
}
