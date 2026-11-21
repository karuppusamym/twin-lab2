import { useState, type FormEvent, type KeyboardEvent } from "react";
import { api, data, type Schemas } from "../api/client";
import { plural } from "../lib/format";
import { EventChooser, facets, filterEvents, type ChooserEvent } from "./EventChooser";
import { ActionButton, ConflictView, DirtyNote, ErrorText, conflictOf, useAction, useDraft, useLeaveWarning } from "./ui";

type Key = "name" | "starting" | "actions" | "expected" | "failures" | "examples" | "questions" | "notes";

const PLACEHOLDERS: Record<Key, string> = {
  name: "Create an invoice from a saved order",
  starting: "Signed in as a clerk; order 1001 exists and is unpaid.",
  actions: "Search for the order",
  expected: "An invoice for order 1001 is shown with status Open.",
  failures: "Unknown order number: 'No order found' and no invoice",
  examples: "Order number: 1001\nCustomer: Example Ltd",
  questions: "Is VAT added before or after the discount?",
  notes: "",
};

/** The parts the guided form fills: a migrated scenario has none of them, only notes. */
const GUIDED: Key[] = ["starting", "actions", "expected", "failures"];
const CHOICES_PAGE = 50;

function blank(): Schemas["ScenarioInput"] {
  return { name: "", starting: "", actions: "", expected: "", failures: "", examples: "", questions: "", notes: "", events: [], bindings: [] };
}

/** The editable fields of a saved scenario (what a draft compares). */
function fieldsOf(s: Schemas["Scenario"]): Schemas["ScenarioInput"] {
  return { name: s.name, starting: s.starting, actions: s.actions, expected: s.expected, failures: s.failures,
           examples: s.examples, questions: s.questions, notes: s.notes, events: [...(s.events ?? [])],
           bindings: [...(s.bindings ?? [])] };
}

/** What is saved: the lists without their blank lines. */
function cleaned(v: Schemas["ScenarioInput"]): Schemas["ScenarioInput"] {
  const lines = (text: string | undefined) => (text ?? "").split("\n").map((l) => l.trimEnd()).filter((l) => l.trim()).join("\n");
  const events = new Set(v.events ?? []);
  return { ...v, name: v.name.trim(), actions: lines(v.actions), failures: lines(v.failures),
           bindings: (v.bindings ?? []).filter((b) => events.has(b.event)) };
}

const TYPING = new Set(["type", "input", "fill", "select", "change"]);

/** The fixture fields of the example lines: `Field: value` or `Field (type): value`, several per line with "; ". */
export function fixtureFields(examples: string): string[] {
  const out: string[] = [];
  for (const line of examples.split("\n")) {
    for (const part of line.split(/;\s+/)) {
      const m = /^\s*([^:=()]{1,60}?)\s*(?:\([a-z]+\))?\s*[:=]\s*\S/.exec(part);
      const name = m?.[1]?.trim();
      if (name && !out.some((f) => f.toLowerCase() === name.toLowerCase())) out.push(name);
    }
  }
  return out;
}

/** Each linked step that types or selects, bound to one example field or a shared identifier. */
function Bindings({ list, values, onChange, of }: {
  list: Schemas["ScenarioList"]; values: Schemas["ScenarioInput"]; onChange: (b: Schemas["Binding"][]) => void; of: string;
}) {
  const linked = new Set(values.events ?? []);
  const steps = list.event_choices.filter((c) => linked.has(c.ref) && TYPING.has(c.action));
  const fields = fixtureFields(values.examples ?? "");
  const identifiers = list.identifiers ?? [];
  const bindings = values.bindings ?? [];
  if (!steps.length) return null;
  const current = (ref: string) => {
    const b = bindings.find((x) => x.event === ref);
    return b?.field ? `field:${b.field}` : b?.identifier ? `id:${b.identifier}` : "";
  };
  const set = (ref: string, value: string) => {
    const rest = bindings.filter((b) => b.event !== ref);
    if (value.startsWith("field:")) rest.push({ event: ref, field: value.slice(6) });
    else if (value.startsWith("id:")) rest.push({ event: ref, identifier: value.slice(3) });
    onChange(rest);
  };
  const unbound = steps.filter((s) => !current(s.ref)).length;
  return (
    <fieldset className="guided-part bindings">
      <legend>Bound values</legend>
      <p className="field-hint">The replay types a value at each step that types or selects: choose which example field (or shared identifier) it is.
        {unbound ? ` ${plural(unbound, "step")} not bound yet.` : " Every step is bound."}</p>
      {steps.map((s) => (
        <label key={s.ref}>#{s.seq} {s.action} {s.label ?? ""} <span className="muted">{s.screen ?? ""}</span>
          <select value={current(s.ref)} onChange={(e) => set(s.ref, e.target.value)} aria-label={`Value bound to step ${s.seq} of ${of}`}>
            <option value="">Not bound</option>
            {fields.length > 0 && <optgroup label="Example fields">{fields.map((f) => <option key={f} value={`field:${f}`}>{f}</option>)}</optgroup>}
            {identifiers.length > 0 && <optgroup label="Shared identifiers">{identifiers.map((i) => <option key={i.name} value={`id:${i.name}`}>{i.name} ({i.value})</option>)}</optgroup>}
          </select>
        </label>
      ))}
      {!fields.length && <p className="muted">Write the example values as “Field: value” to bind them.</p>}
    </fieldset>
  );
}

