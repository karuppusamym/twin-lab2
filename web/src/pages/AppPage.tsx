import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, data, postForm, type Schemas } from "../api/client";
import { ScenarioForm } from "../components/ScenarioForm";
import { FlowTable, JourneyNav } from "../components/shared";
import { ActionButton, Badge, Crumbs, ErrorText, QueryView, useAction, useField, useTitle } from "../components/ui";
import { plural, short, when } from "../lib/format";
import { useCan, useRefreshSession } from "../session";

export function useAppId() {
  const { appId } = useParams();
  return Number(appId);
}

export function AppPage() {
  const appId = useAppId();
  const can = useCan();
  const owner = can("owner", appId);
  const app = useQuery({ queryKey: ["app", appId], queryFn: async () => data(await api.GET("/api/v1/apps/{app_id}", { params: { path: { app_id: appId } } })) });
  // restricted evidence and scenarios: fetched only for owners (the server refuses others)
  const evidence = useQuery({
    queryKey: ["app", appId, "evidence"], enabled: owner,
    queryFn: async () => data(await api.GET("/api/v1/apps/{app_id}/evidence", { params: { path: { app_id: appId } } })),
  });
  const scenarios = useQuery({
    queryKey: ["app", appId, "scenarios"], enabled: owner,
    queryFn: async () => data(await api.GET("/api/v1/apps/{app_id}/scenarios", { params: { path: { app_id: appId } } })),
  });
  useTitle(app.data?.name);
  return (
    <QueryView query={app}>
      {(item) => <AppView item={item} evidence={owner ? evidence.data : undefined} scenarios={owner ? scenarios.data : undefined} evidenceError={evidence.error ?? scenarios.error} />}
    </QueryView>
  );
}

function AppView({ item, evidence, scenarios, evidenceError }: {
  item: Schemas["AppDetail"];
  evidence?: Schemas["AppEvidence"];
  scenarios?: Schemas["ScenarioList"];
  evidenceError: unknown;
}) {
  const can = useCan();
  const reviewer = can("owner", item.id);
  const builder = can("builder", item.id);
  const manageRoles = reviewer || can("admin");
  const j = item.journey;
  return (
    <>
      <Crumbs><Link to="/">Applications</Link></Crumbs>
      <h1>{item.name} <Badge kind={item.status}>{item.status_label}</Badge></h1>
      <dl className="about">
        <dt>Owner</dt><dd>{item.owner || "not set"}</dd>
        <dt>Purpose</dt><dd>{item.purpose || "not set"}</dd>
        <dt>Stage</dt><dd>{j.stage}</dd>
        {j.missing.length > 0 && <><dt>Missing</dt><dd>{j.missing.join(", ")}</dd></>}
      </dl>
      <JourneyNav journey={j} name={item.name} />
      {reviewer && <p className="muted">Identities, Git repository and deployment settings: <Link to={`/apps/${item.id}/setup`}>Setup / Advanced</Link>.</p>}
      {manageRoles && <p className="muted"><Link to={`/apps/${item.id}/history`}>Audit history</Link>: who reviewed evidence, froze packages, started builds, published releases and changed roles.</p>}
      {!reviewer && (
        <p className="muted" id="read-only">You have read-only access to this application. Recordings and uploaded evidence are restricted to its owners{builder ? "; you can open its builder-approved packages below" : ""}.</p>
      )}
      <ErrorText error={evidenceError} />

      <section id="choose" aria-labelledby="choose-h">
        <h2 id="choose-h">1. Choose application</h2>
        {item.status === "discovered" && reviewer && (
          <p>A recording found this application. Confirm it to replicate it, rename it, merge it into an application it is the same as, or mark it as not replicated: later recordings then note it only as having been used.</p>
        )}
        <div className="columns">
          <Roles appId={item.id} manage={manageRoles} />
          {reviewer && <OwnerForms item={item} others={evidence?.merge_candidates ?? []} />}
        </div>
      </section>

      {reviewer && evidence && <Examples item={item} evidence={evidence} />}

      {reviewer && scenarios && (
        <section id="behaviour" aria-labelledby="behaviour-h">
          <h2 id="behaviour-h">3. Review required behaviour</h2>
          <div id="scenarios">
            <p className="muted">One business scenario per outcome the copy must reproduce. Use made-up example values only: the build pack must contain no real customer data.</p>
            {scenarios.scenarios.map((sc) => <ScenarioForm key={sc.id} appId={item.id} list={scenarios} scenario={sc} />)}
            <ScenarioForm appId={item.id} list={scenarios} />
          </div>
        </section>
      )}

      <section id="build" aria-labelledby="build-h">
        <h2 id="build-h">4. Build</h2>
        {reviewer && (scenarios?.scenarios.length
          ? <p><Link className="button" to={`/apps/${item.id}/build-pack/preview`}>Preview and approve a builder package</Link></p>
          : <p className="muted">Add a business scenario to prepare a builder package.</p>)}
        {builder && (
          <>
            {item.packages.length > 0 && <p><Link className="button" to={`/apps/${item.id}/build`}>Build sessions</Link></p>}
            <h3>Builder-approved packages</h3>
            {item.packages.length ? item.packages.map((p) => (
              <p key={p.rev}>
                <Link to={`/apps/${item.id}/packages/${p.rev}`}>Revision {p.rev}</Link>{" "}
                <span className="muted">approved by {p.approved_by} · {when(p.approved_at)} · <code>{short(p.sha256)}</code></span>
                {" · "}<a href={`/api/v1/apps/${item.id}/packages/${p.rev}/download`}>Download</a>
              </p>
            )) : <p>No package is approved for the builder yet.</p>}
          </>
        )}
        <h3>Releases</h3>
        {item.releases.length ? item.releases.map((r) => (
          <p key={r.id}><Link to={`/releases/${r.id}`}>{r.version}</Link> <code>{short(r.commit_sha)}</code> <Badge kind={r.state}>{r.state.charAt(0).toUpperCase() + r.state.slice(1)}</Badge></p>
        )) : <p>Register a committed demo with the release command in the project README.</p>}
      </section>

      <section id="verify" aria-labelledby="verify-h">
        <h2 id="verify-h">5. Verify · 6. Open or download</h2>
        {item.releases[0]
          ? <p>Verify and open each release from its page: <Link to={`/releases/${item.releases[0].id}`}>{item.releases[0].version}</Link> is the latest.</p>
          : <p>A release appears here once it is built and registered.</p>}
      </section>
    </>
  );
}

