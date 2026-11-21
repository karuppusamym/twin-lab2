import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api, data, type Schemas } from "../api/client";
import { plural } from "../lib/format";
import { ActionButton, Badge, ErrorText, QueryView, useAction } from "./ui";

const LEVEL_TEXT: Record<string, string> = {
  documented: "documented", observed: "observed", "owner-confirmed": "owner-confirmed", "replay-tested": "replay-tested",
};
const LEVEL_HINT: Record<string, string> = {
  documented: "Only written down (a line or a keyword): not counted as covered.",
  observed: "Seen in the recording.",
  "owner-confirmed": "Set, approved or confirmed by the owner.",
  "replay-tested": "Checked by Verify against a release built from this package.",
};

/** The evidence level of each item, and per application, apart from the completeness score. */
export function EvidenceLevels({ completeness, appId }: { completeness: Schemas["PackageCompleteness"]; appId?: number }) {
  const items = (completeness.items ?? []).filter((i) => i.status !== "not_applicable");
  const levels = completeness.levels;
  if (!items.length || !levels) return null;
  const families = appId ? [["app", appId]] : undefined;
  return (
    <section aria-labelledby="evidence-h" id="evidence">
      <h3 id="evidence-h">Evidence per item</h3>
      <p id="evidence-summary">
        <strong>{completeness.coverage ?? 0}% covered</strong> (observed in the recording, confirmed by you, or tested by Verify).
        The {completeness.score}% above counts what is present; documented-only items are not covered.{" "}
        This application: {levels.missing} missing, {levels.documented} documented, {levels.observed} observed,{" "}
        {levels["owner-confirmed"]} owner-confirmed, {levels["replay-tested"]} replay-tested.
      </p>
      <div className="scroll">
        <table className="flow" aria-label="Evidence level per item">
          <thead><tr><th scope="col">Item</th><th scope="col">Severity</th><th scope="col">Evidence</th>{appId && <th scope="col">Confirm</th>}</tr></thead>
          <tbody>{items.map((i) => (
            <tr key={i.item} data-item={i.item}>
              <th scope="row">{i.title}</th>
              <td>{i.severity}</td>
              <td>{i.level
                ? <span title={LEVEL_HINT[i.level]}><Badge kind={i.level}>{LEVEL_TEXT[i.level]}</Badge></span>
                : <Badge kind="missing">missing</Badge>}</td>
              {appId && <td>{i.confirmed
                ? <ActionButton secondary label="Withdraw" name={`Withdraw the confirmation of ${i.title}`} invalidate={families}
                    run={async () => { await api.DELETE("/api/v1/apps/{app_id}/confirmations/{item}", { params: { path: { app_id: appId, item: i.item } } }); }} />
                : i.level === "documented" || i.level === "observed"
                  ? <ActionButton secondary label="Confirm" name={`Confirm ${i.title}`} invalidate={families}
                      run={async () => data(await api.PUT("/api/v1/apps/{app_id}/confirmations/{item}", { params: { path: { app_id: appId, item: i.item } }, body: { note: "" } }))} />
                  : null}</td>}
            </tr>
          ))}</tbody>
        </table>
      </div>
    </section>
  );
}

/** Approve one baseline per screen state, at a viewport, theme and scale, from the recorder's masked pictures or a Screen attachment. */
export function BaselinesSection({ appId }: { appId: number }) {
  const list = useQuery({
    queryKey: ["app", appId, "baselines"],
    queryFn: async () => data(await api.GET("/api/v1/apps/{app_id}/baselines", { params: { path: { app_id: appId } } })),
  });
  return (
    <section id="baselines" aria-labelledby="baselines-h">
      <h2 id="baselines-h">Visual baselines</h2>
      <p className="muted">Each baseline is for one screen state the scenarios reach, at one viewport, theme and scale. Pictures of one screen do not
        cover its other states.</p>
      <QueryView query={list}>{(b) => (
        b.needed.length === 0
          ? <p>No recorded screen state is linked to a scenario yet.</p>
          : <>
            <p id="baselines-summary">{b.needed.filter((n) => b.approved.some((a) => a.screen === n.screen && a.state === n.state)).length} of{" "}
              {plural(b.needed.length, "screen state")} have an approved baseline.</p>
            <ul className="checks">{b.needed.map((n) => <NeededState key={`${n.screen}|${n.state}`} appId={appId} need={n} list={b} />)}</ul>
          </>
      )}</QueryView>
    </section>
  );
}

function pictureUrl(appId: number, c: Schemas["BaselineCandidate"]) {
  const q = c.attachment_id != null ? `attachment_id=${c.attachment_id}` : `recording_id=${encodeURIComponent(c.recording_id ?? "")}&event_id=${encodeURIComponent(c.event_id ?? "")}`;
  return `/api/v1/apps/${appId}/baselines/picture?${q}`;
}