/** A numbered list kept as one line per item (the API's "one per line" text). */
function LineList({ id, value, onChange, item, add, placeholder, maxLength, of }: {
  id: string; value: string; onChange: (value: string) => void; item: string; add: string; placeholder: string; maxLength?: number;
  /** What the list belongs to, in its buttons' names ("of Create an invoice"). */
  of: string;
}) {
  const lines = value ? value.split("\n") : [""];
  const put = (next: string[]) => onChange(next.join("\n"));
  const focus = (i: number) => requestAnimationFrame(() => document.getElementById(`${id}-${i}`)?.focus());
  const insert = (at: number) => { put([...lines.slice(0, at), "", ...lines.slice(at)]); focus(at); };
  const move = (i: number, by: number) => {
    const next = [...lines];
    next.splice(i + by, 0, ...next.splice(i, 1));
    put(next); focus(i + by);
  };
  const remove = (i: number) => { put(lines.length > 1 ? lines.filter((_, j) => j !== i) : [""]); focus(Math.max(0, i - 1)); };
  const onKey = (i: number) => (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") { e.preventDefault(); insert(i + 1); }
    else if (e.key === "Backspace" && !lines[i] && lines.length > 1) { e.preventDefault(); remove(i); }
  };
  const used = value.length;
  return (
    <>
      <ol className="line-list">
        {lines.map((line, i) => (
          <li key={i}>
            <label htmlFor={`${id}-${i}`} className="visually-hidden">{item} {i + 1}</label>
            <input id={`${id}-${i}`} value={line} placeholder={i === 0 ? placeholder : undefined} onKeyDown={onKey(i)}
              maxLength={maxLength ? maxLength - (used - line.length) : undefined}
              onChange={(e) => put(lines.map((l, j) => (j === i ? e.target.value.replace(/\n/g, " ") : l)))} />
            <span className="line-tools">
              <button type="button" className="icon-button" disabled={i === 0} onClick={() => move(i, -1)} aria-label={`Move ${item.toLowerCase()} ${i + 1} of ${of} up`}>↑</button>
              <button type="button" className="icon-button" disabled={i === lines.length - 1} onClick={() => move(i, 1)} aria-label={`Move ${item.toLowerCase()} ${i + 1} of ${of} down`}>↓</button>
              <button type="button" className="icon-button" onClick={() => remove(i)} aria-label={`Remove ${item.toLowerCase()} ${i + 1} of ${of}`}>✕</button>
            </span>
          </li>
        ))}
      </ol>
      <button type="button" className="secondary" onClick={() => insert(lines.length)} aria-label={`${add} to ${of}`}>{add}</button>
    </>
  );
}

