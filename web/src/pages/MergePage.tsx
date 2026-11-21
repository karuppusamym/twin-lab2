import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, data, type Schemas } from "../api/client";
import { ErrorText, QueryView, useAction, useTitle } from "../components/ui";
import { short } from "../lib/format";
import { useAppId } from "./AppPage";

export function MergePage() {
  const appId = useAppId();
  const into = Number(useSearchParams()[0].get("into"));
  const plan = useQuery({
    queryKey: ["app", appId, "merge", into],
    queryFn: async () => data(await api.GET("/api/v1/apps/{app_id}/merge-plan", { params: { path: { app_id: appId }, query: { into } } })),
  });
  useTitle(plan.data ? `Merge ${plan.data.source.name}` : undefined);
  return <QueryView query={plan}>{(p) => <Plan plan={p} />}</QueryView>;
}

function None() {
  return <p className="muted">None.</p>;
}

function Plan({ plan }: { plan: Schemas["MergePlan"] }) {
  const navigate = useNavigate();
  const merge = useAction(async () => data(await api.POST("/api/v1/apps/{app_id}/merge", {
    params: { path: { app_id: plan.source.id } }, body: { into: plan.target.id, confirm: plan.fingerprint },
  })), (app) => navigate(`/apps/${app.id}`));
  return (
    <>
      <p><Link to={`/apps/${plan.source.id}`}>{plan.source.name}</Link></p>
      <h1>Merge {plan.source.name} into {plan.target.name}</h1>
      <p>Everything below moves to <Link to={`/apps/${plan.target.id}`}>{plan.target.name}</Link>, then {plan.source.name} is removed. Nothing is merged until you confirm.</p>
      {plan.blockers.length > 0 && <p className="error">This application cannot be merged while it has {plan.blockers.join(", ")}.</p>}
      <div className="columns">
        <section aria-labelledby="moves-h">
          <h2 id="moves-h">Moves</h2>
          <h3>Identities</h3>
          {plan.identities.length ? plan.identities.map((k) => <p key={k}><code>{k}</code></p>) : <None />}
          {plan.identities.length > 0 && plan.source.url_match !== plan.target.url_match && (
            <p className="muted">{plan.target.name} matches web addresses {plan.target.url_match === "host" ? "by the whole host" : "by path"}: the moved web addresses match that way too.</p>
          )}
          <h3>Recorded flows</h3>
          {plan.flows.length ? plan.flows.map((f) => (
            <p key={`${f.recording_id}/${f.key}`}>{f.name} <code>{f.key}</code>{f.shared && <span className="muted"> · also recorded for {plan.target.name}: the steps are combined</span>}</p>
          )) : <None />}
          <h3>Linked recordings</h3>
          {plan.links.length ? plan.links.map((l) => <p key={l.recording_id}>{l.name} <span className="muted">· {l.selected} events selected</span></p>) : <None />}
          <h3>Automations</h3>
          {plan.automations.length ? plan.automations.map((n) => <p key={n}>{n}</p>) : <None />}
          <h3>Scenarios</h3>
          {plan.scenarios.length ? plan.scenarios.map((n) => <p key={n}>{n}</p>) : <None />}
          <h3>Build briefs</h3>
          {plan.briefs.length ? plan.briefs.map((b) => (
            <p key={b.revision}><code>{short(b.revision)}</code> <span className="muted">· {b.package ? "kept with its download" : "kept"}</span></p>
          )) : <None />}
        </section>
        <section aria-labelledby="combined-h">
          <h2 id="combined-h">Combined</h2>
          {plan.combined.length ? plan.combined.map((link) => (
            <div key={link.recording_id}>
              <h3>{link.name}</h3>
              <p>Both applications link this recording. {plan.source.name} selected {link.source.length} events, {plan.target.name} {link.target.length}: the merged link selects {link.selection.length} ({link.shown.join(", ")}).
                {link.suggested.length > 0 && <> {link.suggested.length} more ({link.shown_suggested.join(", ")}) were not reviewed and stay <span className="badge discovered">new, not yet reviewed</span> until you select them.</>}</p>
              {link.notes && <p className="notes">{link.notes}</p>}
            </div>
          )) : <p className="muted">No recording is linked to both applications.</p>}
        </section>
      </div>
      {plan.blockers.length === 0 && (
        <form aria-label="Confirm merge" onSubmit={(e) => { e.preventDefault(); merge.mutate(undefined); }}>
          <ErrorText error={merge.error} />
          <button disabled={merge.isPending}>Merge into {plan.target.name}</button>
          <Link to={`/apps/${plan.source.id}`}>Cancel</Link>
        </form>
      )}
    </>
  );
}