function Roles({ appId, manage }: { appId: number; manage: boolean }) {
  const refresh = useRefreshSession();
  const roles = useQuery({ queryKey: ["app", appId, "roles"], queryFn: async () => data(await api.GET("/api/v1/apps/{app_id}/roles", { params: { path: { app_id: appId } } })) });
  const [user, setUser] = useState("");
  const [role, setRole] = useState<Schemas["Role"]>("owner");
  const assign = useAction(async () => data(await api.PUT("/api/v1/apps/{app_id}/roles/{user_name}", {
    params: { path: { app_id: appId, user_name: user.trim() } }, body: { role },
  })), () => { setUser(""); void refresh(); });
  const submit = (e: FormEvent) => { e.preventDefault(); assign.mutate(undefined); };
  return (
    <div id="roles">
      <h3>Who has access</h3>
      <QueryView query={roles}>
        {(list) => (
          <>
            <table className="flow">
              <thead><tr><th scope="col">User</th><th scope="col">Role</th><th scope="col"><span className="visually-hidden">Actions</span></th></tr></thead>
              <tbody>
                {list.roles.length ? list.roles.map((r) => (
                  <tr key={r.user_name}>
                    <td>{r.user_name}</td><td>{r.role}</td>
                    <td>{manage && <ActionButton secondary label="Remove" run={async () => {
                      data(await api.DELETE("/api/v1/apps/{app_id}/roles/{user_name}", { params: { path: { app_id: appId, user_name: r.user_name } } }));
                      await refresh();
                    }} />}</td>
                  </tr>
                )) : <tr><td colSpan={3}>No one is assigned yet{manage ? ": assign an owner" : ""}.</td></tr>}
              </tbody>
            </table>
            {manage && (
              <form onSubmit={submit} aria-label="Assign role">
                <label>User (their sign-in name) <input required maxLength={120} value={user} onChange={(e) => setUser(e.target.value)} /></label>
                <label>Role
                  <select value={role} onChange={(e) => setRole(e.target.value as Schemas["Role"])}>
                    {[...list.role_names].reverse().map((r) => <option key={r} value={r}>{r}</option>)}
                  </select>
                </label>
                <ErrorText error={assign.error} />
                <button disabled={assign.isPending}>Assign role</button>
              </form>
            )}
            {manage && <p className="muted">Owners review evidence, freeze builder packages and publish; builders get the frozen packages and start builds; viewers read this page and published releases.</p>}
          </>
        )}
      </QueryView>
    </div>
  );
}

