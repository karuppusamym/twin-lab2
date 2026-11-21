// The signed-in user's home, by role: a viewer gets the published demos; a builder the packages
// ready to build and the questions waiting for them; an owner the recordings' workboards and the
// triage queue; an admin also Setup. Everyone can still open the full catalog.
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, data, type Schemas } from "../api/client";
import { AccessRequestsCard } from "../components/AccessRequests";
import { Badge, Empty, QueryView, useTitle } from "../components/ui";
import { plural, when } from "../lib/format";

export function HomePage() {
  useTitle(undefined);
  const home = useQuery({ queryKey: ["home"], queryFn: async () => data(await api.GET("/api/v1/home")) });
  return <QueryView query={home}>{(h) => <HomeView home={h} />}</QueryView>;
}

const GREETING: Record<Schemas["HomeRole"], string> = {
  admin: "Portal administration, and the work of the applications you own.",
  owner: "Your recordings and what each application found in them still needs.",
  builder: "Approved packages ready to build, and questions waiting for your answer.",
  viewer: "The published demos you can open or download.",
  none: "You have no role on any application yet: open an application's link and request access, or ask its owner to add you.",
};

function HomeView({ home }: { home: Schemas["Home"] }) {
  const owner = home.role === "owner" || home.role === "admin";
  const builder = owner || home.role === "builder";
  return (
    <>
      <h1>Home</h1>
      <p className="muted">{GREETING[home.role]}</p>
      <AccessRequestsCard />
      {home.is_admin && <AdminCard />}
      {owner && <OwnerWork home={home} />}
      {builder && <BuilderWork home={home} />}
      <Published home={home} />
      <p><Link to="/apps">All applications</Link>{home.can_create_apps && " · add an application from there"}</p>
    </>
  );
}

function AdminCard() {
  return (
    <section aria-labelledby="admin-h" id="admin">
      <h2 id="admin-h">Portal setup</h2>
      <p><Link to="/setup">Setup</Link>: recorder machines, build providers and GitHub, artifact storage. <Link to="/admin/users">Users</Link>: who can sign in and who is an admin.</p>
    </section>
  );
}

function OwnerWork({ home }: { home: Schemas["Home"] }) {
  return (
    <section aria-labelledby="owner-h" id="workboards">
      <h2 id="owner-h">Recordings and scope decisions</h2>
      {home.triage_count > 0 ? (
        <p className="nextcard"><b>{plural(home.triage_count, "application")} {home.triage_count === 1 ? "waits" : "wait"} for a scope decision.</b>{" "}
          <Link className="button" to="/triage">Decide scope</Link></p>
      ) : <p className="muted">No application waits for a scope decision.</p>}
      {home.workboards.length ? (
        <div className="scroll">
          <table className="flow" aria-label="Recording workboards">
            <thead><tr><th scope="col">Recording</th><th scope="col">Received</th><th scope="col">Applications</th><th scope="col">Undecided</th></tr></thead>
            <tbody>{home.workboards.map((w) => (
              <tr key={w.id}>
                <th scope="row"><Link to={`/recordings/${encodeURIComponent(w.id)}`}>{w.name}</Link></th>
                <td>{when(w.received_at) || <span className="muted">imported</span>}</td>
                <td>{w.apps}</td>
                <td>{w.needs_decision > 0 ? <Badge kind="discovered">{w.needs_decision} to decide</Badge> : <span className="muted">none</span>}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      ) : <Empty><p>No recordings yet. <Link to="/sessions">Request a recording</Link>, or import one from <Link to="/recordings">Recordings</Link>.</p></Empty>}
    </section>
  );
}

function BuilderWork({ home }: { home: Schemas["Home"] }) {
  return (
    <section aria-labelledby="builder-h" id="ready-to-build">
      <h2 id="builder-h">Ready to build</h2>
      {home.waiting.length > 0 && (
        <ul className="list" aria-label="Waiting for your answer">{home.waiting.map((w) => (
          <li key={w.build_id}><Badge kind="waiting_for_answer">{plural(w.questions, "question")}</Badge>{" "}
            <Link to={`/builds/${w.build_id}`}>Build session #{w.build_id} of {w.name}</Link> waits for your answer.</li>
        ))}</ul>
      )}
      {home.ready_to_build.length ? (
        <div className="scroll">
          <table className="flow" aria-label="Packages ready to build">
            <thead><tr><th scope="col">Application</th><th scope="col">Approved package</th><th scope="col">Latest build</th><th scope="col">Release</th><th scope="col"><span className="visually-hidden">Action</span></th></tr></thead>
            <tbody>{home.ready_to_build.map((r) => (
              <tr key={r.app_id}>
                <th scope="row"><Link to={`/apps/${r.app_id}`}>{r.name}</Link></th>
                <td>Revision {r.rev} <span className="muted">{when(r.approved_at)}</span></td>
                <td>{r.build ? <><Badge kind={r.build.state}>{r.build.state_label}</Badge> <Link to={`/builds/${r.build.id}`}>#{r.build.id}</Link></> : <span className="muted">none yet</span>}</td>
                <td>{r.release ? <Link to={`/releases/${r.release.id}`}>{r.release.version} <Badge kind={r.release.state}>{r.release.state}</Badge></Link> : <span className="muted">none</span>}</td>
                <td><Link className="button" to={`/apps/${r.app_id}/build`} aria-label={`Build ${r.name}`}>{r.build ? "Build sessions" : "Start build"}</Link></td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      ) : <Empty><p>No package is approved for you to build yet.</p></Empty>}
    </section>
  );
}

function Published({ home }: { home: Schemas["Home"] }) {
  return (
    <section aria-labelledby="published-h" id="published">
      <h2 id="published-h">Published demos</h2>
      {home.published.length ? (
        <div className="cards">{home.published.map((d) => (
          <article className="card" key={d.app_id}>
            <h3><Link to={`/apps/${d.app_id}`}>{d.name}</Link> {d.verified && <Badge kind="verified">Verified deployment</Badge>}</h3>
            {d.purpose && <p className="muted">{d.purpose}</p>}
            <p>Version {d.version}</p>
            <p>
              {d.url && <><a className="button" href={d.url} aria-label={`Open the ${d.name} demo`}>Open</a>{" "}</>}
              <a href={`/api/v1/releases/${d.release_id}/download`} aria-label={`Download ${d.name} ${d.version}`}>Download</a>{" · "}
              <Link to={`/releases/${d.release_id}`} aria-label={`Release ${d.version} of ${d.name}`}>Release page</Link>
            </p>
          </article>
        ))}</div>
      ) : <Empty><p>No demo is published yet.</p></Empty>}
    </section>
  );
}
