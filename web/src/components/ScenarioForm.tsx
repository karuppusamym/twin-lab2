import { type FormEvent } from "react";
import { api, data, type Schemas } from "../api/client";
import { plural } from "../lib/format";
import { ActionButton, ConflictView, DirtyNote, ErrorText, conflictOf, useAction, useDraft, useLeaveWarning } from "./ui";

type Key = "name" | "starting" | "actions" | "expected" | "failures" | "examples" | "questions" | "notes";

const PLACEHOLDERS: Record<Key, string> = {
  name: "Create an invoice from a saved order",
  starting: "Signed in as a clerk; order 1001 exists and is unpaid.",
  actions: "Search for the order\nOpen it and press Invoice",
  expected: "An invoice for order 1001 is shown with status Open.",
  failures: "Unknown order number: 'No order found' and no invoice",
  examples: "Order number: 1001\nCustomer: Example Ltd",
  questions: "Is VAT added before or after the discount?",
  notes: "",
};
const ROWS: Record<Key, number> = { name: 1, starting: 2, actions: 4, expected: 2, failures: 3, examples: 3, questions: 2, notes: 2 };

function blank(): Schemas["ScenarioInput"] {
  return { name: "", starting: "", actions: "", expected: "", failures: "", examples: "", questions: "", notes: "", events: [] };
}

/** The editable fields of a saved scenario (what a draft compares). */
function fieldsOf(s: Schemas["Scenario"]): Schemas["ScenarioInput"] {
  return { name: s.name, starting: s.starting, actions: s.actions, expected: s.expected, failures: s.failures,
           examples: s.examples, questions: s.questions, notes: s.notes, events: [...(s.events ?? [])] };
}

/** One business scenario (or a new one), the old _scenario.html. */
export function ScenarioForm({ appId, list, scenario }: { appId: number; list: Schemas["ScenarioList"]; scenario?: Schemas["Scenario"] }) {
  // follows the saved scenario until the user edits it; a save names the revision it started from
  const draft = useDraft<Schemas["ScenarioInput"]>(scenario ? fieldsOf(scenario) : blank(), scenario?.revision ?? 0);
  useLeaveWarning(draft.dirty);
  const values = draft.value;
  const setValues = draft.set;
  const formId = scenario ? `scenario-${scenario.id}` : "scenario-new";
  const families = [["app", appId]];
  const save = useAction(async (revision: number) => {
    if (scenario) {
      return data(await api.PUT("/api/v1/apps/{app_id}/scenarios/{scenario_id}", { params: { path: { app_id: appId, scenario_id: scenario.id } }, body: { ...values, expected_revision: revision } }));
    }
    return data(await api.POST("/api/v1/apps/{app_id}/scenarios", { params: { path: { app_id: appId } }, body: values }));
  }, (saved) => { if (scenario) draft.reset(fieldsOf(saved), saved.revision); else draft.reset(blank(), 0); }, families);
  const theirs = conflictOf<Schemas["Scenario"]>(save.error);
  const set = (key: Key, value: string) => setValues((v) => ({ ...v, [key]: value }));
  const events = new Set(values.events ?? []);
  const toggle = (ref: string) => {
    const next = new Set(events);
    if (next.has(ref)) next.delete(ref); else next.add(ref);
    setValues((v) => ({ ...v, events: [...next] }));
  };
  const submit = (e: FormEvent) => { e.preventDefault(); save.mutate(draft.revision); };
  const fields = list.fields.filter((f) => f.key in PLACEHOLDERS) as (Schemas["ScenarioField"] & { key: Key })[];
  return (
    <details id={formId} className="scenario" open={!scenario && list.scenarios.length === 0}>
      <summary>
        {scenario ? (
          <><strong>{scenario.name}</strong> <span className="muted">{plural((scenario.events ?? []).length, "linked event")}{scenario.expected ? "" : " · expected result missing"}</span></>
        ) : <strong>Add a scenario</strong>} <DirtyNote dirty={draft.dirty} />
      </summary>
      <form onSubmit={submit} aria-label={scenario ? `Scenario ${scenario.name}` : "New scenario"}>
        {fields.map((f) => (
          <div key={f.key}>
            <label>
              {f.label}{" "}
              {f.key === "name"
                ? <input required maxLength={f.max_length} value={values.name} placeholder={PLACEHOLDERS.name} onChange={(e) => set("name", e.target.value)} />
                : <textarea rows={f.key === "notes" && values.notes ? 6 : ROWS[f.key]} maxLength={f.max_length} value={values[f.key] ?? ""} placeholder={PLACEHOLDERS[f.key]} onChange={(e) => set(f.key, e.target.value)} />}
            </label>
            {f.key === "actions" && (list.event_choices.length ? (
              <fieldset>
                <legend>Recorded events that show these actions</legend>
                <div className="events scenario-events">
                  {list.event_choices.map((ev) => (
                    <label key={ev.ref}>
                      <input type="checkbox" checked={events.has(ev.ref)} onChange={() => toggle(ev.ref)} />
                      <span>{ev.seq} · {ev.action}{ev.label && <> · <code>{ev.label}</code></>}{ev.screen && <span className="muted"> on {ev.screen}</span>} <span className="muted">({ev.recording})</span></span>
                    </label>
                  ))}
                </div>
              </fieldset>
            ) : <p className="muted">Select events on a recording's page to link them to scenario actions.</p>)}
          </div>
        ))}
        {theirs ? (
          <ConflictView
            busy={save.isPending}
            rows={[...fields.map((f) => ({ label: f.label, mine: (values[f.key] ?? "").trim(), theirs: (theirs[f.key] ?? "").trim() })),
                   { label: "Linked events", mine: (values.events ?? []).join(", "), theirs: (theirs.events ?? []).join(", ") }]}
            onKeepMine={() => save.mutate(theirs.revision)}
            onTakeTheirs={() => { draft.reset(fieldsOf(theirs), theirs.revision); save.reset(); }}
          />
        ) : <ErrorText error={save.error} />}
        <button disabled={save.isPending}>{scenario ? "Save scenario" : "Add scenario"}</button>
      </form>
      {scenario && <ActionButton destructive label="Remove scenario" confirm={`Remove the scenario ${scenario.name}?`} invalidate={families} run={async () => {
        await api.DELETE("/api/v1/apps/{app_id}/scenarios/{scenario_id}", { params: { path: { app_id: appId, scenario_id: scenario.id }, query: { expected_revision: scenario.revision } } });
      }} />}
    </details>
  );
}
