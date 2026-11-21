import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useDeferredValue, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, data, postForm, type Schemas } from "../api/client";
import { JourneyLink, TestedReleaseLine } from "../components/shared";
import { Badge, ErrorText, Pager, QueryView, useAction, useTitle } from "../components/ui";
import { SCOPE_LABELS } from "../lib/scope";
import { useSession } from "../session";

const STATUSES = (Object.keys(SCOPE_LABELS) as Schemas["AppStatus"][]).map((value) => ({ value, label: SCOPE_LABELS[value] }));
const SORTS = [
  { value: "", label: "Catalog order" }, { value: "name", label: "Name (A–Z)" }, { value: "-name", label: "Name (Z–A)" },
  { value: "owner", label: "Owner" }, { value: "status", label: "Status" }, { value: "stage", label: "Stage (earliest first)" },
  { value: "-stage", label: "Stage (furthest first)" },
] as const;
type Sort = Exclude<(typeof SORTS)[number]["value"], "">;
type Family = Schemas["AppFamily"];
const LIMIT = 50;
const FAMILIES: Family[] = ["decide", "build", "tested", "published", "not_replicated"];

/** The collapsed groups: the `collapsed` URL parameter (comma-separated; empty = none), else the
 * out-of-scope group, unless the status filter asks for exactly those applications. */
function collapsedOf(value: string | null, status: string): Family[] {
  if (value === null) return status === "not_replicated" ? [] : ["not_replicated"];
  return value.split(",").filter((f): f is Family => (FAMILIES as string[]).includes(f));
}