/** The app's recorded events (from every recording assigned to it) in the shared chooser, filtered in memory. */
function LinkedEvents({ choices, selected, onSelect }: { choices: Schemas["EventChoice"][]; selected: Set<string>; onSelect: (ids: string[], on: boolean) => void }) {
  const [q, setQ] = useState("");
  const [flow, setFlow] = useState("");
  const [screen, setScreen] = useState("");
  const [offset, setOffset] = useState(0);
  const all: ChooserEvent[] = choices.map((c) => ({ id: c.ref, seq: c.seq, action: c.action, label: c.label, screen: c.screen, flow: c.recording }));
  const { byFlow, matching } = filterEvents(all, { q, flow, screen });
  const page = { total: matching.length, limit: CHOICES_PAGE, offset, next_offset: offset + CHOICES_PAGE < matching.length ? offset + CHOICES_PAGE : null };
  const recordings = facets(all.map((e) => e.flow), "Unnamed recording");
  return (
    <EventChooser
      legend="Recorded events that show these steps" events={matching.slice(offset, offset + CHOICES_PAGE)} page={page}
      selected={selected} onSelect={onSelect} q={q} onQ={(v) => { setQ(v); setOffset(0); }}
      flow={recordings.length > 1 ? { label: "Recording", value: flow, options: recordings, onChange: (v) => { setFlow(v); setScreen(""); setOffset(0); } } : undefined}
      screen={{ label: "Screen", value: screen, options: facets(byFlow.map((e) => e.screen), "No screen"), onChange: (v) => { setScreen(v); setOffset(0); } }}
      onSelectMatching={(on) => onSelect(matching.map((e) => e.id), on)} onOffset={setOffset}
      hint="Link the events of the recordings that show this scenario: the package gives them to the builder as its example."
    />
  );
}

