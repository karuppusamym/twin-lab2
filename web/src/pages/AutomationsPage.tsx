import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, data, postForm, type Schemas } from "../api/client";
import { Crumbs, Empty, ErrorText, QueryView, useAction, useTitle } from "../components/ui";
import { when } from "../lib/format";

export function AutomationsPage() {
  useTitle("Automations");
  const page = useQuery({ queryKey: ["automations"], queryFn: async () => data(await api.GET("/api/v1/automations")) });
  return (
    <>
      <Crumbs><Link to="/setup">Setup / Advanced</Link></Crumbs>
      <h1>Automations</h1>
      <QueryView query={page}>
        {(p) => (
          <div className="columns">
            <section aria-labelledby="list-h">
              <h2 id="list-h">Automations</h2>
              {p.automations.length ? p.automations.map((a) => (
                <div className="row" key={a.id}>
                  <Link to={`/automations/${a.id}`}><strong>{a.name}</strong></Link> <span className="muted">{a.kind}</span> <span>{a.apps || "no application yet"}</span>
                </div>
              )) : <Empty><p>No automations yet.</p></Empty>}
            </section>
            <section aria-labelledby="add-h">
              <h2 id="add-h">Add an automation</h2>
              <AddAutomation apps={p.apps} />
              <ImportAutomation />
            </section>
          </div>
        )}
      </QueryView>
    </>
  );
}

function AddAutomation({ apps }: { apps: Schemas["AppRef"][] }) {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [kind, setKind] = useState("");
  const [notes, setNotes] = useState("");
  const [chosen, setChosen] = useState<number[]>([]);
  const save = useAction(async () => data(await api.POST("/api/v1/automations", {
    body: { name: name.trim(), kind: kind.trim(), notes: notes.trim(), apps: chosen },
  })), (d) => navigate(`/automations/${d.automation.id}`));
  const submit = (e: FormEvent) => { e.preventDefault(); save.mutate(undefined); };
  return (
    <form aria-label="Add an automation" onSubmit={submit}>
      <label>Name <input required maxLength={120} placeholder="UiPath order to invoice" value={name} onChange={(e) => setName(e.target.value)} /></label>
      <label>Kind <input maxLength={40} placeholder="uipath, python, agentic" value={kind} onChange={(e) => setKind(e.target.value)} /></label>
      <label>Notes <textarea rows={3} maxLength={2000} value={notes} onChange={(e) => setNotes(e.target.value)} /></label>
      <fieldset>
        <legend>Applications it touches</legend>
        {apps.map((a) => (
          <label className="check" key={a.id}>
            <input type="checkbox" checked={chosen.includes(a.id)} onChange={(e) => setChosen((c) => (e.target.checked ? [...c, a.id] : c.filter((x) => x !== a.id)))} /> {a.name}
          </label>
        ))}
      </fieldset>
      <ErrorText error={save.error} />
      <button disabled={save.isPending}>Save automation</button>
    </form>
  );
}

function ImportAutomation() {
  const navigate = useNavigate();
  const [file, setFile] = useState<File | null>(null);
  const upload = useAction(async () => {
    const form = new FormData();
    if (file) form.set("file", file);
    return postForm<Schemas["AutomationDetail"]>("/api/v1/automations/import", form);
  }, (d) => navigate(`/automations/${d.automation.id}`));
  return (
    <form aria-label="Import automation" onSubmit={(e) => { e.preventDefault(); upload.mutate(undefined); }}>
      <label>Or import an automation file (automation.yaml) <input type="file" accept=".yaml,.yml" required onChange={(e) => setFile(e.target.files?.[0] ?? null)} /></label>
      <ErrorText error={upload.error} />
      <button disabled={upload.isPending}>Import</button>
    </form>
  );
}

export function AutomationPage() {
  const automationId = Number(useParams().automationId);
  const detail = useQuery({
    queryKey: ["automation", automationId],
    queryFn: async () => data(await api.GET("/api/v1/automations/{automation_id}", { params: { path: { automation_id: automationId } } })),
  });
  useTitle(detail.data?.automation.name);
  return (
    <QueryView query={detail}>
      {({ automation, apps, recordings }) => (
        <>
          <p><Link to="/automations">Automations</Link></p>
          <h1>{automation.name}</h1>
          {automation.kind && <p className="muted">{automation.kind}</p>}
          {automation.notes && <p className="notes">{automation.notes}</p>}
          <div className="columns">
            <section aria-labelledby="apps-h"><h2 id="apps-h">Applications</h2>
              {apps.length ? apps.map((a) => <p key={a.id}><Link to={`/apps/${a.id}`}>{a.name}</Link></p>) : <p>No application yet.</p>}
            </section>
            <section aria-labelledby="recs-h"><h2 id="recs-h">Recordings</h2>
              {recordings.length ? recordings.map((r) => (
                <p key={r.id}><Link to={`/recordings/${encodeURIComponent(r.id)}`}>{r.name}</Link> <span className="muted">{when(r.received_at)}</span></p>
              )) : <p>No recording made for it yet.</p>}
            </section>
          </div>
        </>
      )}
    </QueryView>
  );
}
