// The one mapping of scope statuses (the API's AppStatus) to the words the pages use. The server
// keeps the same table (journey.STATUS_LABELS) and sends it as `labels` on the workboard and the
// triage; api/openapi.yaml (AppStatus) documents it.
//
//   known          → "Replicate"                   decision `replicate`
//   discovered     → "Needs a scope decision"      (the state before a decision: never a choice)
//   not_replicated → "Not replicated (used only)"  decision `not_replicated`
//
// "Same as …" is not a status: it merges the application into another one (the merge page).
import type { Schemas } from "../api/client";

export type Scope = Schemas["AppStatus"];
export type Decision = Schemas["TriageDecision"]["decision"];

export const SCOPE_LABELS: Record<Scope, string> = {
  known: "Replicate",
  discovered: "Needs a scope decision",
  not_replicated: "Not replicated (used only)",
};

export const DECISION_OF: Record<Decision, Scope> = { replicate: "known", not_replicated: "not_replicated" };

/** What an owner may choose (the app page's scope select, the triage's buttons). */
export const OWNER_CHOICES: Scope[] = ["known", "not_replicated"];

export function scopeLabel(status: string): string {
  return SCOPE_LABELS[status as Scope] ?? status.replace(/_/g, " ");
}