export function CatalogPage() {
  useTitle("Applications");
  const session = useSession();
  const canCreate = session.can_create_apps ?? true;
  const owner = session.is_admin || session.roles.some((r) => r.role === "owner");
  const [params, setParams] = useSearchParams();
  const get = (k: string) => params.get(k) ?? "";
  const [q, setQ] = useState(get("q"));
  const search = useDeferredValue(q.trim());
  const view = get("view") === "table" ? "table" : "cards";
  const filters = {
    q: search || undefined, owner: get("owner") || undefined, stage: get("stage") || undefined,
    status: (get("status") || undefined) as Schemas["AppStatus"] | undefined, sort: (get("sort") || undefined) as Sort | undefined,
  };
  const offset = Number(get("offset")) || 0;
  const collapsed = collapsedOf(params.get("collapsed"), get("status"));
  const catalog = useQuery({
    queryKey: ["apps", filters, collapsed, offset],
    queryFn: async () => data(await api.GET("/api/v1/apps", { params: { query: { ...filters, collapsed, limit: LIMIT, offset } } })),
    placeholderData: keepPreviousData,
  });
  /** Change one URL parameter (a filter goes back to the first page). */
  const put = (key: string, value: string) => setParams((p) => {
    const next = new URLSearchParams(p);
    if (value) next.set(key, value); else next.delete(key);
    if (key !== "offset") next.delete("offset");
    return next;
  }, { replace: true });
  /** Open or close one group (kept in the URL: back, reload and links keep it); back to page one. */
  const toggle = (family: Family) => setParams((p) => {
    const next = new URLSearchParams(p);
    const now = collapsed.includes(family) ? collapsed.filter((f) => f !== family) : [...collapsed, family];
    next.set("collapsed", FAMILIES.filter((f) => now.includes(f)).join(","));
    next.delete("offset");
    return next;
  }, { replace: true });
  const filtered = Boolean(filters.q || filters.owner || filters.stage || filters.status);
  return (
    <>
      <h1>Applications</h1>
      <QueryView query={catalog}>
        {(c) => (
          <>
            <p className="journey">{c.journey_titles.map((t) => <span key={t}>{t}</span>)}</p>
            <form className="toolbar" aria-label="Find applications" role="search" onSubmit={(e) => e.preventDefault()}>
              <label>Search <input type="search" value={q} placeholder="Name, purpose or owner" onChange={(e) => { setQ(e.target.value); put("q", e.target.value.trim()); }} /></label>
              <label>Owner
                <select value={get("owner")} onChange={(e) => put("owner", e.target.value)}>
                  <option value="">Any owner</option>
                  {c.filters.owners.map((o) => <option key={o} value={o}>{o}</option>)}
                </select>
              </label>
              <label>Status
                <select value={get("status")} onChange={(e) => put("status", e.target.value)}>
                  <option value="">Any status</option>
                  {STATUSES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
                </select>
              </label>
              <label>Stage
                <select value={get("stage")} onChange={(e) => put("stage", e.target.value)}>
                  <option value="">Any stage</option>
                  {c.filters.stages.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </label>
              <label>Sort
                <select value={get("sort")} onChange={(e) => put("sort", e.target.value)}>
                  {SORTS.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
                </select>
              </label>
              <div className="view-toggle" role="group" aria-label="View">
                <button type="button" className="secondary" aria-pressed={view === "cards"} onClick={() => put("view", "")}>Cards</button>
                <button type="button" className="secondary" aria-pressed={view === "table"} onClick={() => put("view", "table")}>Table</button>
              </div>
            </form>
            <p className="muted" role="status" aria-live="polite">
              {(() => {
                const all = c.groups.reduce((n, g) => n + g.count, 0);
                const folded = all - c.page.total;
                return `${all === 1 ? "1 application" : `${all} applications`}${filtered ? (all === 1 ? " matches" : " match") : ""}`
                  + (folded > 0 ? ` · ${folded} in collapsed groups` : "");
              })()}
              {catalog.isFetching && !catalog.isPending ? " · updating…" : ""}
            </p>
            {c.groups.some((g) => g.count > 0) ? (
              <div className="catalog-groups">
                {c.groups.filter((g) => g.count > 0).map((g) => (
                  <AppGroup key={g.family} group={g} apps={c.apps.filter((a) => a.family === g.family)} page={c.page} view={view}
                    onToggle={() => toggle(g.family)} onShow={() => put("offset", g.offset ? String(g.offset) : "")} />
                ))}
              </div>
            ) : filtered ? (
              <p>No application matches. <button type="button" className="secondary" onClick={() => { setQ(""); setParams(view === "table" ? { view } : {}); }}>Clear the filters</button></p>
            ) : (
              <p>No applications yet.{canCreate ? " Add one below, or a recording registers the applications it finds." : " An owner adds you to an application."}</p>
            )}
            <Pager page={c.page} onOffset={(o) => put("offset", o ? String(o) : "")} label="Application pages" />
            {(canCreate || owner) && <div className="columns">
              {canCreate && <AddApp />}
              {owner && <section aria-labelledby="recordings-heading">
                <h2 id="recordings-heading">Recordings</h2>
                {c.recordings.length ? (
                  <ul className="list">{c.recordings.map((r) => <li key={r.id}><Link to={`/recordings/${encodeURIComponent(r.id)}`}>{r.name}</Link></li>)}</ul>
                ) : <p>No recordings yet.</p>}
                {c.recordings.length >= 50 && <p><Link to="/recordings">All recordings</Link></p>}
                <ImportRecording />
              </section>}
            </div>}
          </>
        )}
      </QueryView>
    </>
  );
}

/** One status family: a heading that opens and closes it, with its count over every page, and
 * the applications of this page in it. A group whose applications are on another page says so. */
function AppGroup({ group, apps, page, view, onToggle, onShow }: {
  group: Schemas["CatalogGroup"]; apps: Schemas["AppSummary"][]; page: Schemas["Page"]; view: "cards" | "table";
  onToggle: () => void; onShow: () => void;
}) {
  const id = `group-${group.family}`;
  const open = !group.collapsed;
  const start = group.offset ?? 0;
  const before = open && apps.length > 0 && start < page.offset;
  const after = open && apps.length > 0 && start + group.count > page.offset + page.limit;
  return (
    <div className="catalog-group" role="region" aria-labelledby={`${id}-heading`} data-family={group.family}>
      <h2 id={`${id}-heading`} className="group-heading">
        <button type="button" className="group-toggle" aria-expanded={open} aria-controls={id} onClick={onToggle}>
          <span className="chevron" aria-hidden="true" />
          <span className="group-label">{group.label}</span>
          {" "}<span className="count">{group.count}<span className="visually-hidden"> {group.count === 1 ? "application" : "applications"}</span></span>
        </button>
        {before && <span className="muted group-note">continued from the previous page</span>}
      </h2>
      <div id={id} hidden={!open}>
        {apps.length ? (
          view === "table" ? <AppTable apps={apps} label={group.label} /> : <div className="cards">{apps.map((app) => <AppCard key={app.id} app={app} />)}</div>
        ) : open ? (
          <p className="muted group-note">On another page. <button type="button" className="link" onClick={onShow}>Show {group.label.toLowerCase()}</button></p>
        ) : null}
        {after && <p className="muted group-note">More on the next page.</p>}
      </div>
    </div>
  );
}

function AppCard({ app }: { app: Schemas["AppSummary"] }) {
  const j = app.journey;
  return (
    <article className="card">
      <h3><Link to={`/apps/${app.id}`}>{app.name}</Link> <Badge kind={app.status}>{app.status_label}</Badge></h3>
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

/** The compact view: one row per application. */
function AppTable({ apps, label }: { apps: Schemas["AppSummary"][]; label: string }) {
  return (
    <div className="scroll">
      <table className="flow apps-table" aria-label={`Applications: ${label}`}>
        <thead><tr><th scope="col">Application</th><th scope="col">Status</th><th scope="col">Owner</th><th scope="col">Stage</th><th scope="col">Missing</th><th scope="col">Next step</th></tr></thead>
        <tbody>{apps.map((app) => (
          <tr key={app.id}>
            <th scope="row"><Link to={`/apps/${app.id}`}>{app.name}</Link>{app.purpose && <div className="muted">{app.purpose}</div>}</th>
            <td><Badge kind={app.status}>{app.status_label}</Badge></td>
            <td>{app.owner || <span className="muted">not set</span>}</td>
            <td>{app.journey.stage}</td>
            <td>{app.journey.missing.join(", ")}</td>
            <td>{app.journey.out_of_scope ? <span className="muted">out of scope</span>
              : app.journey.next ? <JourneyLink href={app.journey.next.href} title={app.journey.next.hint}>{app.journey.next.action}</JourneyLink> : null}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
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
    [["apps"]],
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
    [["apps"], ["recordings"]],
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