function OwnerForms({ item, others }: { item: Schemas["AppDetail"]; others: Schemas["AppRef"][] }) {
  const navigate = useNavigate();
  const [purpose, setPurpose] = useField(item.purpose);
  const [name, setName] = useField(item.name);
  const [status, setStatus] = useField(item.status);
  const [into, setInto] = useState<string>("");
  const patch = (body: Schemas["AppUpdate"]) => async () => data(await api.PATCH("/api/v1/apps/{app_id}", { params: { path: { app_id: item.id } }, body }));
  const savePurpose = useAction(patch({ purpose }));
  const saveStatus = useAction(patch({ name: name.trim() || undefined, status: status as Schemas["AppStatus"] }));
  return (
    <div>
      <form aria-label="Purpose" onSubmit={(e) => { e.preventDefault(); savePurpose.mutate(undefined); }}>
        <label>Purpose <input maxLength={500} value={purpose} placeholder="What the demo is for" onChange={(e) => setPurpose(e.target.value)} /></label>
        <ErrorText error={savePurpose.error} />
        <button disabled={savePurpose.isPending}>Save purpose</button>
      </form>
      <form aria-label="Name and scope" onSubmit={(e) => { e.preventDefault(); saveStatus.mutate(undefined); }}>
        <label>Name <input maxLength={120} value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label>Scope decision
          <select value={status} onChange={(e) => setStatus(e.target.value as Schemas["AppStatus"])}>
            {item.statuses.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </select>
        </label>
        <ErrorText error={saveStatus.error} />
        <button disabled={saveStatus.isPending}>Save</button>
      </form>
      {others.length > 0 && (
        <form aria-label="Merge" onSubmit={(e) => { e.preventDefault(); navigate(`/apps/${item.id}/merge?into=${into || others[0]?.id}`); }}>
          <label>Same application as
            <select value={into || String(others[0]?.id)} onChange={(e) => setInto(e.target.value)}>
              {others.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
            </select>
          </label>
          <button className="secondary">Preview merge</button>
        </form>
      )}
    </div>
  );
}

function Examples({ item, evidence }: { item: Schemas["AppDetail"]; evidence: Schemas["AppEvidence"] }) {
  return (
    <section id="examples" aria-labelledby="examples-h">
      <h2 id="examples-h">2. Add examples</h2>
      <p className="muted">Recordings and uploads are <strong>restricted evidence</strong>: only the owners of this application see them. Uploading never shares anything with the builder; only a builder package you approve does.</p>
      <p><Link to="/sessions">Request a recording</Link> of the process on a paired machine, or import a Twin Lab recording from the <Link to="/">catalog</Link>.</p>
      <div className="columns">
        <div>
          <h3>Recorded flows</h3>
          {evidence.recorded_flows.length ? evidence.recorded_flows.map((rec, i) => (
            <details key={`${rec.recording_id}/${rec.key}`} open={i === 0}>
              <summary>
                <Link to={`/recordings/${encodeURIComponent(rec.recording_id)}`}>{rec.recording_name}</Link>{" "}
                <span className="muted">{rec.level} · {plural(rec.steps.length, "step")}{rec.machine ? ` · ${rec.machine}` : ""}{rec.received_at ? ` · ${when(rec.received_at)}` : ""}</span>
              </summary>
              <FlowTable steps={rec.steps} caption={`Steps of ${item.name} in ${rec.recording_name}`} />
            </details>
          )) : <p>No recording of this application yet.</p>}
          <h3>Automations</h3>
          {evidence.automations.length
            ? evidence.automations.map((a) => <p key={a.id}><Link to={`/automations/${a.id}`}>{a.name}</Link></p>)
            : <p>No automation yet.</p>}
        </div>
        <div>
          <h3>Linked recordings</h3>
          {evidence.links.length ? evidence.links.map((link) => (
            <div key={link.recording_id}>
              <h4><Link to={`/recordings/${encodeURIComponent(link.recording_id)}?app_id=${item.id}`}>{link.name}</Link></h4>
              <p>{link.selected_count} events selected{" "}
                {!link.reviewed && <Badge kind="discovered">not yet reviewed</Badge>}{" "}
                {link.suggested_count > 0 && <Badge kind="discovered">{link.suggested_count} new, not yet reviewed</Badge>}</p>
              {link.notes && <p className="notes">{link.notes}</p>}
            </div>
          )) : <p>Open a recording from the catalog to link its events to this app.</p>}
          <h3>Uploaded evidence</h3>
          {evidence.attachments.map((file) => <AttachmentRow key={file.id} appId={item.id} file={file} states={evidence.share_states} />)}
          <UploadEvidence appId={item.id} evidence={evidence} />
        </div>
      </div>
    </section>
  );
}

function AttachmentRow({ appId, file, states }: { appId: number; file: Schemas["Attachment"]; states: Schemas["Choice"][] }) {
  const [share, setShare] = useField(file.share);
  const save = useAction(async () => data(await api.PATCH("/api/v1/apps/{app_id}/attachments/{attachment_id}", {
    params: { path: { app_id: appId, attachment_id: file.id } }, body: { share: share as Schemas["ShareState"] },
  })));
  return (
    <div className="attachment">
      <p><span>{file.kind} · {file.title}</span> <Badge kind={file.share === "restricted" ? "discovered" : "recorded"}>{file.share}</Badge></p>
      <form className="inline" aria-label={`Review of ${file.title}`} onSubmit={(e) => { e.preventDefault(); save.mutate(undefined); }}>
        <select aria-label={`Share ${file.title}`} value={share} onChange={(e) => setShare(e.target.value as Schemas["ShareState"])}>
          {states.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
        </select>{" "}
        <button className="secondary" disabled={save.isPending}>Save review</button>
      </form>{" "}
      <ActionButton secondary label="Remove" run={async () => {
        await api.DELETE("/api/v1/apps/{app_id}/attachments/{attachment_id}", { params: { path: { app_id: appId, attachment_id: file.id } } });
      }} />
      <ErrorText error={save.error} />
    </div>
  );
}

function UploadEvidence({ appId, evidence }: { appId: number; evidence: Schemas["AppEvidence"] }) {
  const kinds = Object.keys(evidence.attachment_kinds);
  const [title, setTitle] = useState("");
  const [kind, setKind] = useState(kinds[0] ?? "");
  const [share, setShare] = useState<string>("restricted");
  const [file, setFile] = useState<File | null>(null);
  const upload = useAction(async () => {
    const form = new FormData();
    form.set("title", title.trim());
    form.set("kind", kind);
    form.set("share", share);
    if (file) form.set("file", file);
    return postForm<Schemas["Attachment"]>(`/api/v1/apps/${appId}/attachments`, form);
  }, () => setTitle(""));
  return (
    <form aria-label="Upload evidence" onSubmit={(e) => { e.preventDefault(); upload.mutate(undefined); }}>
      <label>Evidence title <input required maxLength={200} value={title} onChange={(e) => setTitle(e.target.value)} /></label>
      <label>Evidence type
        <select value={kind} onChange={(e) => setKind(e.target.value)}>{kinds.map((k) => <option key={k}>{k}</option>)}</select>
      </label>
      <label>File <input type="file" required accept={(evidence.attachment_kinds[kind] ?? []).join(",")} onChange={(e) => setFile(e.target.files?.[0] ?? null)} /></label>
      <label>Share with the builder
        <select value={share} onChange={(e) => setShare(e.target.value)}>{evidence.share_states.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}</select>
      </label>
      <p className="muted">Screenshots and files stay restricted until you mark them reviewed or synthetic. Shared files are still redacted (HAR credentials and bodies, sensitive JSON fields, image metadata, e-mail addresses, card numbers…); the package preview lists every exclusion and redaction.</p>
      <ErrorText error={upload.error} />
      <button disabled={upload.isPending}>Upload evidence</button>
    </form>
  );
}