/** One business scenario (or a new one), as a guided form: starting conditions, steps, expected result, failure cases. */
export function ScenarioForm({ appId, list, scenario }: { appId: number; list: Schemas["ScenarioList"]; scenario?: Schemas["Scenario"] }) {
  // follows the saved scenario until the user edits it; a save names the revision it started from
  const draft = useDraft<Schemas["ScenarioInput"]>(scenario ? fieldsOf(scenario) : blank(), scenario?.revision ?? 0);
  useLeaveWarning(draft.dirty);
  const values = draft.value;
  const setValues = draft.set;
  const formId = scenario ? `scenario-${scenario.id}` : "scenario-new";
  const families = [["app", appId]];
  const save = useAction(async (revision: number) => {
    const body = cleaned(values);
    if (scenario) {
      return data(await api.PUT("/api/v1/apps/{app_id}/scenarios/{scenario_id}", { params: { path: { app_id: appId, scenario_id: scenario.id } }, body: { ...body, expected_revision: revision } }));
    }
    return data(await api.POST("/api/v1/apps/{app_id}/scenarios", { params: { path: { app_id: appId } }, body }));
  }, (saved) => { if (scenario) draft.reset(fieldsOf(saved), saved.revision); else draft.reset(blank(), 0); }, families);
  const theirs = conflictOf<Schemas["Scenario"]>(save.error);
  const set = (key: Key) => (value: string) => setValues((v) => ({ ...v, [key]: value }));
  const events = new Set(values.events ?? []);
  const select = (ids: string[], on: boolean) => setValues((v) => {
    const next = new Set(v.events ?? []);
    for (const id of ids) if (on) next.add(id); else next.delete(id);
    return { ...v, events: [...next] };
  });
  const submit = (e: FormEvent) => { e.preventDefault(); save.mutate(draft.revision); };
  const field = (key: Key) => list.fields.find((f) => f.key === key);
  const label = (key: Key, fallback: string) => field(key)?.label ?? fallback;
  const max = (key: Key) => field(key)?.max_length;
  const fields = list.fields.filter((f) => f.key in PLACEHOLDERS) as (Schemas["ScenarioField"] & { key: Key })[];
  // judged on the saved scenario, so the notes do not move while the owner sorts them into steps
  const migrated = Boolean(scenario?.notes?.trim()) && GUIDED.every((k) => !(scenario?.[k] ?? "").trim());
  const title = scenario ? `the scenario ${scenario.name}` : "the new scenario";
  const steps = (values.actions ?? "").split("\n").filter((l) => l.trim()).length;
  /** A text field; `hidden` when its section's title already names it (the label stays for screen readers). */
  const area = (key: Key, rows: number, hint?: string, hidden?: boolean) => (
    <label>{hidden ? <span className="visually-hidden">{label(key, key)}</span> : label(key, key)}{hint && <span className="field-hint">{hint}</span>}
      <textarea rows={rows} maxLength={max(key)} value={values[key] ?? ""} placeholder={PLACEHOLDERS[key]} onChange={(e) => set(key)(e.target.value)} />
    </label>
  );
  const notes = (
    <fieldset className={`guided-part${migrated ? " migrated" : ""}`}>
      <legend>{migrated ? "Notes carried over from the old requirements" : "Notes"}</legend>
      {migrated && <p className="field-hint">This scenario was made from the application&apos;s earlier free-text requirements. Move each part into the steps of the guided form; the text stays here until you remove it.</p>}
      <label><span className="visually-hidden">Notes</span>
        <textarea rows={values.notes ? Math.min(12, Math.max(4, (values.notes ?? "").split("\n").length + 1)) : 2} maxLength={max("notes")}
          value={values.notes ?? ""} onChange={(e) => set("notes")(e.target.value)} />
      </label>
    </fieldset>
  );
  return (
    <details id={formId} className="scenario" open={!scenario && list.scenarios.length === 0}>
      <summary>
        {scenario ? (
          <><strong>{scenario.name}</strong> <span className="muted">{plural(steps, "step")} · {plural((scenario.events ?? []).length, "linked event")}{scenario.expected ? "" : " · expected result missing"}{migrated ? " · notes to sort into steps" : ""}</span></>
        ) : <strong>Add a scenario</strong>} <DirtyNote dirty={draft.dirty} />
      </summary>
      <form onSubmit={submit} aria-label={scenario ? `Scenario ${scenario.name}` : "New scenario"} className="guided">
        <label>{label("name", "Scenario name")}
          <input required maxLength={max("name")} value={values.name} placeholder={PLACEHOLDERS.name} onChange={(e) => set("name")(e.target.value)} />
        </label>
        {migrated && notes}
        <fieldset className="guided-part">
          <legend><span className="step-number" aria-hidden="true">1</span> Starting conditions</legend>
          {area("starting", 2, "What is true before the first step: who is signed in, which records exist.", true)}
          {area("examples", 3, "Made-up values the steps use, one per line. Never real customer data.")}
        </fieldset>
        <fieldset className="guided-part">
          <legend><span className="step-number" aria-hidden="true">2</span> Steps</legend>
          <p className="field-hint">What the user does, in order. Enter adds the next step.</p>
          <LineList id={`${formId}-step`} value={values.actions ?? ""} onChange={set("actions")} item="Step" of={title} add="Add a step" placeholder={PLACEHOLDERS.actions} maxLength={max("actions")} />
          {list.event_choices.length
            ? <LinkedEvents choices={list.event_choices} selected={events} onSelect={select} />
            : <p className="muted">Assign a recording&apos;s events to this application on the recording&apos;s page to link them to these steps.</p>}
        </fieldset>
        <Bindings list={list} values={values} of={title} onChange={(b) => setValues((v) => ({ ...v, bindings: b }))} />
        <fieldset className="guided-part">
          <legend><span className="step-number" aria-hidden="true">3</span> Expected result</legend>
          {area("expected", 2, "What the user sees when the scenario succeeds: the business outcome, not the clicks.", true)}
        </fieldset>
        <fieldset className="guided-part">
          <legend><span className="step-number" aria-hidden="true">4</span> Failure cases</legend>
          <p className="field-hint">Each thing that can go wrong, and what the application does then.</p>
          <LineList id={`${formId}-failure`} value={values.failures ?? ""} onChange={set("failures")} item="Failure case" of={title} add="Add a failure case" placeholder={PLACEHOLDERS.failures} maxLength={max("failures")} />
        </fieldset>
        <fieldset className="guided-part">
          <legend>Open questions</legend>
          {area("questions", 2, "What you do not know yet, one per line: the builder asks before guessing.", true)}
        </fieldset>
        {!migrated && notes}
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
      {scenario && <ActionButton destructive label="Remove scenario" name={`Remove the scenario ${scenario.name}`} confirm={`Remove the scenario ${scenario.name}?`} invalidate={families} run={async () => {
        await api.DELETE("/api/v1/apps/{app_id}/scenarios/{scenario_id}", { params: { path: { app_id: appId, scenario_id: scenario.id }, query: { expected_revision: scenario.revision } } });
      }} />}
    </details>
  );
}
