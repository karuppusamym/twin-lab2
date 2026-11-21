// Small shared pieces: loading and error states, badges, action buttons.
import { useMutation, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { errorText } from "../api/client";
import { capitalize, words } from "../lib/format";

export function Loading({ what = "Loading" }: { what?: string }) {
  return <p className="muted" role="status">{what}…</p>;
}

export function ErrorText({ error }: { error: unknown }) {
  if (!error) return null;
  return <p className="error" role="alert">{errorText(error)}</p>;
}

export function Notice({ children }: { children: ReactNode }) {
  if (!children) return null;
  return <p className="notice" role="status">{children}</p>;
}

/** Renders `children(data)` once a query has data; else its loading or error state. */
export function QueryView<T>({ query, children, what }: { query: UseQueryResult<T>; children: (data: T) => ReactNode; what?: string }) {
  if (query.isPending) return <Loading what={what} />;
  if (query.isError) return <ErrorText error={query.error} />;
  return <>{children(query.data)}</>;
}

export function Badge({ kind, children }: { kind?: string | null; children: ReactNode }) {
  return <span className={`badge ${kind ?? ""}`}>{children}</span>;
}

export function StateBadge({ state }: { state: string }) {
  return <Badge kind={state}>{capitalize(words(state))}</Badge>;
}

/**
 * A mutation whose success refreshes every query (the server is the source of truth) and whose
 * error is shown where the action is.
 */
export function useAction<TVars, TResult>(run: (vars: TVars) => Promise<TResult>, onDone?: (result: TResult) => void) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: run,
    onSuccess: async (result) => {
      await client.invalidateQueries();
      onDone?.(result);
    },
  });
}

/** A one-button action (the old pages' small POST forms). */
export function ActionButton({ label, run, secondary, confirm, disabled }: {
  label: string;
  run: () => Promise<unknown>;
  secondary?: boolean;
  confirm?: string;
  disabled?: boolean;
}) {
  const action = useAction(run);
  return (
    <span className="action">
      <button
        type="button"
        className={secondary ? "secondary" : undefined}
        disabled={disabled || action.isPending}
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

/** A value that follows `initial` until the user edits it (form fields filled from the server). */
export function useField(initial: string) {
  const [value, setValue] = useState(initial);
  useEffect(() => setValue(initial), [initial]);
  return [value, setValue] as const;
}