function NeededState({ appId, need, list }: { appId: number; need: Schemas["NeededState"]; list: Schemas["BaselineList"] }) {
  const approved = list.approved.filter((a) => a.screen === need.screen && a.state === need.state);
  const candidates = list.candidates.filter((c) => c.state == null || (c.screen === need.screen && c.state === need.state));
  const [choice, setChoice] = useState(0);
  const [viewport, setViewport] = useState("1280x720");
  const [theme, setTheme] = useState<"light" | "dark">("light");
  const [scale, setScale] = useState("1");
  const families = [["app", appId]];
  const approve = useAction(async () => {
    const c = candidates[choice];
    if (!c) throw new Error("Choose a picture.");
    return data(await api.POST("/api/v1/apps/{app_id}/baselines", { params: { path: { app_id: appId } }, body: {
      screen: need.screen, state: need.state, viewport, theme, scale,
      source: c.attachment_id != null ? { attachment_id: c.attachment_id } : { recording_id: c.recording_id, event_id: c.event_id },
    } }));
  }, undefined, families);
  const label = `${need.screen} state ${need.number}`;
  const id = `baseline-${need.state}`;
  const submit = (e: FormEvent) => { e.preventDefault(); approve.mutate(undefined); };
  return (
    <li className={approved.length ? undefined : "failed"} data-state={need.state}>
      <Badge kind={approved.length ? "pass" : "missing"}>{approved.length ? "approved" : "no baseline"}</Badge> <strong><code>{need.screen}</code> state {need.number}</strong>
      {approved.length > 0 && <ul>{approved.map((a) => (
        <li key={a.id}>{a.viewport}, {a.theme}, scale {a.scale} ({a.source}, by {a.approved_by}){" "}
          <ActionButton secondary label="Remove" name={`Remove the ${a.viewport} ${a.theme} baseline of ${label}`} invalidate={families}
            run={async () => { await api.DELETE("/api/v1/apps/{app_id}/baselines/{baseline_id}", { params: { path: { app_id: appId, baseline_id: a.id } } }); }} />
        </li>))}</ul>}
      {candidates.length ? (
        <form onSubmit={submit} aria-label={`Approve a baseline of ${label}`} className="inline-form">
          <label>Picture
            <select id={`${id}-pic`} value={choice} onChange={(e) => setChoice(Number(e.target.value))}>
              {candidates.map((c, i) => <option key={i} value={i}>{c.label}</option>)}
            </select>
          </label>
          {candidates[choice] && <a href={pictureUrl(appId, candidates[choice])} target="_blank" rel="noreferrer">View picture</a>}
          <label>Viewport <input value={viewport} pattern="[0-9]{2,5}x[0-9]{2,5}" size={10} onChange={(e) => setViewport(e.target.value)} /></label>
          <label>Theme
            <select value={theme} onChange={(e) => setTheme(e.target.value as "light" | "dark")}><option value="light">light</option><option value="dark">dark</option></select>
          </label>
          <label>Scale <input value={scale} size={4} onChange={(e) => setScale(e.target.value)} /></label>
          <button disabled={approve.isPending}>Approve baseline</button>
          <ErrorText error={approve.error} />
        </form>
      ) : <p className="muted">No picture of this state: attach a Screen picture and mark it reviewed, or record it again.</p>}
    </li>
  );
}

/** The interface contracts with the applications recorded together: generated, editable by the owner. */
export function InterfacesSection({ appId }: { appId: number }) {
  const list = useQuery({
    queryKey: ["app", appId, "interfaces"],
    queryFn: async () => data(await api.GET("/api/v1/apps/{app_id}/interfaces", { params: { path: { app_id: appId } } })),
  });
  return (
    <section id="interfaces" aria-labelledby="interfaces-h">
      <h2 id="interfaces-h">Interfaces with other applications</h2>
      <p className="muted">For applications recorded together: the identifiers they share, who produces and who consumes what, the order to start and
        reset them, and what a consumer does when its producer is down. Inferred from the recording; correct it here. Each package carries the
        part that concerns its application.</p>
      <QueryView query={list}>{(l) => l.contracts.length === 0
        ? <p>This application was not recorded together with another one.</p>
        : <>{l.contracts.map((c) => <Contract key={c.recording_id} appId={appId} contract={c} />)}</>}</QueryView>
    </section>
  );
}

