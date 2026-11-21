import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, data, type Schemas } from "../api/client";
import { ActionButton, Badge, ErrorText, QueryView, useAction, useTitle } from "../components/ui";

/** Request a recording (the old /sessions page). */
export function CapturePage() {
  useTitle("Request a recording");
  const page = useQuery({ queryKey: ["capture"], queryFn: async () => data(await api.GET("/api/v1/capture-sessions")) });
  return (
    <>
      <h1>Request a recording</h1>
      <p>A recording requested here is offered to its machine: the recorder lists it, and recording starts only when the person at the machine presses Start. The target applications are recorded in full (or steps only); every other application only as having been used.</p>
      <QueryView query={page}>
        {(p) => (
          <div className="columns">
            <section aria-labelledby="request-h">
              <h2 id="request-h">Request a recording</h2>
              {p.machines.length && p.apps.length ? <RequestForm page={p} /> : (
                <>
                  {!p.machines.length && <p>First <Link to="/machines">pair a recorder machine</Link> (Setup / Advanced).</p>}
                  {!p.apps.length && <p>First <Link to="/">add an application</Link> and give it an identity in its Setup / Advanced page.</p>}
                </>
              )}
            </section>
            <section aria-labelledby="requested-h">
              <h2 id="requested-h">Requested and recorded</h2>
              {p.sessions.length ? p.sessions.map((s) => (
                <div className="row" key={s.id}>
                  <strong>{s.name}</strong> <Badge kind={s.status}>{s.status === "offered" ? "requested" : s.status}</Badge>
                  <span className="muted">{s.machine}{s.automation && ` · ${s.automation}`} · {[...new Set(s.targets.map((t) => t.name))].join(", ")}</span>
                  {s.recording_id && <Link to={`/recordings/${encodeURIComponent(s.recording_id)}`}>recording</Link>}
                  {s.status === "offered" && (
                    <ActionButton secondary label="Withdraw" run={async () => data(await api.POST("/api/v1/capture-sessions/{session_id}/withdraw", { params: { path: { session_id: s.id } } }))} />
                  )}
                </div>
              )) : <p>No recording requested yet.</p>}
            </section>
          </div>
        )}
      </QueryView>
    </>
  );
}

function RequestForm({ page }: { page: Schemas["CapturePage"] }) {
  const [name, setName] = useState("");
  const [machine, setMachine] = useState(String(page.machines[0]?.id ?? ""));
  const [automation, setAutomation] = useState("");
  const [targets, setTargets] = useState<Record<number, "full" | "steps" | undefined>>({});
  const [levels, setLevels] = useState<Record<number, "full" | "steps">>({});
  const request = useAction(async () => data(await api.POST("/api/v1/capture-sessions", {
    body: {
      name: name.trim(), machine_id: Number(machine), automation_id: automation ? Number(automation) : null,
      targets: Object.entries(targets).filter(([, on]) => on).map(([id]) => ({ app_id: Number(id), level: levels[Number(id)] ?? "full" })),
    },
  })), () => { setName(""); setTargets({}); });
  const submit = (e: FormEvent) => { e.preventDefault(); request.mutate(undefined); };
  return (
    <form aria-label="Request a recording" onSubmit={submit}>
      <label>Recording name <input required maxLength={120} placeholder="Order to invoice" value={name} onChange={(e) => setName(e.target.value)} /></label>
      <label>Machine <select value={machine} onChange={(e) => setMachine(e.target.value)}>{page.machines.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}</select></label>
      <label>Automation (optional)
        <select value={automation} onChange={(e) => setAutomation(e.target.value)}>
          <option value="">None</option>
          {page.automations.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
      </label>
      <fieldset>
        <legend>Target applications</legend>
        {page.apps.map((app) => (
          <div className="row" key={app.id}>
            <label className="check">
              <input type="checkbox" disabled={!app.identity_count} checked={!!targets[app.id]}
                onChange={(e) => setTargets((t) => ({ ...t, [app.id]: e.target.checked ? "full" : undefined }))} /> {app.name}
            </label>
            <select aria-label={`Level for ${app.name}`} value={levels[app.id] ?? "full"} onChange={(e) => setLevels((l) => ({ ...l, [app.id]: e.target.value as "full" | "steps" }))}>
              <option value="full">full</option><option value="steps">steps only</option>
            </select>
            {!app.identity_count && <span className="muted"><Link to={`/apps/${app.id}/setup#identity`}>add an identity</Link> first</span>}
          </div>
        ))}
      </fieldset>
      <ErrorText error={request.error} />
      <button disabled={request.isPending}>Request recording</button>
    </form>
  );
}
