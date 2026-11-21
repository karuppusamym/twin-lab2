import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, data, postForm, type Schemas } from "../api/client";
import { JourneyLink, TestedReleaseLine } from "../components/shared";
import { Badge, ErrorText, QueryView, useAction, useTitle } from "../components/ui";

export function CatalogPage() {
  useTitle(undefined);
  const catalog = useQuery({ queryKey: ["apps"], queryFn: async () => data(await api.GET("/api/v1/apps")) });
  return (
    <>
      <h1>Applications</h1>
      <QueryView query={catalog}>
        {(c) => (
          <>
            <p className="journey">{c.journey_titles.map((t) => <span key={t}>{t}</span>)}</p>
            {c.apps.length ? (
              <div className="cards">{c.apps.map((app) => <AppCard key={app.id} app={app} />)}</div>
            ) : (
              <p>No applications yet. Add one below, or a recording registers the applications it finds.</p>
            )}
            <div className="columns">
              <AddApp />
              <section aria-labelledby="recordings-heading">
                <h2 id="recordings-heading">Recordings</h2>
                {c.recordings.length ? (
                  <ul className="list">{c.recordings.map((r) => <li key={r.id}><Link to={`/recordings/${encodeURIComponent(r.id)}`}>{r.name}</Link></li>)}</ul>
                ) : <p>No recordings yet.</p>}
                <ImportRecording />
              </section>
            </div>
          </>
        )}
      </QueryView>
    </>
  );
}

function AppCard({ app }: { app: Schemas["AppSummary"] }) {
  const j = app.journey;
  return (
    <article className="card">
      <h2><Link to={`/apps/${app.id}`}>{app.name}</Link> <Badge kind={app.status}>{app.status_label}</Badge></h2>
      <dl className="about">
        <dt>Owner</dt><dd>{app.owner || "not set"}</dd>
        <dt>Purpose</dt><dd>{app.purpose || "not set"}</dd>
        <dt>Stage</dt><dd>{j.stage}</dd>
        {j.missing.length > 0 && <><dt>Missing</dt><dd>{j.missing.join(", ")}</dd></>}
      </dl>
      {app.tested_release && <TestedReleaseLine release={app.tested_release} />}
      {j.out_of_scope ? (
        <p className="muted">Recordings note it only as used.</p>
      ) : j.next ? (
        <p><JourneyLink className="button" href={j.next.href} title={j.next.hint}>{j.next.action}</JourneyLink></p>
      ) : null}
    </article>
  );
}

function AddApp() {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [owner, setOwner] = useState("");
  const [purpose, setPurpose] = useState("");
  const add = useAction(
    async () => data(await api.POST("/api/v1/apps", { body: { name: name.trim(), owner: owner.trim() || undefined, purpose: purpose.trim() || undefined } })),
    (app) => navigate(`/apps/${app.id}`),
  );
  const submit = (e: FormEvent) => { e.preventDefault(); add.mutate(undefined); };
  return (
    <section aria-labelledby="add-heading">
      <h2 id="add-heading">Add an application</h2>
      <form onSubmit={submit}>
        <label>Demo name <input required value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label>Owner <input maxLength={120} value={owner} onChange={(e) => setOwner(e.target.value)} /></label>
        <label>Purpose <input maxLength={500} placeholder="What the demo is for" value={purpose} onChange={(e) => setPurpose(e.target.value)} /></label>
        <ErrorText error={add.error} />
        <button disabled={add.isPending}>Create demo app</button>
      </form>
    </section>
  );
}

function ImportRecording() {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const upload = useAction(
    async () => {
      const form = new FormData();
      form.set("name", name.trim());
      if (file) form.set("log", file);
      return postForm<Schemas["RecordingRef"]>("/api/v1/recordings", form);
    },
    (rec) => navigate(`/recordings/${encodeURIComponent(rec.id)}`),
  );
  const submit = (e: FormEvent) => { e.preventDefault(); upload.mutate(undefined); };
  return (
    <form onSubmit={submit} aria-label="Import recording">
      <label>Recording name <input required value={name} onChange={(e) => setName(e.target.value)} /></label>
      <label>Recording file (steps.jsonl) <input type="file" accept=".jsonl" required onChange={(e) => setFile(e.target.files?.[0] ?? null)} /></label>
      <ErrorText error={upload.error} />
      <button disabled={upload.isPending}>Import recording</button>
    </form>
  );
}