function Contract({ appId, contract }: { appId: number; contract: Schemas["InterfaceContract"] }) {
  const [doc, setDoc] = useState<Schemas["InterfaceDoc"]>(contract.doc);
  const families = [["app", appId]];
  const name = (id: number | null | undefined) => contract.doc.apps.find((a) => a.id === id)?.name ?? String(id);
  const save = useAction(async (body: Schemas["InterfaceDoc"]) => data(await api.PUT("/api/v1/apps/{app_id}/interfaces/{recording_id}", {
    params: { path: { app_id: appId, recording_id: contract.recording_id } }, body: { doc: body, expected_revision: contract.revision },
  })), (saved) => setDoc(saved.doc), families);
  const setIdent = (i: number, patch: Partial<Schemas["SharedIdentifier"]>) =>
    setDoc((d) => ({ ...d, identifiers: d.identifiers.map((x, j) => (j === i ? { ...x, ...patch } : x)) }));
  const setRel = (i: number, patch: Partial<Schemas["InterfaceRelationship"]>) =>
    setDoc((d) => ({ ...d, relationships: d.relationships.map((x, j) => (j === i ? { ...x, ...patch } : x)) }));
  const move = (i: number, by: number) => setDoc((d) => {
    const order = [...d.launch_order];
    order.splice(i + by, 0, ...order.splice(i, 1));
    return { ...d, launch_order: order, reset_order: [...order].reverse() };
  });
  const addIdent = () => setDoc((d) => ({ ...d, identifiers: [...d.identifiers, { name: "", field: "", type: "identifier", value: "", producer: appId, consumers: d.apps.filter((a) => a.id !== appId).map((a) => a.id) }] }));
  const submit = (e: FormEvent) => {
    e.preventDefault();
    // every identifier gets its relationships (producer to each consumer) if it has none yet
    const rels = [...doc.relationships];
    for (const i of doc.identifiers) {
      for (const c of i.consumers) {
        if (i.producer != null && !rels.some((r) => r.kind === "identifier" && r.identifier === i.name && r.consumer === c)) {
          rels.push({ kind: "identifier", identifier: i.name, producer: i.producer, consumer: c, when_down: "" });
        }
      }
    }
    const next = { ...doc, relationships: rels };
    setDoc(next);
    save.mutate(next);
  };
  return (
    <form onSubmit={submit} aria-label={`Interface contract of recording ${contract.recording_id}`} className="guided contract" data-recording={contract.recording_id}>
      <p><strong>Recording {contract.recording_id}</strong> · {contract.edited ? <>edited by {contract.edited_by}</> : "inferred from the recording"} ·{" "}
        {plural(doc.apps.length, "application")}</p>
      <fieldset className="guided-part">
        <legend>Launch order (reset runs in reverse)</legend>
        <ol>{doc.launch_order.map((id, i) => (
          <li key={id}>{name(id)}{" "}
            <button type="button" className="icon-button" disabled={i === 0} onClick={() => move(i, -1)} aria-label={`Start ${name(id)} earlier`}>↑</button>
            <button type="button" className="icon-button" disabled={i === doc.launch_order.length - 1} onClick={() => move(i, 1)} aria-label={`Start ${name(id)} later`}>↓</button>
          </li>))}</ol>
      </fieldset>
      <fieldset className="guided-part">
        <legend>Shared synthetic identifiers</legend>
        {doc.identifiers.length === 0 && <p className="muted">None found: add one when an application types a value another one produced.</p>}
        {doc.identifiers.map((i, n) => (
          <div key={n} className="inline-form">
            <label>Name <input value={i.name} required onChange={(e) => setIdent(n, { name: e.target.value, field: i.field || e.target.value })} /></label>
            <label>Synthetic value <input value={i.value} onChange={(e) => setIdent(n, { value: e.target.value })} /></label>
            <label>Produced by
              <select value={i.producer ?? ""} onChange={(e) => {
                const p = Number(e.target.value);
                setIdent(n, { producer: p, consumers: doc.apps.map((a) => a.id).filter((a) => a !== p) });
              }}>{doc.apps.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}</select>
            </label>
          </div>
        ))}
        <button type="button" className="secondary" onClick={addIdent}>Add a shared identifier</button>
      </fieldset>
      <fieldset className="guided-part">
        <legend>Relationships and what happens when the producer is down</legend>
        {doc.relationships.length === 0 && <p className="muted">No producer and consumer relationship yet.</p>}
        {doc.relationships.map((r, n) => (
          <label key={n}>{name(r.producer)} → {name(r.consumer)} by {r.kind === "identifier" ? `identifier ${r.identifier}` : r.kind}
            {r.kind === "api" && r.schema ? ` (${String((r.schema as Record<string, unknown>).method ?? "")} ${String((r.schema as Record<string, unknown>).path ?? "")})` : ""}
            <span className="field-hint">What {name(r.consumer)} does when {name(r.producer)} is down</span>
            <input value={r.when_down} placeholder="Shows an error, keeps the input, retries later" onChange={(e) => setRel(n, { when_down: e.target.value })} />
          </label>
        ))}
      </fieldset>
      <ErrorText error={save.error} />
      <button disabled={save.isPending}>Save the interface contract</button>{" "}
      {contract.edited && <ActionButton secondary label="Back to the inferred contract" invalidate={families}
        run={async () => { await api.DELETE("/api/v1/apps/{app_id}/interfaces/{recording_id}", { params: { path: { app_id: appId, recording_id: contract.recording_id } } }); }} />}
    </form>
  );
}
