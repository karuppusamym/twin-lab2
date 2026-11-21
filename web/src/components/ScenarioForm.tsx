import { useState, type FormEvent } from "react";
import { api, data, type Schemas } from "../api/client";
import { plural } from "../lib/format";
import { ActionButton, ErrorText, useAction } from "./ui";

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

/** One business scenario (or a new one), the old _scenario.html. */
export function ScenarioForm({ appId, list, scenario }: { appId: number; list: Schemas["ScenarioList"]; scenario?: Schemas["Scenario"] }) {
  const [values, setValues] = useState<Schemas["ScenarioInput"]>(() => (scenario ? { ...scenario } : blank()));
  const formId = scenario ? `scenario-${scenario.id}` : "scenario-new";
  const save = useAction(async () => {
    if (scenario) {
      return data(await api.PUT("/api/v1/apps/{app_id}/scenarios/{scenario_id}", { params: { path: { app_id: appId, scenario_id: scenario.id } }, body: values }));
    }
    return data(await api.POST("/api/v1/apps/{app_id}/scenarios", { params: { path: { app_id: appId } }, body: values }));
  }, () => { if (!scenario) setValues(blank()); });
  const set = (key: Key, value: string) => setValues((v) => ({ ...v, [key]: value }));
  const events = new Set(values.events ?? []);
  const toggle = (ref: string) => {
    const next = new Set(events);
    if (next.has(ref)) next.delete(ref); else next.add(ref);
    setValues((v) => ({ ...v, events: [...next] }));
  };
  const submit = (e: FormEvent) => { e.preventDefault(); save.mutate(undefined); };
  const fields = list.fields.filter((f) => f.key in PLACEHOLDERS) as (Schemas["ScenarioField"] & { key: Key })[];
  return (
    <details id={formId} className="scenario" open={!scenario && list.scenarios.length === 0}>
      <summary>
        {scenario ? (
          <><strong>{scenario.name}</strong> <span className="muted">{plural((scenario.events ?? []).length, "linked event")}{scenario.expected ? "" : " · expected result missing"}</span></>
        ) : <strong>Add a scenario</strong>}
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
        <ErrorText error={save.error} />
        <button disabled={save.isPending}>{scenario ? "Save scenario" : "Add scenario"}</button>
      </form>
      {scenario && <ActionButton secondary label="Remove scenario" confirm={`Remove the scenario ${scenario.name}?`} run={async () => {
        await api.DELETE("/api/v1/apps/{app_id}/scenarios/{scenario_id}", { params: { path: { app_id: appId, scenario_id: scenario.id } } });
      }} />}
    </details>
  );
}
